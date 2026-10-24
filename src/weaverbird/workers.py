"""The worker framework — every factory step as an independent
worker (0.7.0).

Before 0.7.0 the factory's steps lived inside their stage
modules and were wired together by the build stage and the
pipeline handlers: one monolith per stage, with the build stage
itself a single function that made the world, the mechanics and
the assembly in one breath. The user's directive for this pass:
*every* different step is an independent worker — asset creation
independent from world creation independent from player/
mechanics management — coordinated by an orchestrator
(orchestrator.py), with nothing left as a special case.

The uniform contract:

- A worker is registered in :data:`WORKERS` with a name, a
  title, its declared `inputs` (artifact paths it reads, relative
  to the project), its declared `outputs` (artifact paths it
  writes), the workers it `depends_on`, and a
  `run(project_dir, ctx) -> WorkerResult` adapter.
- Adapters do not fork logic: each one calls the same library
  entry point the CLI and the app call, then translates that
  module's report file into a WorkerResult
  {ok, artifacts, metrics, issues, summary}. Issues are
  {kind, detail, owner} — the owner is the worker the
  orchestrator routes a failure back to.
- `ctx` (:class:`WorkerContext`) carries the reporter, the
  settings path, the inputs hash, the run attempt, the issues
  from a previous attempt (the retry digest), and job_id when
  the run came through the job runner.
- Every run writes a run record at
  `.weaverbird/workers/<name>.json`:
  {worker, ran_at, inputs_hash, ok, artifacts, metrics, issues,
  summary, job_id?}. The inputs hash covers the worker's
  declared input files' contents plus its relevant config
  slice (per-worker `scope_fields`, or a custom slice function);
  the orchestrator skips a worker whose record matches the
  current hash and says ok — and only that worker's dependents
  re-run when an input changes.

The registry is code, like the asset generator registry: adding
a step to the factory means registering a worker here.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import scope as scope_module

WORKERS_DIR = "workers"


class WorkerError(Exception):
    """Raised when a worker run is refused outright."""


@dataclass
class WorkerContext:
    """What a worker adapter receives."""

    project_dir: Path
    reporter: object = None
    settings_path: object = None
    inputs_hash: str = ""
    attempt: int = 0
    issues: list = field(default_factory=list)
    job_id: str | None = None
    params: dict = field(default_factory=dict)


@dataclass
class WorkerSpec:
    name: str
    title: str
    inputs: tuple = ()
    outputs: tuple = ()
    depends_on: tuple = ()
    run: object = None
    # Scope fields whose VALUES feed this worker — part of its
    # inputs hash (its "relevant config slice"). None means the
    # worker declares a custom slice via slice_fn.
    scope_fields: tuple | None = ()
    slice_fn: object = None
    can_skip: bool = True
    self_corrects: bool = False


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def result(ok: bool, summary: str, artifacts=(), metrics=None,
           issues=()) -> dict:
    """A WorkerResult in the contract's shape."""
    return {
        "ok": bool(ok),
        "artifacts": list(artifacts),
        "metrics": dict(metrics or {}),
        "issues": [dict(i) for i in issues],
        "summary": str(summary),
    }


def issue(kind: str, detail: str, owner: str) -> dict:
    return {"kind": kind, "detail": detail, "owner": owner}


# ---------------------------------------------------------------------------
# Inputs hashing + run records

def scope_values(project_dir) -> dict:
    scope_file = scope_module.scope_path_for(project_dir)
    if not scope_file.exists():
        return {}
    try:
        parsed = scope_module.load_scope(scope_file)
    except scope_module.ScopeError:
        return {}
    return {f.name: f.value for f in parsed.fields}


def inputs_hash(project_dir, spec: WorkerSpec) -> str:
    """Hash of the worker's declared input files' contents plus
    its relevant config slice. Missing files hash as a marker —
    their absence is part of the state."""
    project = Path(project_dir)
    digest = hashlib.sha256()
    digest.update(spec.name.encode("utf-8"))
    for rel in spec.inputs:
        path = project / rel
        digest.update(b"\nfile:" + rel.encode("utf-8") + b"=")
        if path.is_file():
            digest.update(path.read_bytes())
        elif path.is_dir():
            for child in sorted(path.rglob("*")):
                if child.is_file():
                    digest.update(child.relative_to(path)
                                  .as_posix().encode("utf-8"))
                    digest.update(child.read_bytes())
        else:
            digest.update(b"<missing>")
    if spec.slice_fn is not None:
        digest.update(b"\nslice:" + str(spec.slice_fn(project)).encode("utf-8"))
    elif spec.scope_fields:
        values = scope_values(project)
        slice_data = {name: values.get(name, "") for name in spec.scope_fields}
        digest.update(b"\nslice:" + json.dumps(
            slice_data, sort_keys=True).encode("utf-8"))
    return digest.hexdigest()[:24]


def record_path_for(project_dir, name: str) -> Path:
    return Path(project_dir) / ".weaverbird" / WORKERS_DIR / f"{name}.json"


def load_record(project_dir, name: str) -> dict | None:
    path = record_path_for(project_dir, name)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def run_worker(name: str, project_dir, reporter=None, settings_path=None,
               attempt: int = 0, issues=(), job_id: str | None = None,
               params: dict | None = None) -> dict:
    """Run one worker through its adapter and write its run
    record. Returns the WorkerResult (the record is on disk
    either way). Adapter exceptions become a failed result with
    an issue owned by the worker itself — a worker never takes
    the orchestrator down with it."""
    spec = WORKERS.get(name)
    if spec is None:
        raise WorkerError(f"Unknown worker {name!r}. "
                          f"Known: {', '.join(sorted(WORKERS))}.")
    project = Path(project_dir)
    ctx = WorkerContext(
        project_dir=project, reporter=reporter,
        settings_path=settings_path,
        inputs_hash=inputs_hash(project, spec), attempt=attempt,
        issues=[dict(i) for i in issues], job_id=job_id,
        params=dict(params or {}))
    try:
        outcome = spec.run(project, ctx)
    except Exception as exc:  # a worker fails as a result, never a crash
        outcome = result(
            False, f"{spec.title} failed: {type(exc).__name__}: {exc}",
            issues=[issue("worker_error",
                          f"{type(exc).__name__}: {exc}", name)])
    record = {
        "worker": name,
        "ran_at": _now(),
        "inputs_hash": ctx.inputs_hash,
        "ok": outcome["ok"],
        "artifacts": outcome["artifacts"],
        "metrics": outcome["metrics"],
        "issues": outcome["issues"],
        "summary": outcome["summary"],
        "attempt": attempt,
    }
    if job_id:
        record["job_id"] = job_id
    path = record_path_for(project, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    return outcome


# ---------------------------------------------------------------------------
# The adapters — one per existing step. Each calls the same entry
# point the CLI/app call and translates its report file.

def _run_scope(project, ctx):
    from . import scope as scope_mod
    scope_file = scope_mod.scope_path_for(project)
    if not scope_file.exists():
        return result(False, f"No scope file found at {scope_file}.",
                      issues=[issue("missing_scope",
                                    f"no scope file at {scope_file}",
                                    "scope")])
    parsed = scope_mod.load_scope(scope_file)
    if not parsed.is_complete:
        missing = ", ".join(f.name for f in parsed.missing_fields)
        return result(False,
                      f"Scope incomplete — still missing: {missing}.",
                      artifacts=["SCOPE.md"],
                      issues=[issue("scope_incomplete",
                                    f"missing: {missing}", "scope")])
    if not parsed.is_approved:
        return result(False,
                      "Scope is complete but not approved yet.",
                      artifacts=["SCOPE.md"],
                      issues=[issue("scope_unapproved",
                                    "the scope is complete but not "
                                    "approved", "scope")])
    return result(True,
                  "Scope complete and approved — its gate passes.",
                  artifacts=["SCOPE.md"],
                  metrics={"filled_fields": len(parsed.filled_fields)})


def _run_skeleton(project, ctx):
    from . import skeleton as skeleton_module
    try:
        report = skeleton_module.build_skeleton(
            project, reporter=ctx.reporter)
    except skeleton_module.SkeletonError as exc:
        return result(False, str(exc),
                      issues=[issue("skeleton_failed", str(exc), "skeleton")])
    return result(True,
                  f"Skeleton built and validated "
                  f"({report['validation'].get('godot_version', '')}).",
                  artifacts=["game/project.godot",
                             ".weaverbird/skeleton_report.json"],
                  metrics={"files": len(report.get("files", []))})


def _build_ctx(project, ctx):
    """The build context for the generation workers: the world
    worker's persisted copy when one exists (so mechanics,
    assembly and retries consume exactly what the world worker
    derived — a retry differs only by its attempt number, e.g.
    the world worker's shifted platformer seed), else a fresh
    prepare.

    The persisted copy is a cache keyed by the scope values it
    was derived from AND the factory version that derived it:
    when the scope's values on disk differ from the saved ones
    — a hand-edit to SCOPE.md is the standing case — or the
    saved context was derived by a different Weaverbird than
    the one now running, the context is re-prepared from the
    current scope and re-saved, so a re-run of any generation
    worker derives from the scope as it now reads, with the
    derivation as it now works, never from the first parse."""
    from . import generate as generate_module
    saved = generate_module.load_build_context(project)
    if generate_module.build_context_current(saved, scope_values(project)):
        return saved
    fresh = generate_module.prepare_build(
        project, settings_path=ctx.settings_path,
        reporter=ctx.reporter)
    generate_module.save_build_context(project, fresh)
    return fresh


def _run_world(project, ctx):
    from . import generate as generate_module
    from . import world as world_module
    try:
        build = _build_ctx(project, ctx)
        world_data, issues = generate_module.run_world_step(
            project, build, attempt=ctx.attempt)
    except generate_module.GenerationError as exc:
        return result(False, str(exc),
                      issues=[issue("world_failed", str(exc), "world")])
    if issues:
        return result(False,
                      "World gate failed: "
                      + "; ".join(i["detail"] for i in issues),
                      artifacts=[world_module.WORLD_REL], issues=issues)
    manifest = world_module.world_manifest(world_data)
    plan_line = ""
    if manifest.get("movement"):
        plan_line = (f"Plan: {manifest['movement']} / "
                     f"{manifest.get('objective', '')} — ")
    return result(True,
                  plan_line
                  + f"World written: {manifest['units']} unit(s), "
                  f"entity kinds {', '.join(manifest['entity_kinds'])}; "
                  "its gate passes.",
                  artifacts=[world_module.WORLD_REL],
                  metrics={"units": manifest["units"],
                           "entity_kinds": len(manifest["entity_kinds"])})


def _run_mechanics(project, ctx):
    from . import generate as generate_module
    from . import mechanics as mechanics_module
    from . import world as world_module
    build = _build_ctx(project, ctx)
    if build.get("bespoke"):
        module = project / mechanics_module.BESPOKE_MODULE_REL
        if not module.exists():
            try:
                mechanics_module.write_bespoke_module(
                    project, settings_path=ctx.settings_path,
                    brain_fn=ctx.params.get("brain_fn"),
                    reporter=ctx.reporter)
            except mechanics_module.MechanicsError as exc:
                return result(False, str(exc),
                              issues=[issue("bespoke_blocked",
                                            str(exc), "mechanics")])
    world_data = world_module.load_world(project)
    if world_data is None:
        return result(False,
                      "No world data found — the world worker must "
                      "run before mechanics.",
                      issues=[issue("missing_world",
                                    "game/world/world.json is missing",
                                    "world")])
    world_issues = world_module.validate_world(world_data)
    if world_issues:
        return result(False,
                      "The world data on disk fails the world "
                      "gate: " + "; ".join(i["detail"]
                                            for i in world_issues),
                      issues=world_issues)
    try:
        written, issues = generate_module.run_mechanics_step(
            project, build, world_data)
    except mechanics_module.MechanicsError as exc:
        return result(False, str(exc),
                      issues=[issue("mechanics_failed", str(exc),
                                    "mechanics")])
    if issues:
        return result(False,
                      "Mechanics gate failed: "
                      + "; ".join(i["detail"] for i in issues),
                      artifacts=written, issues=issues)
    return result(True,
                  f"Mechanics written: {len(written)} script(s) "
                  "interpreting the world data.",
                  artifacts=written,
                  metrics={"files": len(written)})


def _run_assets(project, ctx):
    from . import assets_gen as assets_module
    try:
        report = assets_module.generate_assets(
            project, reporter=ctx.reporter)
    except assets_module.AssetsError as exc:
        return result(False, str(exc),
                      issues=[issue("assets_failed", str(exc), "assets")])
    requests = report.get("requests", {})
    based_on = requests.get("based_on", "the plan")
    return result(True,
                  f"Assets generated: {len(report['files'])} files "
                  f"(requests from {based_on}), every one in the "
                  "provenance log.",
                  artifacts=[".weaverbird/assets_report.json"],
                  metrics={"files": len(report["files"])})


def _run_assembly(project, ctx):
    from . import generate as generate_module
    from . import mechanics as mechanics_module
    from . import world as world_module
    build = _build_ctx(project, ctx)
    world_data = world_module.load_world(project)
    if world_data is None:
        return result(False,
                      "No world data found — the world worker must "
                      "run before assembly.",
                      issues=[issue("missing_world",
                                    "game/world/world.json is missing",
                                    "world")])
    world_issues = world_module.validate_world(world_data)
    if world_issues:
        return result(False,
                      "The world data on disk fails the world "
                      "gate: " + "; ".join(i["detail"]
                                            for i in world_issues),
                      issues=world_issues)
    written = mechanics_module.template_files(build)
    files = [f"game/{rel}" for rel in sorted(written)]
    if build.get("bespoke"):
        files.append(mechanics_module.BESPOKE_MODULE_REL)
    try:
        report = generate_module.run_assembly_step(
            project, build, world_data, files,
            reporter=ctx.reporter)
    except generate_module.GenerationError as exc:
        return result(False, str(exc),
                      artifacts=[".weaverbird/generation_report.json"],
                      issues=[issue("validation_failed", str(exc),
                                    "assembly")])
    return result(True,
                  f"Game assembled: {report['units_delivered']} "
                  "content unit(s), Godot validation passed, "
                  "tagged known-good/build.",
                  artifacts=["game/game_config.json",
                             ".weaverbird/generation_report.json"],
                  metrics={"units": report["units_delivered"]})


def _run_testing(project, ctx):
    from . import testing as testing_module
    try:
        report = testing_module.run_tests(
            project, settings_path=ctx.settings_path,
            reporter=ctx.reporter)
    except testing_module.TestingError as exc:
        return result(False, str(exc),
                      issues=[issue("testing_failed", str(exc), "testing")])
    ok = bool(report.get("ok"))
    issues = [] if ok else [issue(
        "tests_failed",
        "the headless suite failed — see .weaverbird/test_report.json",
        "testing")]
    return result(ok,
                  "Test suite: " + ("PASS — win and lose proven."
                                     if ok else "FAIL."),
                  artifacts=[".weaverbird/test_report.json"],
                  metrics={"runs": len(report.get("runs", [])),
                           "units": len(report.get("units", []))},
                  issues=issues)


def _run_model_playtest(project, ctx):
    from . import modelplayer as modelplayer_module
    try:
        record = modelplayer_module.run_model_playtest(
            project, settings_path=ctx.settings_path,
            reporter=ctx.reporter, brain_fn=ctx.params.get("brain_fn"))
    except modelplayer_module.ModelPlayerError as exc:
        return result(False, str(exc),
                      issues=[issue("playtest_failed", str(exc),
                                    "model-playtest")])
    return result(True,
                  f"Model playtest: {record['outcome']} — "
                  f"{record['decisions']} decisions, "
                  f"{record['deaths']} deaths.",
                  artifacts=[".weaverbird/model_playtest.json"],
                  metrics={"decisions": record["decisions"],
                           "deaths": record["deaths"]})


def _run_balance(project, ctx):
    """Analysis only: read the test report's per-unit metrics,
    flag and suggest. Applying a suggestion stays with the
    patch worker (through the patch gates), by design."""
    from . import balance as balance_module
    try:
        units = balance_module.load_units(project)
        flags = balance_module.analyse(units)
        suggestions = balance_module.suggestions(project) if units else []
    except balance_module.BalanceError as exc:
        return result(False, str(exc),
                      issues=[issue("balance_failed", str(exc), "balance")])
    return result(True,
                  f"Balance analysis: {len(units)} unit record(s), "
                  f"{len(flags)} flag(s), {len(suggestions)} "
                  "suggestion(s) — applying one goes through the "
                  "patch worker.",
                  artifacts=[".weaverbird/test_report.json"],
                  metrics={"units": len(units), "flags": len(flags),
                           "suggestions": len(suggestions)})


def _run_repair(project, ctx):
    from . import repair as repair_module
    try:
        outcome = repair_module.run_repair(
            project, settings_path=ctx.settings_path,
            reporter=ctx.reporter)
    except repair_module.RepairError as exc:
        return result(False, str(exc),
                      issues=[issue("repair_failed", str(exc), "repair")])
    ok = outcome.get("outcome") in ("repaired", "already_passing")
    return result(ok, f"Self-repair: {outcome['outcome']} — "
                  f"{outcome['note']}",
                  artifacts=["REPAIR_REPORT.md"],
                  metrics={"attempts": len(outcome.get("attempts", []))},
                  issues=[] if ok else [issue(
                      "repair_exhausted", outcome["note"], "repair")])


def _run_feedback(project, ctx):
    from . import feedback as feedback_module
    fetch = feedback_module.fetch_feedback(project)
    triage = feedback_module.triage(
        project, settings_path=ctx.settings_path)
    added = sum(e.get("added", 0) for e in fetch.values())
    return result(True,
                  f"Feedback fetched (+{added} new) and triaged: "
                  f"{triage['note']}",
                  artifacts=[".weaverbird/feedback.json"],
                  metrics={"added": added,
                           "queued": len(triage.get("items", []))})


def _run_patch(project, ctx):
    from . import patch as patch_module
    item_id = str(ctx.params.get("item", ""))
    if not item_id:
        return result(False,
                      "The patch worker needs a feedback item id "
                      "(params['item']).",
                      issues=[issue("missing_item",
                                    "no feedback item named", "patch")])
    try:
        outcome = patch_module.start_patch(
            project, item_id, settings_path=ctx.settings_path,
            reporter=ctx.reporter)
    except patch_module.PatchError as exc:
        return result(False, str(exc),
                      issues=[issue("patch_failed", str(exc), "patch")])
    ok = outcome.get("outcome") in ("patched", "planned")
    return result(ok, f"Patch: {outcome['outcome']} — "
                  f"{outcome.get('change') or outcome.get('reason', '')}",
                  artifacts=["game/VERSION", "CHANGELOG.md"],
                  metrics={"outcome": outcome.get("outcome", "")},
                  issues=[] if ok else [issue(
                      "patch_rejected", outcome.get("reason", ""), "patch")])


def _run_finish(project, ctx):
    from . import finish as finish_module
    evaluation = finish_module.evaluate_finish(project)
    path = finish_module.build_finish_report(
        project, settings_path=ctx.settings_path)
    open_items = evaluation.get("open", {})
    ok = not open_items
    issues = [issue("dod_open", f"{key}: {reason}", "finish")
              for key, reason in open_items.items()]
    marked = list(evaluation.get("marked_now", {})) + \
        list(evaluation.get("already_marked", {}))
    return result(ok,
                  f"Finish check: {len(marked)}/7 definition-of-done "
                  f"items evidenced; report at {path.name}."
                  + ("" if ok else " Still open: "
                     + ", ".join(open_items) + "."),
                  artifacts=["FINISH_REPORT.md",
                             ".weaverbird/state.json"],
                  metrics={"marked": len(marked),
                           "open": len(open_items)},
                  issues=issues)


def _run_export(project, ctx):
    from . import exports as exports_module
    from . import state as state_module
    try:
        stage = state_module.load_state(project).get("stage")
    except Exception:
        stage = None
    if stage != "finished":
        return result(
            False,
            "Exports run on a finished project — this one is at "
            f"stage {stage or 'unknown'}, not finished.",
            issues=[issue("not_finished",
                          "the project's stage is "
                          f"{stage or 'unknown'}, not finished",
                          "finish")])
    targets = ctx.params.get("targets") or None
    try:
        report = exports_module.run_exports(
            project, targets=targets, reporter=ctx.reporter)
    except exports_module.ExportsError as exc:
        return result(False, str(exc),
                      issues=[issue("export_failed", str(exc), "export")])
    exported = [r for r in report["targets"] if r["status"] == "exported"]
    blocked = [r for r in report["targets"] if r["status"] == "blocked"]
    bits = []
    if exported:
        bits.append("exported " + ", ".join(
            f"{r['target']} ({r['size']} bytes)" for r in exported))
    if blocked:
        bits.append("blocked " + ", ".join(
            f"{r['target']} — {r['reason']}" for r in blocked))
    return result(
        True,
        "Exports: " + ("; ".join(bits) if bits else "nothing requested")
        + ". Report at EXPORT_REPORT.md.",
        artifacts=["EXPORT_REPORT.md", "exports/EXPORTS.json"]
                  + [r["artifact"] for r in exported],
        metrics={"exported": len(exported), "blocked": len(blocked),
                 **{f"target_{r['target']}": r["status"]
                    for r in report["targets"]}})


# ---------------------------------------------------------------------------
# The registry

WORKERS: dict[str, WorkerSpec] = {}


def _register(spec: WorkerSpec) -> None:
    WORKERS[spec.name] = spec


_register(WorkerSpec(
    name="scope", title="Scope gate",
    # Its input is the scope file itself, hashed by content
    # like every declared input: ANY edit to SCOPE.md — a
    # changed value, not just a newly filled field — re-runs
    # the gate, and the downstream workers follow through their
    # own declared slices (the field values each one reads) and
    # the build context, which is keyed to the scope values it
    # was derived from (see _build_ctx). An unchanged scope
    # still skips: the scope step never writes SCOPE.md, so its
    # own run cannot invalidate it.
    inputs=("SCOPE.md",), outputs=("SCOPE.md",),
    depends_on=(), run=_run_scope))
_register(WorkerSpec(
    name="skeleton", title="Skeleton builder",
    inputs=(), outputs=("game/project.godot",
                        ".weaverbird/skeleton_report.json"),
    depends_on=("scope",), run=_run_skeleton,
    scope_fields=("Title (working)", "One-line pitch",
                  "Session length (one sitting)",
                  "Content counts (levels / waves / rooms / items, as applicable)")))
_register(WorkerSpec(
    name="world", title="World worker",
    inputs=("game/project.godot",),
    outputs=("game/world/world.json",),
    depends_on=("skeleton",), run=_run_world,
    scope_fields=("Title (working)", "Dimension", "Genre / archetype",
                  "Core verb (what the player does most)",
                  "Win condition", "Lose / fail condition",
                  "One-line pitch", "Must-have features (max 3)",
                  "Session length (one sitting)",
                  "Difficulty target (who it is for, how hard)",
                  "Content counts (levels / waves / rooms / items, as applicable)"),
    self_corrects=True))
_register(WorkerSpec(
    name="mechanics", title="Mechanics worker",
    inputs=("game/world/world.json",),
    outputs=("game/scripts/game.gd",),
    depends_on=("world",), run=_run_mechanics,
    scope_fields=("Dimension", "Genre / archetype",
                  "Core verb (what the player does most)",
                  "Win condition", "Lose / fail condition")))
_register(WorkerSpec(
    name="assets", title="Asset generation",
    inputs=("game/project.godot", "game/world/world.json"),
    outputs=("game/assets/", ".weaverbird/assets_report.json",
             "game/icon.svg"),
    depends_on=("skeleton",), run=_run_assets,
    scope_fields=("Palette",
                  "Style sample chosen (id / link to approved sample)",
                  "Audio mood (music + SFX character)")))
_register(WorkerSpec(
    name="assembly", title="Assembly worker",
    inputs=("game/world/world.json", "game/scripts/game.gd",
            ".weaverbird/assets_report.json"),
    outputs=("game/game_config.json",
             ".weaverbird/generation_report.json"),
    depends_on=("world", "mechanics", "assets"), run=_run_assembly,
    scope_fields=("Palette",)))
_register(WorkerSpec(
    name="testing", title="Headless test stage",
    inputs=("game/game_config.json", "game/world/world.json",
            "game/scripts/game.gd", ".weaverbird/assets_report.json"),
    outputs=(".weaverbird/test_report.json",),
    depends_on=("assembly",), run=_run_testing))
_register(WorkerSpec(
    name="model-playtest", title="Model playtester",
    inputs=(".weaverbird/test_report.json",),
    outputs=(".weaverbird/model_playtest.json",),
    depends_on=("testing",), run=_run_model_playtest, can_skip=False))
_register(WorkerSpec(
    name="balance", title="Balance lab (analysis)",
    inputs=(".weaverbird/test_report.json", "game/game_config.json"),
    outputs=(".weaverbird/balance_report.json",),
    depends_on=("testing",), run=_run_balance))
_register(WorkerSpec(
    name="repair", title="Bounded self-repair",
    inputs=(".weaverbird/test_report.json",),
    outputs=("REPAIR_REPORT.md",),
    depends_on=("testing",), run=_run_repair, can_skip=False))
_register(WorkerSpec(
    name="feedback", title="Feedback fetch + triage",
    inputs=("feedback-inbox",),
    outputs=(".weaverbird/feedback.json",),
    depends_on=(), run=_run_feedback, can_skip=False))
_register(WorkerSpec(
    name="patch", title="Patch loop",
    inputs=(".weaverbird/feedback.json",),
    outputs=("game/VERSION", "CHANGELOG.md"),
    depends_on=("feedback", "testing"), run=_run_patch,
    can_skip=False))
_register(WorkerSpec(
    name="finish", title="Finish check + report",
    inputs=(".weaverbird/test_report.json",
            ".weaverbird/generation_report.json",
            ".weaverbird/assets_report.json"),
    outputs=("FINISH_REPORT.md",),
    depends_on=("testing",), run=_run_finish))
_register(WorkerSpec(
    name="export", title="Export worker",
    inputs=("game/", "FINISH_REPORT.md"),
    outputs=("exports/", "EXPORT_REPORT.md"),
    depends_on=("finish",), run=_run_export, can_skip=False))


def worker_names() -> list[str]:
    return list(WORKERS)


# ---------------------------------------------------------------------------
# Pipeline wiring: stage handlers delegate to worker adapters, so
# `pipeline advance`, the app and the orchestrator can never
# diverge — there is exactly one implementation of each step.

def _handler_for(worker_name: str):
    def handler(project_dir, state=None) -> bool:
        outcome = run_worker(worker_name, project_dir)
        if not outcome["ok"]:
            from . import state as state_module
            raise state_module.PipelineError(
                f"The {worker_name} worker did not pass: "
                f"{outcome['summary']}")
        return True
    return handler


def register_stage_handlers() -> None:
    """Register the pipeline's stage handlers as worker runs:
    leaving scope/skeleton runs the skeleton worker, leaving
    assets runs the assets worker, leaving build runs world +
    mechanics + assembly (in dependency order), leaving
    headless_test/playtest runs the testing worker."""
    from . import state as state_module

    def build_handler(project_dir, state=None) -> bool:
        for name in ("world", "mechanics", "assembly"):
            outcome = run_worker(name, project_dir)
            if not outcome["ok"]:
                raise state_module.PipelineError(
                    f"The {name} worker did not pass: "
                    f"{outcome['summary']}")
        return True

    state_module.register_handler("scope", _handler_for("skeleton"))
    state_module.register_handler("skeleton", _handler_for("skeleton"))
    state_module.register_handler("assets", _handler_for("assets"))
    state_module.register_handler("build", build_handler)
    state_module.register_handler("headless_test", _handler_for("testing"))
    state_module.register_handler("playtest", _handler_for("testing"))
