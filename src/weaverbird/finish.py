"""The finish check and the finish report.

The factory marks its own definition-of-done checkboxes — but only
where it holds real evidence, and always with the evidence written
next to the mark (who marked it, when, and what backs it). An item it
cannot evidence stays open, with a plain reason. Honesty is the
feature: nothing here marks a box because it would be convenient.

Evidence sources, in order of authority:

- ``.weaverbird/test_report.json`` — a test/playtest report (the
  format the test stages produce; see ``_test_evidence`` for the keys
  read). When it exists it takes precedence.
- ``.weaverbird/skeleton_report.json`` — the Godot validation runs
  from the skeleton build.
- The generated game itself (``game/``) — which scripts exist.

``finish report`` then writes FINISH_REPORT.md: the game's facts,
what was built, the definition-of-done table with its evidence, the
cost ledger, the provenance summary with the store disclosure draft,
and the decisions that are left for the human. That report is what
the approval stage reads.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from . import brain as brain_module
from . import ledger as ledger_module
from . import provenance as provenance_module
from . import scope as scope_module
from . import skeleton as skeleton_module
from . import state as state_module

REPORT_FILE_NAME = "FINISH_REPORT.md"
TEST_REPORT_NAME = "test_report.json"

# Definition-of-done items in plain words, for reports and screens.
DOD_LABELS = {
    "scenario_tests_pass": "Scenario tests pass",
    "no_crashes_in_full_playthroughs": "No crashes in full playthroughs",
    "performance_targets_met": "Performance targets met",
    "menus_save_settings_complete": "Menus, save and settings complete",
    "game_winnable_and_losable": "Game is winnable and losable",
    "content_counts_delivered": "Content counts delivered",
    "icon_and_polish_gate_passed": "Icon and polish gate passed",
}

_MENU_SYSTEM_FILES = {
    "main_menu.gd": "main menu",
    "settings.gd": "settings screen",
    "settings_store.gd": "saved settings and run stats (user:// JSON)",
    "pause_input.gd": "pause",
}


class FinishError(Exception):
    """Raised when a finish check or report cannot run at all."""


# ---------------------------------------------------------------------------
# Evidence gathering

def test_report_path_for(project_dir) -> Path:
    return Path(project_dir) / ".weaverbird" / TEST_REPORT_NAME


def load_test_report(project_dir) -> dict | None:
    path = test_report_path_for(project_dir)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return data if isinstance(data, dict) else None


def _test_evidence(report: dict) -> dict:
    """Normalise a test report into the evidence the checks read.

    Keys read (all optional): ``runs`` (a list of {name, ok}), and the
    booleans ``scenarios_pass``, ``no_crashes``, ``winnable``,
    ``losable``, ``performance_targets_met`` (or ``performance_ok``),
    ``content_counts_delivered`` (or ``content_ok``),
    ``icon_and_polish_gate_passed`` (or ``icon_polish_ok``). A nested
    ``performance: {"ok": true}`` is also honoured.
    """
    evidence: dict = {}
    runs = report.get("runs")
    if isinstance(runs, list) and runs:
        evidence["runs"] = runs
        evidence["runs_all_ok"] = all(bool(r.get("ok")) for r in runs
                                      if isinstance(r, dict)) and all(
                                          isinstance(r, dict) for r in runs)
    for key in ("scenarios_pass", "no_crashes", "winnable", "losable"):
        if isinstance(report.get(key), bool):
            evidence[key] = report[key]
    perf = report.get("performance")
    if isinstance(perf, dict) and isinstance(perf.get("ok"), bool):
        evidence["performance_targets_met"] = perf["ok"]
    for key, aliases in (
        ("performance_targets_met", ("performance_targets_met", "performance_ok")),
        ("content_counts_delivered", ("content_counts_delivered", "content_ok")),
        ("icon_and_polish_gate_passed", ("icon_and_polish_gate_passed", "icon_polish_ok")),
    ):
        for alias in aliases:
            if isinstance(report.get(alias), bool):
                evidence[key] = report[alias]
    return evidence


def _skeleton_runs_ok(report: dict) -> bool:
    validation = report.get("validation", {})
    return bool(validation.get("passed")) and bool(validation.get("runs"))


def _smoke_won(report: dict) -> bool:
    for run in report.get("validation", {}).get("runs", []):
        if run.get("name") == "flow smoke":
            return bool(run.get("ok") and run.get("smoke_pass"))
    return False


def _evaluate(project_dir) -> tuple[dict, dict]:
    """Work out what can be marked. Returns (marks, open_reasons).

    marks: item -> evidence note. open_reasons: item -> why it stays
    open. Nothing is written to state here.
    """
    project = Path(project_dir)
    marks: dict = {}
    reasons: dict = {}

    test_report = load_test_report(project)
    tev = _test_evidence(test_report) if test_report else {}
    skeleton_report = skeleton_module.load_report(project)
    from_test = test_report is not None

    # --- scenario tests and crashes -----------------------------------
    if from_test and (tev.get("scenarios_pass") or tev.get("runs_all_ok")):
        marks["scenario_tests_pass"] = (
            "Test report: all scenario runs passed."
            if tev.get("runs_all_ok") else "Test report: scenarios pass."
        )
    elif not from_test and skeleton_report and _skeleton_runs_ok(skeleton_report):
        names = ", ".join(
            run["name"] for run in skeleton_report["validation"]["runs"]
        )
        marks["scenario_tests_pass"] = (
            f"Skeleton validation passed in Godot "
            f"({skeleton_report['validation'].get('godot_version', 'unknown version')}): "
            f"{names} — the flow smoke plays menu → game → win."
        )
    else:
        reasons["scenario_tests_pass"] = (
            "No passing test evidence yet — neither a test report nor a "
            "passed skeleton validation is on record."
        )

    if from_test and (tev.get("no_crashes") or tev.get("runs_all_ok")):
        marks["no_crashes_in_full_playthroughs"] = (
            "Test report: full playthroughs completed with no crashes."
        )
    elif not from_test and skeleton_report and _skeleton_runs_ok(skeleton_report):
        marks["no_crashes_in_full_playthroughs"] = (
            "Every Godot validation run finished with no errors and no "
            "crashes (import, menu scene, game scene and the full flow smoke)."
        )
    else:
        reasons["no_crashes_in_full_playthroughs"] = (
            "No crash-free playthrough evidence is on record yet."
        )

    # --- performance ---------------------------------------------------
    if tev.get("performance_targets_met"):
        marks["performance_targets_met"] = "Test report: performance targets met."
    else:
        reasons["performance_targets_met"] = (
            "No performance measurement exists yet — the headless runs "
            "check for errors, not frame-rate or load-time targets. The "
            "test stages must measure performance before this can be marked."
        )

    # --- menus / save / settings ---------------------------------------
    scripts_dir = project / skeleton_module.GAME_DIR_NAME / "scripts"
    present = [name for name in _MENU_SYSTEM_FILES if (scripts_dir / name).exists()]
    if len(present) == len(_MENU_SYSTEM_FILES):
        named = ", ".join(sorted(_MENU_SYSTEM_FILES[n] for n in present))
        marks["menus_save_settings_complete"] = (
            f"The generated game contains the menu, settings, save and "
            f"pause systems ({named}); the flow smoke exercised them."
        )
    else:
        missing = [n for n in _MENU_SYSTEM_FILES if n not in present]
        reasons["menus_save_settings_complete"] = (
            "The generated game is missing system scripts: "
            + ", ".join(sorted(missing))
            + "."
        )

    # --- winnable and losable ------------------------------------------
    winnable = bool(tev.get("winnable")) or (
        not from_test and skeleton_report and _smoke_won(skeleton_report)
    )
    losable = bool(tev.get("losable"))
    if winnable and losable:
        source = "Test report" if tev.get("winnable") or tev.get("losable") \
            else "Skeleton validation"
        marks["game_winnable_and_losable"] = (
            f"{source}: a win and a loss are both on record."
        )
    elif winnable and not losable:
        reasons["game_winnable_and_losable"] = (
            "The win path is proven (the flow smoke reaches the win "
            "screen), but no lose-path evidence exists yet — nobody has "
            "recorded the game being lost. The item stays open."
        )
    else:
        reasons["game_winnable_and_losable"] = (
            "Neither a win nor a loss is on record yet."
        )

    # --- content counts --------------------------------------------------
    if tev.get("content_counts_delivered"):
        marks["content_counts_delivered"] = "Test report: content counts delivered."
    else:
        reasons["content_counts_delivered"] = (
            "The scope's content counts describe the finished game "
            "(levels, waves, rooms, items). The grey-box skeleton "
            "generates one arena with its targets, which is not the "
            "full content — this can only be marked once real content "
            "generation is checked against the scope."
        )

    # --- icon and polish -------------------------------------------------
    if tev.get("icon_and_polish_gate_passed"):
        marks["icon_and_polish_gate_passed"] = "Test report: icon and polish gate passed."
    else:
        reasons["icon_and_polish_gate_passed"] = (
            "The skeleton ships a placeholder icon, and no clean "
            "first-run polish check has been recorded. The real icon "
            "arrives with asset generation; the gate stays open."
        )

    return marks, reasons


def evaluate_finish(project_dir) -> dict:
    """Mark every definition-of-done item the evidence supports.

    Persists the marks (with evidence) into pipeline state and
    returns {marked_now, already_marked, open} — open maps item ->
    the plain reason it stays open.
    """
    project = Path(project_dir)
    try:
        state = state_module.load_state(project)
    except state_module.PipelineError as exc:
        raise FinishError(str(exc)) from exc
    marks, reasons = _evaluate(project)
    evidence = state.get("dod_evidence", {})
    dod = state.get("definition_of_done", {})
    marked_now: dict = {}
    already: dict = {}
    for key, note in marks.items():
        if dod.get(key) and key in evidence:
            already[key] = evidence[key].get("note", note)
        else:
            state_module.mark_definition_of_done(project, key, note,
                                                 by="weaverbird")
            marked_now[key] = note
    open_items = {key: reasons[key] for key in state_module.DEFINITION_OF_DONE
                  if key not in marks}
    return {"marked_now": marked_now, "already_marked": already,
            "open": open_items}


# ---------------------------------------------------------------------------
# The finish report

def _facts(project: Path) -> dict:
    parsed = None
    scope_file = scope_module.scope_path_for(project)
    if scope_file.exists():
        parsed = scope_module.load_scope(scope_file)
    values = {f.name: f.value for f in parsed.fields} if parsed else {}
    state = {}
    try:
        state = state_module.load_state(project)
    except state_module.PipelineError:
        state = {}
    return {"values": values, "state": state, "scope": parsed}


def template_summary(facts: dict, evaluation: dict) -> str:
    """The report's opening paragraph, built from the facts alone."""
    values = facts["values"]
    title = values.get("Title (working)") or "This game"
    dod = facts["state"].get("definition_of_done", {})
    done = sum(1 for v in dod.values() if v)
    stage = facts["state"].get("stage", "scope")
    summary = ledger_module.summarise(facts["project"])
    return (
        f"{title} is at the {stage} stage of the factory. "
        f"{done} of {len(dod)} definition-of-done items are marked, each "
        f"backed by recorded evidence; the rest stay open with their "
        f"reasons listed below. Total recorded spend is "
        f"{summary['tokens']} tokens (cost {summary['cost']:.4f}); the "
        f"skeleton stage itself is deterministic and spent none."
    )


def build_finish_report(project_dir, settings_path=None,
                        use_brain: bool = True) -> Path:
    """Write FINISH_REPORT.md for a project and return its path.

    Runs the finish check first (so the state the report describes is
    current), then writes the report. When a frontier model is
    configured, it may write the opening summary paragraph from the
    structured facts — clearly labelled as the only model-written
    part; everything else is built from the records.
    """
    project = Path(project_dir)
    evaluation = evaluate_finish(project)
    facts = _facts(project)
    facts["project"] = project
    values = facts["values"]
    state = facts["state"]
    title = values.get("Title (working)") or project.name
    pitch = values.get("One-line pitch") or ""

    summary_paragraph = ""
    summary_note = ""
    if use_brain:
        _, data = brain_module.resolve_settings(settings_path, project)
        status = brain_module.tier_status(data, "frontier") if data else None
        if status and status["usable"]:
            plain_facts = {
                "title": title,
                "pitch": pitch,
                "stage": state.get("stage"),
                "definition_of_done": state.get("definition_of_done", {}),
                "evidence": state.get("dod_evidence", {}),
                "open_reasons": evaluation["open"],
                "ledger": ledger_module.summarise(project),
            }
            try:
                result = brain_module.complete(
                    project, "frontier",
                    [{"role": "user", "content": (
                        "Write a short summary paragraph (3–5 sentences, "
                        "plain words, no hype) of this game's state for its "
                        "finish report, using only these facts as JSON:\n"
                        + json.dumps(plain_facts, indent=2)
                    )}],
                    stage="finish-report", settings_path=settings_path,
                    max_tokens=320,
                )
                if result.ok and result.text.strip():
                    summary_paragraph = result.text.strip()
                    summary_note = (
                        "Summary written by the frontier model "
                        f"({result.provider}/{result.model}) from the "
                        "structured facts below — the only model-written "
                        "part of this report."
                    )
            except brain_module.BrainBudgetError:
                pass  # fall back to the template paragraph, silently honest
    if not summary_paragraph:
        summary_paragraph = template_summary(facts, evaluation)
        summary_note = ("Summary built from the project's records "
                        "(no model was used).")

    lines: list[str] = []
    lines.append(f"# Finish report — {title}")
    lines.append("")
    if pitch:
        lines.append(pitch)
        lines.append("")
    lines.append(summary_paragraph)
    lines.append("")
    lines.append(f"_{summary_note}_")
    lines.append("")

    lines.append("## Where the game is")
    lines.append("")
    lines.append(f"Current pipeline stage: **{state.get('stage', 'unknown')}**")
    lines.append("")
    lines.append("Stage history:")
    for item in state.get("history", []):
        lines.append(f"- {item.get('stage')} — {item.get('at')}")
    lines.append("")

    lines.append("## What was built")
    lines.append("")
    skeleton_report = skeleton_module.load_report(project)
    if skeleton_report:
        files = skeleton_report.get("files", [])
        lines.append(
            f"A grey-box Godot project in `game/` ({len(files)} files), "
            "generated from the approved scope by the skeleton builder."
        )
        validation = skeleton_report.get("validation", {})
        if validation.get("runs"):
            lines.append("")
            lines.append(f"Godot validation ({validation.get('godot_version', 'unknown')}):")
            for run in validation["runs"]:
                mark = "✓" if run.get("ok") else "✗"
                lines.append(
                    f"- {mark} {run.get('name')} — "
                    f"{run.get('duration_seconds', '?')}s, "
                    f"{'no errors' if not run.get('errors') else '; '.join(run['errors'][:3])}"
                )
        if skeleton_report.get("known_good_tag"):
            lines.append("")
            lines.append(f"Known-good tag: `{skeleton_report['known_good_tag']}`")
    else:
        lines.append("No skeleton has been built for this project yet.")
    test_report = load_test_report(project)
    if test_report:
        lines.append("")
        lines.append("A test report (`.weaverbird/test_report.json`) is on "
                     "record and was used as evidence where it applies.")
    lines.append("")

    lines.append("## Definition of done")
    lines.append("")
    lines.append("| Item | Status | Evidence / reason |")
    lines.append("| --- | --- | --- |")
    dod = state.get("definition_of_done", {})
    evidence = state.get("dod_evidence", {})
    for key in state_module.DEFINITION_OF_DONE:
        label = DOD_LABELS.get(key, key)
        if dod.get(key):
            note = evidence.get(key, {}).get("note", "Marked.")
            lines.append(f"| {label} | ✓ done | {note} |")
        else:
            reason = evaluation["open"].get(key, "Still open.")
            lines.append(f"| {label} | open | {reason} |")
    lines.append("")

    summary = ledger_module.summarise(project)
    lines.append("## What it cost")
    lines.append("")
    lines.append(f"Total: {summary['tokens']} tokens, cost {summary['cost']:.4f} "
                 f"({summary['entries']} ledger entries).")
    if summary["by_stage"]:
        lines.append("")
        lines.append("By stage:")
        for name, slot in sorted(summary["by_stage"].items()):
            lines.append(f"- {name}: {slot['tokens']} tokens, cost {slot['cost']:.4f}")
    if summary["by_tier"]:
        lines.append("")
        lines.append("By model tier:")
        for name, slot in sorted(summary["by_tier"].items()):
            lines.append(f"- {name}: {slot['tokens']} tokens, cost {slot['cost']:.4f}")
    lines.append("")
    lines.append("The skeleton stage is deterministic — it calls no model "
                 "and its ledger entries are 0 tokens, 0 cost.")
    lines.append("")

    records = provenance_module.list_records(project)
    counts: dict = {}
    for record in records:
        counts[record.get("source", "unknown")] = \
            counts.get(record.get("source", "unknown"), 0) + 1
    lines.append("## Provenance")
    lines.append("")
    if records:
        parts = ", ".join(f"{counts[k]} {k}" for k in sorted(counts))
        lines.append(f"{len(records)} assets/files recorded ({parts}).")
    else:
        lines.append("No provenance records yet.")
    lines.append("")
    lines.append("Disclosure draft for store content surveys, built from "
                 "the provenance log:")
    lines.append("")
    lines.append("```")
    lines.append(provenance_module.disclosure_text(project).rstrip("\n"))
    lines.append("```")
    lines.append("")

    lines.append("## Decisions left for the human")
    lines.append("")
    if evaluation["open"]:
        lines.append("Definition-of-done items still open:")
        for key, reason in evaluation["open"].items():
            lines.append(f"- {DOD_LABELS.get(key, key)} — {reason}")
        lines.append("")
    lines.append("Approval-stage decisions (always the maker's, never the "
                 "factory's):")
    price = values.get(
        "Price intent (free / paid + rough price / decide at approval gate)", "")
    lines.append(f"- Final price — scope intent: {price or 'not stated'}; "
                 "the price itself is set at approval.")
    platforms = values.get("Platforms (Windows / macOS / Linux / Web / Android / iOS)", "")
    lines.append(f"- Platforms — scope says: {platforms or 'not stated'}; "
                 "confirm before publishing.")
    stores = values.get("Target storefronts, in order (itch / Steam / Play / other)", "")
    lines.append(f"- Storefronts — scope says: {stores or 'not stated'}; each "
                 "store's publish step needs your permission at the time.")
    lines.append("- Store copy — no store page draft exists yet; it is "
                 "written and approved at the approval stage.")
    lines.append("- The game itself — play the release candidate and "
                 "approve it before anything is published.")
    lines.append("")

    path = project / REPORT_FILE_NAME
    path.write_text("\n".join(lines), encoding="utf-8")
    return path
