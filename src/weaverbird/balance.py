"""The balance lab — per-unit difficulty evidence, deterministic
flags, and suggestions that ship only through the patch gates.

The test stage's scripted players record per-unit stats (levels,
rounds, puzzles) into ``.weaverbird/test_report.json`` under
``"units"``. The lab reads them and does three things:

1. **Show** — a per-unit table (completion, deaths, time, moves
   against budget) plus the model playtest's summary when one is
   on record.
2. **Flag** — deterministic rules over the numbers, each flag
   carrying the numbers behind it:
   - *difficulty spike*: a completed unit whose competent deaths
     are at least twice the median deaths of completed units
     (and at least 2);
   - *wall*: a unit the competent player could not complete;
   - *first-time stall*: the first-time player spent more than
     twice the median completed-unit time on one unit, or died
     3+ times on it;
   - *breeze*: every competent unit completed with zero deaths
     in total and the first-time player finished the game too —
     the game may be too easy. (The flag lists the actual times;
     "fast" is judged against the first-time player's own run,
     not an invented par time.)
3. **Suggest / apply / compare** — suggestions are concrete
   config changes built ONLY from the patch loop's bounded nudge
   table (the same clamps repair uses): spikes and walls ease,
   a breeze hardens, a stall tied to the objective hint proposes
   the confusion path's clearer hint. Applying a suggestion goes
   through patch.py as a synthetic feedback item (source
   "balance-lab") with the full gates — test stage plus
   save-compatibility — a version bump and a changelog entry
   citing the lab numbers. There is no side door. Comparing runs
   an A/B: baseline metrics (the existing test report when it is
   fresher than the config, else a fresh test run), candidate
   config, test stage, then the project is restored byte-for-byte
   (config plus the whole factory-state directory), with the
   side-by-side verdict in ``.weaverbird/balance_report.json``.

Honest limits: per-unit metrics exist only for units the bots
record — competent and first-time runs of the current build.
Reports written by older builds have no units; the lab says so
instead of inventing numbers. A compare costs up to two full
test-stage runs.

Archetype independence: the lab never branches on a game's
kind. Metrics are read from the test report's units data and
shown from the fields each record carries — a metric a game
does not record is shown as unavailable. The one place a game
kind is read at all is the value stored in the game's own
config, passed to patch.py's pre-existing bounded-knob tables
when a suggestion is computed; Pass 5's mechanics layer will
replace those tables, and the lab's interface will not change.
"""

from __future__ import annotations

import hashlib
import json
import statistics
from datetime import datetime, timezone
from pathlib import Path

from . import feedback as feedback_module
from . import generate as generate_module
from . import modelplayer as modelplayer_module
from . import patch as patch_module
from . import testing as testing_module

REPORT_NAME = "balance_report.json"
SOURCE = "balance-lab"


class BalanceError(Exception):
    """Raised when a balance operation cannot run."""


def report_path_for(project_dir) -> Path:
    return Path(project_dir) / ".weaverbird" / REPORT_NAME


def load_compare_report(project_dir) -> dict | None:
    path = report_path_for(project_dir)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


# ---------------------------------------------------------------------------
# Reading the evidence

def load_units(project_dir) -> list[dict]:
    """Per-unit records from the latest test report ([] if the
    report predates per-unit recording)."""
    report = testing_module.load_report(project_dir)
    if not report:
        return []
    units = report.get("units")
    return units if isinstance(units, list) else []


def unit_rows(units: list[dict]) -> list[dict]:
    """Units merged by number for display:
    [{unit, competent: rec|None, first_time: rec|None}]."""
    by_unit: dict[int, dict] = {}
    for record in units:
        row = by_unit.setdefault(int(record.get("unit", 0)),
                                 {"unit": int(record.get("unit", 0)),
                                  "competent": None, "first_time": None})
        row[record.get("source", "")] = record
    return [by_unit[key] for key in sorted(by_unit)]


def model_summary(project_dir) -> dict | None:
    """The model playtest's headline numbers, when a run is on
    record — context for the table, not flag input."""
    record = None
    path = modelplayer_module.report_path_for(project_dir)
    if path.exists():
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            record = None
    if not isinstance(record, dict):
        return None
    return {
        "outcome": record.get("outcome"),
        "finished": record.get("finished"),
        "decisions": record.get("decisions"),
        "deaths": record.get("deaths"),
        "time_s": record.get("time_s"),
        "confusion": record.get("confusion_notes") or [],
    }


# ---------------------------------------------------------------------------
# Flags — deterministic rules, numbers attached

def _median(values: list[float]) -> float:
    return float(statistics.median(values)) if values else 0.0


def analyse(units: list[dict]) -> list[dict]:
    """The flags for a units list. Each flag: {kind, unit (None
    for whole-game flags), text, numbers}."""
    flags: list[dict] = []
    comp = {int(r["unit"]): r for r in units
            if r.get("source") == "competent"}
    first = {int(r["unit"]): r for r in units
             if r.get("source") == "first_time"}
    comp_done = [r for r in comp.values() if r.get("completed")]
    comp_deaths = [float(r.get("deaths", 0)) for r in comp_done]
    median_deaths = _median(comp_deaths)

    for unit in sorted(comp):
        record = comp[unit]
        if not record.get("completed"):
            flags.append({
                "kind": "wall", "unit": unit,
                "text": f"Unit {unit} is a wall — the competent "
                        "player could not complete it.",
                "numbers": {"deaths": record.get("deaths", 0),
                            "time_s": record.get("time_s")},
            })
        elif float(record.get("deaths", 0)) >= 2 \
                and float(record.get("deaths", 0)) >= 2 * median_deaths \
                and len(comp_done) >= 2:
            flags.append({
                "kind": "spike", "unit": unit,
                "text": f"Unit {unit} is a difficulty spike — "
                        f"{int(record.get('deaths', 0))} deaths against "
                        f"a median of {median_deaths:g} across the "
                        "completed units.",
                "numbers": {"deaths": record.get("deaths", 0),
                            "median_deaths": median_deaths,
                            "time_s": record.get("time_s")},
            })

    first_done_times = [float(r.get("time_s", 0)) for r in first.values()
                        if r.get("completed")
                        and isinstance(r.get("time_s"), (int, float))]
    median_first_time = _median(first_done_times)
    for unit in sorted(first):
        record = first[unit]
        time_s = record.get("time_s")
        died = int(record.get("deaths", 0))
        slow = isinstance(time_s, (int, float)) and median_first_time > 0 \
            and time_s > 2 * median_first_time and len(first_done_times) >= 1
        if died >= 3 or slow:
            reason = (f"died {died} times" if died >= 3 else
                      f"spent {time_s}s against a median of "
                      f"{median_first_time:g}s on the units it finished")
            flags.append({
                "kind": "stall", "unit": unit,
                "text": f"First-time stall on unit {unit} — the "
                        f"first-time player {reason}.",
                "numbers": {"deaths": died, "time_s": time_s,
                            "median_time_s": median_first_time},
            })

    if comp and len(comp_done) == len(comp) \
            and sum(comp_deaths) == 0:
        first_finished = all(r.get("completed") for r in first.values()) \
            if first else False
        if first_finished or not first:
            times = [r.get("time_s") for r in comp_done]
            flags.append({
                "kind": "breeze", "unit": None,
                "text": "Breeze — every unit was completed with "
                        "zero deaths"
                        + (" and the first-time player finished "
                           "the game too" if first else "")
                        + ". The game may be too easy.",
                "numbers": {"units": len(comp_done),
                            "total_deaths": 0,
                            "unit_times_s": times},
            })
    return flags


# ---------------------------------------------------------------------------
# Suggestions — only the patch loop's bounded knobs

def _archetype_for(project: Path, config: dict) -> str:
    archetype = config.get("archetype", "")
    if archetype not in generate_module.ARCHETYPES:
        report = generate_module.load_report(project)
        archetype = (report or {}).get("archetype", archetype)
    if archetype not in generate_module.ARCHETYPES:
        raise BalanceError("The game's archetype is unknown, so no "
                           "safe suggestion can be computed.")
    return archetype


def suggestions(project_dir) -> list[dict]:
    """Numbered (1-based, in list order) suggestions from the
    current flags and config. Each: {kind, category, rationale,
    changes, config_after}. The rationale doubles as the
    synthetic feedback text when applied, so it names the lab
    numbers and — for config suggestions — the direction words
    the patch loop's deterministic table keys on."""
    project = Path(project_dir)
    units = load_units(project)
    flags = analyse(units)
    if not flags:
        return []
    try:
        config = patch_module.load_config(project)
    except patch_module.PatchError as exc:
        raise BalanceError(str(exc)) from exc
    archetype = _archetype_for(project, config)
    out: list[dict] = []

    hard_flags = [f for f in flags if f["kind"] in ("spike", "wall")]
    if hard_flags:
        new_config, lines, _note = patch_module.deterministic_balance(
            archetype, config, "too hard")
        if lines:
            detail = " ".join(f["text"] for f in hard_flags)
            out.append({
                "kind": "ease", "category": "balance",
                "rationale": f"Balance lab: {detail} This part of "
                             "the game is too hard — easing it with "
                             "the patch loop's bounded nudge.",
                "changes": lines,
                "config_after": new_config,
            })
    if any(f["kind"] == "breeze" for f in flags):
        new_config, lines, _note = patch_module.deterministic_balance(
            archetype, config, "too easy")
        if lines:
            breeze = next(f for f in flags if f["kind"] == "breeze")
            out.append({
                "kind": "harden", "category": "balance",
                "rationale": f"Balance lab: {breeze['text']} This "
                             "game is too easy — hardening it with "
                             "the patch loop's bounded nudge.",
                "changes": lines,
                "config_after": new_config,
            })
    stalls = [f for f in flags if f["kind"] == "stall"]
    if stalls:
        stall = stalls[0]
        probe = {"text": f"Balance lab: {stall['text']} The "
                         "objective hint may not be teaching this "
                         "part of the game."}
        new_hint, how = patch_module.confusion_change(
            project, config, probe, use_brain=False)
        current = str(config.get("objective_hint", ""))
        if new_hint != current:
            new_config = dict(config)
            new_config["objective_hint"] = new_hint
            out.append({
                "kind": "hint", "category": "confusion",
                "rationale": probe["text"],
                "changes": [f"objective hint → “{new_hint}” ({how})"],
                "config_after": new_config,
            })
    return out


def get_suggestion(project_dir, index: int) -> dict:
    found = suggestions(project_dir)
    if index < 1 or index > len(found):
        raise BalanceError(
            f"There is no suggestion {index} — "
            f"{len(found)} suggestion(s) are on offer. Run "
            "'weaverbird balance suggest' to see them.")
    return found[index - 1]


# ---------------------------------------------------------------------------
# Apply — through the patch loop, no side doors

def apply_suggestion(project_dir, index: int, settings_path=None,
                     godot_path=None, reporter=None) -> dict:
    """File the suggestion as a synthetic feedback item (source
    "balance-lab") and run it through patch.start_patch with the
    full gates. Returns the patch outcome dict."""
    project = Path(project_dir)
    suggestion = get_suggestion(project, index)
    item, _created = feedback_module.add_item(
        project, source=SOURCE, text=suggestion["rationale"])
    feedback_module.update_item(
        project, item["id"], category=suggestion["category"],
        status="approved")
    if reporter is not None:
        reporter(f"Filed as feedback [{item['id']}] (source "
                 f"{SOURCE}) and approved — entering the patch "
                 "loop with its full gates")
    return patch_module.start_patch(
        project, item["id"], settings_path=settings_path,
        godot_path=godot_path, reporter=reporter)


# ---------------------------------------------------------------------------
# Compare — an A/B with the project restored exactly

def _metrics_from_report(report: dict | None) -> dict:
    if not report:
        return {"ok": False, "win_proven": False, "lose_proven": False,
                "avg_fps": None, "units": [], "first_time_finished": None,
                "total_deaths": None, "total_time_s": None}
    units = report.get("units") or []
    comp = [u for u in units if u.get("source") == "competent"]
    first_time = (report.get("playtesters") or {}).get("first_time") or {}
    return {
        "ok": bool(report.get("ok")),
        "win_proven": bool(report.get("win_proven")),
        "lose_proven": bool(report.get("lose_proven")),
        "avg_fps": (report.get("performance") or {}).get("avg_fps"),
        "units": units,
        "first_time_finished": first_time.get("finished"),
        "total_deaths": sum(int(u.get("deaths", 0)) for u in comp),
        "total_time_s": round(
            sum(float(u.get("time_s", 0)) for u in comp), 2),
    }


def _verdict(baseline: dict, candidate: dict) -> str:
    if baseline.get("ok") and not candidate.get("ok"):
        return ("The candidate breaks the test suite (the baseline "
                "passes) — do not apply it.")
    if not baseline.get("ok") and candidate.get("ok"):
        return ("The candidate makes the test suite pass where "
                "the baseline fails — a clear improvement.")
    b_deaths, c_deaths = baseline.get("total_deaths"), candidate.get("total_deaths")
    b_time, c_time = baseline.get("total_time_s"), candidate.get("total_time_s")
    if isinstance(b_deaths, (int, float)) and isinstance(c_deaths, (int, float)):
        if c_deaths < b_deaths:
            return (f"The candidate eases the game: competent "
                    f"deaths {b_deaths} → {c_deaths} across units, "
                    f"win still proven: "
                    f"{'yes' if candidate.get('win_proven') else 'no'}.")
        if c_deaths > b_deaths:
            return (f"The candidate hardens the game: competent "
                    f"deaths {b_deaths} → {c_deaths} across units, "
                    f"win still proven: "
                    f"{'yes' if candidate.get('win_proven') else 'no'}.")
        if isinstance(b_time, (int, float)) and isinstance(c_time, (int, float)) \
                and abs(c_time - b_time) > 0.5:
            direction = "faster" if c_time < b_time else "slower"
            return (f"Same deaths ({b_deaths}); the competent run "
                    f"is {direction} with the candidate "
                    f"({b_time}s → {c_time}s of unit time).")
    return ("No measurable difference in unit deaths or times "
            "between the baseline and the candidate.")


def compare(project_dir, suggestion_index: int | None = None,
            config_overrides: dict | None = None, settings_path=None,
            godot_path=None, reporter=None) -> dict:
    """A/B one suggestion (or raw config overrides) against the
    current game, restoring the project exactly afterwards.

    Baseline metrics come from the existing test report when it
    is at least as fresh as the config file; otherwise the test
    stage runs first. The candidate always costs one full test
    run. Returns (and writes) the compare report.
    """
    project = Path(project_dir)
    try:
        config = patch_module.load_config(project)
    except patch_module.PatchError as exc:
        raise BalanceError(str(exc)) from exc
    archetype = _archetype_for(project, config)

    changes: list[str] = []
    label = ""
    if suggestion_index is not None:
        suggestion = get_suggestion(project, suggestion_index)
        candidate_config = suggestion["config_after"]
        changes = suggestion["changes"]
        label = (f"suggestion {suggestion_index} "
                 f"({suggestion['kind']})")
    elif config_overrides is not None:
        numeric = {k: v for k, v in config_overrides.items()
                   if k != "objective_hint"}
        candidate_config, changes = patch_module._apply_numeric_changes(
            config, archetype, numeric)
        factor = numeric.get("move_budget_multiplier")
        if isinstance(factor, (int, float)) and config.get("puzzles"):
            # Budgets live inside the puzzle records, not at the
            # top level — scale them through the patch loop's own
            # budget scaler (same floor rules as a shipped patch).
            candidate_config, budget_lines = \
                patch_module._scale_budgets(candidate_config,
                                            float(factor))
            changes.extend(budget_lines)
        if "objective_hint" in config_overrides:
            candidate_config = dict(candidate_config)
            candidate_config["objective_hint"] = str(
                config_overrides["objective_hint"])
            changes.append("objective hint → "
                           f"“{config_overrides['objective_hint']}”")
        label = "the given config overrides"
    else:
        raise BalanceError("Compare needs a suggestion number or "
                           "config overrides.")
    if candidate_config == config:
        raise BalanceError("The candidate changes nothing — every "
                           "value is already at its bound.")

    config_path = patch_module.config_path_for(project)
    original_config = config_path.read_bytes()
    report_path = testing_module.report_path_for(project)
    # Snapshot the whole factory-state directory, not just the
    # files this function knows about: a test run rewrites its
    # report, appends to the ledger and may touch stage reports,
    # and compare's contract is that ALL of it goes back.
    wb_dir = project / ".weaverbird"
    snapshot: dict[str, bytes] = {}
    if wb_dir.is_dir():
        for path in sorted(wb_dir.rglob("*")):
            if path.is_file():
                snapshot[str(path.relative_to(project))] = \
                    path.read_bytes()

    existing = testing_module.load_report(project)
    baseline_fresh = False
    if existing is not None and report_path.exists():
        baseline_fresh = report_path.stat().st_mtime >= \
            config_path.stat().st_mtime

    # No git checkpoint is taken on purpose: nothing in a
    # compare commits (repair is disabled for its runs), and a
    # checkpoint commit would itself change git status by
    # sweeping up whatever state the directory started in. The
    # byte snapshots are the restore.

    try:
        if baseline_fresh and existing is not None:
            if reporter is not None:
                reporter("Baseline: the existing test report is "
                         "fresher than the config — using it "
                         "(no baseline run needed)")
            baseline_report = existing
        else:
            if reporter is not None:
                reporter("Baseline: running the test stage on the "
                         "current config", done=0, total=2)
            baseline_report = testing_module.run_tests(
                project, godot_path=godot_path,
                settings_path=settings_path, allow_repair=False,
                reporter=reporter)
        baseline = _metrics_from_report(baseline_report)
        if reporter is not None:
            reporter(f"Candidate: applying {label} and running "
                     "the test stage", done=1, total=2)
        patch_module.save_config(project, candidate_config)
        candidate_report = testing_module.run_tests(
            project, godot_path=godot_path,
            settings_path=settings_path, allow_repair=False,
            reporter=reporter)
        candidate = _metrics_from_report(candidate_report)
    finally:
        # Restore exactly: the config returns byte-for-byte and
        # the whole .weaverbird state directory returns to its
        # snapshot (files a run created are removed, files it
        # rewrote get their original bytes). The compare report
        # itself is written after this block — it is the one
        # intended new file.
        config_path.write_bytes(original_config)
        if wb_dir.is_dir():
            for path in sorted(wb_dir.rglob("*")):
                if path.is_file() and \
                        str(path.relative_to(project)) not in snapshot:
                    path.unlink()
        for rel, data in snapshot.items():
            dest = project / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)

    verdict = _verdict(baseline, candidate)
    result = {
        "compared_at": datetime.now(timezone.utc)
            .isoformat(timespec="seconds"),
        "candidate": label,
        "changes": changes,
        "baseline_from_existing_report": baseline_fresh,
        "baseline": baseline,
        "candidate_metrics": candidate,
        "verdict": verdict,
    }
    path = report_path_for(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, indent=2) + "\n",
                    encoding="utf-8")
    if reporter is not None:
        reporter("Project restored to exactly its starting state",
                 done=2, total=2)
    return result


def config_fingerprint(project_dir) -> str:
    """A hash of the game's config — the test/verification seam
    for 'the game directory ends exactly as it started'."""
    path = patch_module.config_path_for(project_dir)
    if not path.exists():
        return ""
    return hashlib.sha256(path.read_bytes()).hexdigest()
