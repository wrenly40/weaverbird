"""The orchestrator — the agent that coordinates the workers.

The workers (workers.py) each own one step of making a game and
know nothing about each other. The orchestrator owns the *plan*:
which workers this project needs, in what order, and what to do
when one fails. It has no creative logic and writes no game
files itself — it dispatches workers, checks their gates, routes
failures back to the owning worker, and keeps the pipeline state
truthful for the app.

Plans (.weaverbird/plan.json):

- `plan(project_dir, target)` produces the ordered DAG of steps
  from the project's current state toward the target (default
  "finished"): scope → skeleton → world → mechanics → assets →
  assembly → testing → finish, with model-playtest available as
  an optional step. Reactive plans (feedback, patch, balance,
  repair) are built when invoked (`--reactive NAME`).
- Each step records its worker, the reason it is in the plan,
  expected inputs/outputs, its status, and whether it may be
  skipped when its inputs are unchanged.

Running a plan — through the job runner from the app (action
"orchestrate"), or synchronously from the CLI
(`weaverbird orchestrate <dir> [--to STAGE] [--force]`):

- For each step: if a worker run record exists whose inputs
  hash matches the current one, says ok, and skipping is
  allowed, the step is recorded as *skipped (inputs unchanged)*
  and the worker does not run. Touching an input un-skips
  exactly the workers that declare it — and their dependents,
  whose own inputs changed as a consequence.
- On a gate failure, the issues are routed to their owning
  worker: ONE bounded retry of that worker with the issue
  digest in its context. Workers that can self-correct use it
  (the world worker regenerates a failing platformer unit with
  a different seed); the rest simply re-run once, honestly.
  Still failing → the plan stops and the plan report escalates
  with the owner and the issues.
- A step whose worker is known-blocked never starts: bespoke
  mechanics without a usable brain is the standing example —
  the plan stops at the plan, with the plain explanation.
- Brain-using workers spend through brain.py's existing caps;
  the orchestrator adds no budget of its own.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from . import state as state_module
from . import workers as workers_module

PLAN_NAME = "plan.json"

# The build chain, in dependency order, and the pipeline stage
# each completed worker lets the state machine claim.
CHAIN = ["scope", "skeleton", "world", "mechanics", "assets",
         "assembly", "testing", "finish"]
WORKER_STAGE = {
    "scope": "scope",
    "skeleton": "skeleton",
    "world": "build",
    "mechanics": "build",
    "assets": "assets",
    "assembly": "build",
    "testing": "playtest",
    "finish": "finished",
}
TARGETS = {
    "skeleton": ["scope", "skeleton"],
    "world": ["scope", "skeleton", "world"],
    "mechanics": ["scope", "skeleton", "world", "mechanics"],
    "assets": ["scope", "skeleton", "assets"],
    "assembly": ["scope", "skeleton", "world", "mechanics",
                 "assets", "assembly"],
    "testing": ["scope", "skeleton", "world", "mechanics",
                "assets", "assembly", "testing"],
    "playtest": ["scope", "skeleton", "world", "mechanics",
                 "assets", "assembly", "testing"],
    "finished": CHAIN,
}
REACTIVE = {
    "feedback": ["feedback"],
    "patch": ["feedback", "patch"],
    "balance": ["balance"],
    "repair": ["repair"],
    "model-playtest": ["model-playtest"],
}

_REASONS = {
    "scope": "the build does not start until the scope is complete "
             "and approved",
    "skeleton": "the grey-box shell every later worker builds on",
    "world": "the world's data — layouts, rounds, puzzles — as "
             "game/world/world.json",
    "mechanics": "the player + game logic scripts that interpret "
                 "the world data",
    "assets": "sprites, icon and audio from the plan + "
              "world requests and the style-lock palette",
    "assembly": "wire shell + mechanics + world + assets into the "
                "runnable game and validate it in Godot",
    "testing": "the headless suite with the scripted players",
    "model-playtest": "the model-driven playtester (optional, "
                      "needs a vision-capable brain tier)",
    "finish": "mark the definition of done from the evidence and "
              "write the finish report",
    "feedback": "fetch player feedback and triage the queue",
    "patch": "ship an approved feedback item through the patch "
             "gates",
    "balance": "analyse the test report's per-unit metrics",
    "repair": "bounded self-repair of a failing test run",
}


class OrchestratorError(Exception):
    """Raised when a plan cannot be built or run at all."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def plan_path_for(project_dir) -> Path:
    return Path(project_dir) / ".weaverbird" / PLAN_NAME


def load_plan(project_dir) -> dict | None:
    path = plan_path_for(project_dir)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _save_plan(project: Path, plan: dict) -> None:
    path = plan_path_for(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(plan, indent=2) + "\n", encoding="utf-8")


def _step_status(project: Path, name: str, force: bool) -> str:
    record = workers_module.load_record(project, name)
    if record is None:
        return "pending"
    if not record.get("ok"):
        return "failed"
    spec = workers_module.WORKERS[name]
    if force or not spec.can_skip:
        return "done"
    current = workers_module.inputs_hash(project, spec)
    return "done" if record.get("inputs_hash") == current else "stale"


def plan(project_dir, target: str = "finished",
         reactive: str | None = None, force: bool = False,
         include_model_playtest: bool = False,
         persist: bool = True) -> dict:
    """Build (and persist) the plan for a project: the ordered
    worker steps from its current state to the target, each with
    its status from the run records."""
    project = Path(project_dir)
    if reactive is not None:
        if reactive not in REACTIVE:
            raise OrchestratorError(
                f"Unknown reactive plan {reactive!r}. "
                f"Known: {', '.join(sorted(REACTIVE))}.")
        names = list(REACTIVE[reactive])
        plan_target = reactive
    else:
        if target not in TARGETS:
            raise OrchestratorError(
                f"Unknown target {target!r}. "
                f"Known: {', '.join(sorted(TARGETS))}.")
        names = list(TARGETS[target])
        if include_model_playtest and "testing" in names:
            names.insert(names.index("testing") + 1, "model-playtest")
        plan_target = target
    steps = []
    for name in names:
        spec = workers_module.WORKERS[name]
        steps.append({
            "worker": name,
            "title": spec.title,
            "reason": _REASONS.get(name, ""),
            "inputs": list(spec.inputs),
            "outputs": list(spec.outputs),
            "depends_on": list(spec.depends_on),
            "skip_if_inputs_unchanged": bool(spec.can_skip),
            "status": _step_status(project, name, force),
        })
    try:
        stage = state_module.load_state(project)["stage"]
    except state_module.PipelineError:
        stage = None
    data = {
        "target": plan_target,
        "built_at": _now(),
        "stage": stage,
        "steps": steps,
        "events": [],
        "outcome": None,
        "escalation": None,
    }
    if persist:
        _save_plan(project, data)
    return data


def _params_carry_brain_fn(params: dict | None) -> bool:
    """An injected brain function unblocks the bespoke path —
    at the top level of the run params or nested under a
    worker's own params (the mechanics step carries it)."""
    if not params:
        return False
    if params.get("brain_fn") is not None:
        return True
    return any(isinstance(v, dict) and v.get("brain_fn") is not None
               for v in params.values())


def blocked_reason(project_dir, name: str,
                   settings_path=None, params: dict | None = None) -> str | None:
    """Why a worker is known-blocked before it starts, or None.
    The standing case: the scope's plan requires bespoke
    mechanics and no usable brain is configured — the plan
    stops at the world step (before any game content exists)
    with the plain explanation, instead of burning a run or
    quietly building a different game. An injected brain
    function (the test seam, or an embedder's own writer)
    unblocks it."""
    project = Path(project_dir)
    if name not in ("world", "mechanics"):
        return None
    if _params_carry_brain_fn(params):
        return None
    from . import generate as generate_module
    from . import mechanics as mechanics_module
    module = project / mechanics_module.BESPOKE_MODULE_REL
    if module.exists():
        return None
    values = workers_module.scope_values(project)
    why = generate_module.bespoke_reason(values) if values else None
    if not why:
        return None
    from . import brain as brain_module
    _, data = brain_module.resolve_settings(settings_path, project)
    for tier in ("frontier", "mid"):
        status = brain_module.tier_status(data, tier) if data else None
        if status and status["usable"]:
            return None
    return (f"This game's core mechanic needs bespoke work — "
            f"{why} — and no usable brain (frontier or mid tier) "
            "is configured to write the mechanics module. "
            "Configure one with 'weaverbird brain status' and "
            "re-run the plan, or change the scope so its "
            "mechanic fits the vocabulary. No substitute game "
            "has been built.")


def _sync_state(project: Path, worker_name: str, reporter=None) -> None:
    """Move the pipeline state forward to the stage a completed
    worker entitles it to — never backwards, one honest history
    entry. The workers ran the real gates; this keeps the app's
    stepper truthful without re-running them through advance()."""
    target_stage = WORKER_STAGE.get(worker_name)
    if target_stage is None:
        return
    try:
        state = state_module.load_state(project)
    except state_module.PipelineError:
        return
    stages = state_module.STAGES
    if stages.index(state["stage"]) >= stages.index(target_stage):
        return
    # The definition-of-done gate for "finished" stays the state
    # machine's own: only claim it when the checklist is true.
    if target_stage == "finished":
        dod = state.get("definition_of_done", {})
        if not all(dod.get(k) for k in state_module.DEFINITION_OF_DONE):
            return
    state["stage"] = target_stage
    state["history"].append({
        "stage": target_stage, "at": _now(),
        "note": f"Orchestrator: the {worker_name} worker completed."})
    state_module.save_state(project, state)
    if reporter is not None:
        reporter(f"Pipeline stage: {target_stage}")


def run(project_dir, target: str = "finished",
        reactive: str | None = None, force: bool = False,
        settings_path=None, reporter=None, job_id: str | None = None,
        params: dict | None = None,
        include_model_playtest: bool = False) -> dict:
    """Execute a plan. Returns the finished plan dict (also
    persisted), with per-step statuses and, on failure, an
    escalation naming the owning worker and its issues."""
    project = Path(project_dir)
    data = plan(project, target=target, reactive=reactive,
                force=force,
                include_model_playtest=include_model_playtest)
    params = dict(params or {})

    def event(text: str) -> None:
        data["events"].append({"at": _now(), "text": text})
        _save_plan(project, data)
        if reporter is not None:
            reporter(text)

    for step in data["steps"]:
        name = step["worker"]
        spec = workers_module.WORKERS[name]
        step_params = params.get(name) \
            if isinstance(params.get(name), dict) else params
        blocked = blocked_reason(project, name,
                                 settings_path=settings_path,
                                 params=step_params)
        if blocked:
            step["status"] = "blocked"
            data["outcome"] = "blocked"
            data["escalation"] = {"worker": name, "issues": [
                {"kind": "blocked", "detail": blocked,
                 "owner": name}]}
            event(f"Plan stopped before {name}: {blocked}")
            _save_plan(project, data)
            return data
        record = workers_module.load_record(project, name)
        current_hash = workers_module.inputs_hash(project, spec)
        if (not force and spec.can_skip and record is not None
                and record.get("ok")
                and record.get("inputs_hash") == current_hash):
            step["status"] = "skipped"
            step["note"] = "inputs unchanged"
            event(f"Skipped {name} — inputs unchanged")
            _sync_state(project, name)
            continue
        event(f"Running {name} — {step['reason']}")
        outcome = workers_module.run_worker(
            name, project, reporter=reporter,
            settings_path=settings_path, job_id=job_id,
            params=params.get(name) if isinstance(params.get(name), dict)
            else params)
        if not outcome["ok"] and outcome["issues"]:
            owners = {i.get("owner") for i in outcome["issues"]}
            retry_owner = name if name in owners else \
                next(iter(owners), name)
            if retry_owner in workers_module.WORKERS:
                event(f"{name} failed — routing its issues to "
                      f"{retry_owner} for one bounded retry")
                outcome = workers_module.run_worker(
                    retry_owner, project, reporter=reporter,
                    settings_path=settings_path, attempt=1,
                    issues=outcome["issues"], job_id=job_id,
                    params=params.get(retry_owner)
                    if isinstance(params.get(retry_owner), dict)
                    else params)
                if retry_owner != name:
                    step["retried_by"] = retry_owner
                    if outcome["ok"]:
                        # The owner fixed its side; the step that
                        # surfaced the failure runs once more.
                        outcome = workers_module.run_worker(
                            name, project, reporter=reporter,
                            settings_path=settings_path,
                            job_id=job_id,
                            params=params.get(name)
                            if isinstance(params.get(name), dict)
                            else params)
        if not outcome["ok"]:
            step["status"] = "failed"
            step["summary"] = outcome["summary"]
            owner = outcome["issues"][0]["owner"] \
                if outcome["issues"] else name
            data["outcome"] = "failed"
            data["escalation"] = {"worker": owner,
                                  "issues": outcome["issues"]}
            event(f"Plan stopped at {name} — owner {owner}: "
                  f"{outcome['summary']}")
            _save_plan(project, data)
            return data
        step["status"] = "done"
        step["summary"] = outcome["summary"]
        event(f"Done {name} — {outcome['summary']}")
        _sync_state(project, name, reporter=reporter)
    data["outcome"] = "done"
    event(f"Plan complete — target {data['target']} reached")
    _save_plan(project, data)
    return data
