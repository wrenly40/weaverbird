"""Self-repair — when the test stage fails, the factory tries to
fix the game itself, inside strict bounds, and proves the result.

The loop (at most ``repair.max_attempts`` attempts, default 3 from
the global settings):

1. Read the latest test report and classify each failure:
   crash_or_error, win_not_proven, lose_not_proven,
   performance_low, breaker_stuck, missing_assets.
2. Apply ONE strategy per failure class per attempt, from a fixed
   menu — config easing/hardening inside generate.py's bounds,
   regenerating assets, regenerating the game for a known parse
   error. A brain, when configured, may choose among the offered
   strategies (mid tier, strict JSON, validated); it may never
   invent a new edit type. Unknown crash signatures escalate
   immediately — the factory does not blind-edit code.
3. Checkpoint in git BEFORE each attempt's changes
   (``repair/attempt-<n>``), re-run the test stage, and keep the
   change only when the pass count strictly improves or the
   failure class it targeted is resolved; otherwise restore the
   checkpoint and move on.

Termination: a passing suite → final commit, tag
``known-good/repair``, REPAIR_REPORT.md. Exhaustion → the project
is restored to its best checkpoint (never worse than it started)
and the report says, plainly, that it needs the user.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from . import assets_gen as assets_module
from . import brain as brain_module
from . import generate as generate_module
from . import patch as patch_module
from . import plan as plan_module
from . import testing as testing_module
from . import vcs as vcs_module

REPORT_NAME = "REPAIR_REPORT.md"
DEFAULT_MAX_ATTEMPTS = 3

FAILURE_CLASSES = (
    "crash_or_error", "win_not_proven", "lose_not_proven",
    "performance_low", "breaker_stuck", "missing_assets",
)

# Strategies the loop may apply, per failure class. The brain may
# pick from these; it may not add to the menu.
STRATEGY_MENU = {
    "win_not_proven": ["ease_config"],
    "lose_not_proven": ["harden_config"],
    "performance_low": ["reduce_load"],
    "missing_assets": ["regenerate_assets"],
    "crash_or_error": ["regenerate_game", "regenerate_assets"],
    "breaker_stuck": ["ease_config"],
}


class RepairError(Exception):
    """Raised when repair cannot run at all."""


def max_attempts_for(project, settings_path=None) -> int:
    _path, data = brain_module.resolve_settings(settings_path, project)
    if isinstance(data, dict):
        section = data.get("repair")
        if isinstance(section, dict):
            value = section.get("max_attempts")
            if isinstance(value, (int, float)) and value >= 0:
                return int(value)
    return DEFAULT_MAX_ATTEMPTS


def classify_failures(report: dict | None) -> list[dict]:
    """Turn a test report into classified failures:
    [{class, detail}] in a stable order."""
    if not report:
        return []
    failures: list[dict] = []
    runs = report.get("runs", [])
    error_text = "\n".join(
        str(e) for run in runs for e in run.get("errors", []))
    missing_assets = any(
        marker in error_text.lower()
        for marker in ("failed loading resource", "cannot open file",
                       "res://assets"))
    base_failed = [r for r in runs
                   if r.get("kind") == "base" and not r.get("ok")]
    scripted_crash = [
        r for r in runs
        if r.get("kind") == "scripted" and not r.get("ok") and r.get("errors")
        and r.get("name") not in ("competent win", "competent lose")]
    if missing_assets:
        failures.append({
            "class": "missing_assets",
            "detail": "a run could not load an asset file"})
    if base_failed or scripted_crash:
        names = [r.get("name") for r in base_failed + scripted_crash]
        failures.append({
            "class": "crash_or_error",
            "detail": "runs with errors: " + ", ".join(names),
            "errors": [e for r in base_failed + scripted_crash
                       for e in r.get("errors", [])][:5]})
    if not report.get("win_proven", report.get("winnable")):
        failures.append({"class": "win_not_proven",
                         "detail": "the competent player did not reach a win"})
    if not report.get("lose_proven", report.get("losable")):
        failures.append({"class": "lose_not_proven",
                         "detail": "the competent player did not reach a loss"})
    performance = report.get("performance", {})
    if performance and not performance.get("ok"):
        failures.append({
            "class": "performance_low",
            "detail": f"average FPS {performance.get('avg_fps')} against "
                      f"a target of {performance.get('target_fps')}"})
    breaker = (report.get("playtesters") or {}).get("breaker", {})
    findings = breaker.get("findings") or []
    if findings and not any(f["class"] == "crash_or_error" for f in failures):
        failures.append({
            "class": "breaker_stuck",
            "detail": "; ".join(str(f) for f in findings[:3])})
    return failures


def pass_count(report: dict | None) -> int:
    """The headline the loop optimises: base runs ok + win + lose
    + performance ok."""
    if not report:
        return 0
    count = sum(1 for r in report.get("runs", [])
                if r.get("kind") == "base" and r.get("ok"))
    count += int(bool(report.get("win_proven")))
    count += int(bool(report.get("lose_proven")))
    count += int(bool((report.get("performance") or {}).get("ok")))
    return count


# ---------------------------------------------------------------------------
# Strategies — each returns a human-readable note, or raises
# RepairError when it cannot change anything (an escalation for
# that failure class, not a loop failure).

def _ease_config(project, config: dict, archetype: str) -> str:
    # The patch loop's nudge table IS the easing table ("too hard").
    new_config, lines, note = patch_module.deterministic_balance(
        archetype, config, "too hard")
    if not lines:
        raise RepairError(
            "config is already at its easiest bounds — easing cannot "
            "change anything")
    patch_module.save_config(project, new_config)
    return note + ": " + "; ".join(lines)


def _harden_config(project, config: dict, archetype: str) -> str:
    if archetype in plan_module.PLAN_BOUNDS:
        # Genre-agnostic build: archetype carries the plan's
        # movement; harden through the same named tunables.
        if archetype in ("top_down_2d", "top_down_3d"):
            new_config, lines = patch_module._apply_numeric_changes(
                config, archetype, {
                    "hazard_speed": float(config.get("hazard_speed", 1.4)) * 1.2,
                    "round_seconds": float(config.get("round_seconds", 60)) * 0.85})
            note = "hardened the game so a loss is reachable"
        elif archetype == "side_view":
            new_config, lines = patch_module._apply_numeric_changes(
                config, archetype, {
                    "gravity": float(config.get("gravity", 1400)) * 1.05,
                    "jump_strength": float(config.get("jump_strength", 545)) * 0.97,
                    "lives": 1})
            note = "hardened the game so a loss is reachable"
        else:
            new_config, lines = patch_module._apply_numeric_changes(
                config, archetype, {
                    "move_budget_multiplier": float(
                        config.get("move_budget_multiplier", 2.5)) * 0.8})
            note = "tightened move budgets so a loss is reachable"
        if not lines:
            raise RepairError("config is already at its hardest bounds")
        patch_module.save_config(project, new_config)
        return note + ": " + "; ".join(lines)
    if archetype in ("arena_collector", "arena_3d"):
        new_config, lines = patch_module._apply_numeric_changes(
            config, archetype, {
                "hazard_speed": float(config.get("hazard_speed", 1.4)) * 1.2,
                "round_seconds": float(config.get("round_seconds", 60)) * 0.85})
        note = "hardened the arena so a loss is reachable"
    elif archetype == "platformer":
        new_config, lines = patch_module._apply_numeric_changes(
            config, archetype, {
                "gravity": float(config.get("gravity", 1400)) * 1.05,
                "jump_strength": float(config.get("jump_strength", 545)) * 0.97,
                "lives": 1})
        note = "hardened the platformer so a loss is reachable"
    else:
        new_config, lines = patch_module._scale_budgets(config, 0.8)
        note = "tightened puzzle budgets so a loss is reachable"
        if not lines:
            raise RepairError("puzzle budgets are already at their floor")
    if not lines:
        raise RepairError("config is already at its hardest bounds")
    patch_module.save_config(project, new_config)
    return note + ": " + "; ".join(lines)


def _reduce_load(project, config: dict, archetype: str) -> str:
    proposals: dict = {}
    if archetype in ("top_down_2d", "top_down_3d"):
        current = int(config.get("targets_per_round", 5))
        floor = patch_module.patch_bounds(archetype)["targets_per_round"][0]
        if current <= floor:
            raise RepairError(
                "load is already at its floor — cannot repair performance "
                "by config alone")
        proposals["targets_per_round"] = max(floor, int(current * 0.8))
    elif archetype == "side_view":
        raise RepairError(
            "side-view load is level geometry, not a config number — "
            "cannot repair performance by config alone")
    elif archetype == "grid_step":
        raise RepairError(
            "grid puzzles have no load knob — cannot repair performance "
            "by config alone")
    elif archetype in ("arena_collector", "arena_3d"):
        current = int(config.get("targets_per_round", 5))
        floor = patch_module.patch_bounds(archetype)["targets_per_round"][0]
        if current <= floor:
            raise RepairError(
                "load is already at its floor — cannot repair performance "
                "by config alone")
        proposals["targets_per_round"] = max(floor, int(current * 0.8))
    elif archetype == "platformer":
        raise RepairError(
            "platformer load is level geometry, not a config number — "
            "cannot repair performance by config alone")
    else:
        raise RepairError(
            "crate puzzles have no load knob — cannot repair performance "
            "by config alone")
    new_config, lines = patch_module._apply_numeric_changes(
        config, archetype, proposals)
    if not lines:
        raise RepairError("no load change was possible")
    patch_module.save_config(project, new_config)
    return "reduced load: " + "; ".join(lines)


def _regenerate_assets(project, _config, _archetype) -> str:
    report = assets_module.generate_assets(project)
    return f"regenerated {len(report['files'])} asset files"


def _regenerate_game(project, _config, _archetype, settings_path=None) -> str:
    errors_joined = ""
    report = testing_module.load_report(project)
    if report:
        errors_joined = "\n".join(
            str(e) for run in report.get("runs", [])
            for e in run.get("errors", [])).lower()
    if "parse error" not in errors_joined and "script error" not in errors_joined:
        raise RepairError(
            "the crash signature is not a known class (no parse/script "
            "error in a generated script) — escalating instead of "
            "blind-editing")
    generate_module.build_game(project, settings_path=settings_path)
    return "regenerated the game from its scope"


_STRATEGY_FUNCS = {
    "ease_config": _ease_config,
    "harden_config": _harden_config,
    "reduce_load": _reduce_load,
    "regenerate_assets": _regenerate_assets,
    "regenerate_game": _regenerate_game,
}


def _brain_choose(project, failure: dict, offered: list[str],
                  settings_path=None) -> str | None:
    """Mid tier picks among the offered strategies. None = use the
    deterministic order. Anything invalid falls back silently."""
    if len(offered) < 2:
        return None
    _, data = brain_module.resolve_settings(settings_path, project)
    status = brain_module.tier_status(data, "mid") if data else None
    if not status or not status.get("usable"):
        return None
    prompt = (
        "A generated game failed its tests. Failure class: "
        f"{failure['class']} — {failure.get('detail', '')}\n"
        "You may choose exactly one of these repair strategies: "
        + ", ".join(offered) + "\n"
        'Answer with one JSON object: {"strategy": "<one of them>"}.')
    try:
        result = brain_module.complete(
            project, "mid", [{"role": "user", "content": prompt}],
            stage="repair", settings_path=settings_path, max_tokens=80)
    except brain_module.BrainBudgetError:
        return None
    if not result.ok:
        return None
    choice = str(brain_module.extract_json(result.text).get("strategy", ""))
    return choice if choice in offered else None


# ---------------------------------------------------------------------------
# The loop

def run_repair(project_dir, settings_path=None, godot_path=None,
               max_attempts: int | None = None, reporter=None) -> dict:
    """Run the repair loop. Returns the outcome dict and writes
    REPAIR_REPORT.md either way. ``reporter`` (optional) narrates
    each attempt for the job runner."""
    project = Path(project_dir)
    if not (project / patch_module.config_path_for(project)).exists() \
            and not (project / "game" / "project.godot").exists():
        raise RepairError("No built game found — there is nothing to repair.")
    if max_attempts is None:
        max_attempts = max_attempts_for(project, settings_path)
    report = testing_module.load_report(project)
    if report is None:
        report = testing_module.run_tests(
            project, godot_path=godot_path, settings_path=settings_path,
            allow_repair=False, reporter=reporter)
    attempts: list[dict] = []
    started = {
        "pass_count": pass_count(report),
        "failures": classify_failures(report),
    }
    if reporter is not None:
        found = ", ".join(f["class"] for f in started["failures"]) or "none"
        reporter(f"Repair: classified failures — {found} "
                 f"(pass count {started['pass_count']})")

    def finish(outcome: str, note: str) -> dict:
        result = {
            "outcome": outcome,
            "attempts": attempts,
            "started_pass_count": started["pass_count"],
            "final_pass_count": pass_count(
                testing_module.load_report(project)),
            "note": note,
        }
        _write_report_file(project, result, started)
        return result

    if report.get("ok"):
        return finish("already_passing",
                      "The latest test report already passes — nothing to repair.")
    if max_attempts <= 0:
        return finish("escalated",
                      "Self-repair is set to 0 attempts in the global "
                      "settings, so the failure is handed to the user.")

    vcs_module.init_repo(project)
    vcs_module.ensure_local_identity(project)
    patch_module._commit_quietly(project, "Weaverbird repair: starting state")
    best_sha = patch_module._git_head(project)
    best_count = started["pass_count"]

    for number in range(1, max_attempts + 1):
        current = testing_module.load_report(project) or report
        failures = classify_failures(current)
        if current.get("ok"):
            break
        if not failures:
            attempts.append({"attempt": number, "outcome": "escalated",
                             "detail": "failures could not be classified"})
            break
        # One strategy per failure class this attempt. A strategy
        # may be applied again on a later attempt — easing compounds
        # (each try moves the numbers another bounded step), and the
        # keep-best rule below is what stops a useless repetition
        # from sticking.
        plan: list[tuple[dict, str]] = []
        blocked: list[str] = []
        for failure in failures:
            offered = list(STRATEGY_MENU.get(failure["class"], []))
            if not offered:
                blocked.append(failure["class"])
                continue
            choice = _brain_choose(project, failure, offered,
                                   settings_path=settings_path) or offered[0]
            plan.append((failure, choice))
        if not plan:
            attempts.append({
                "attempt": number, "outcome": "escalated",
                "detail": "no strategy exists for: "
                          + ", ".join(blocked or [f["class"] for f in failures])})
            break

        if reporter is not None:
            reporter(
                f"Repair attempt {number} of {max_attempts}: "
                + "; ".join(f"{f['class']} → {s}" for f, s in plan),
                done=number - 1, total=max_attempts)
        patch_module._commit_quietly(
            project, f"Weaverbird repair checkpoint before attempt {number}")
        checkpoint_sha = patch_module._git_head(project)
        tag = f"repair/attempt-{number}"
        if tag not in vcs_module.list_tags(project) and checkpoint_sha:
            try:
                vcs_module.tag_known_good(project, tag)
            except vcs_module.VcsError:
                pass

        config = {}
        archetype = ""
        try:
            config = patch_module.load_config(project)
            game_plan = config.get("plan")
            if isinstance(game_plan, dict) and game_plan.get("movement"):
                archetype = str(game_plan["movement"])
            else:
                archetype = config.get("archetype", "")
        except patch_module.PatchError:
            archetype = (generate_module.load_report(project) or {}).get(
                "archetype", "")

        applied: list[str] = []
        strategy_errors: list[str] = []
        for failure, strategy in plan:
            func = _STRATEGY_FUNCS[strategy]
            try:
                if strategy == "regenerate_game":
                    note = func(project, config, archetype,
                                settings_path=settings_path)
                else:
                    note = func(project, config, archetype)
                applied.append(f"{failure['class']} → {strategy}: {note}")
                try:
                    config = patch_module.load_config(project)
                except patch_module.PatchError:
                    pass
            except (RepairError, assets_module.AssetsError,
                    generate_module.GenerationError) as exc:
                strategy_errors.append(f"{failure['class']} → {strategy}: {exc}")

        if not applied:
            attempts.append({
                "attempt": number, "outcome": "no_change",
                "strategies": [s for _f, s in plan],
                "detail": "; ".join(strategy_errors)})
            if checkpoint_sha:
                vcs_module.reset_to_commit(project, checkpoint_sha)
            continue

        new_report = testing_module.run_tests(
            project, godot_path=godot_path, settings_path=settings_path,
            allow_repair=False, reporter=reporter)
        new_count = pass_count(new_report)
        resolved = [f["class"] for f in failures
                    if f["class"] not in
                    {x["class"] for x in classify_failures(new_report)}]
        keep = new_count > best_count or (
            resolved and new_count >= best_count)
        attempts.append({
            "attempt": number,
            "outcome": "kept" if keep else "restored",
            "strategies": applied,
            "strategy_errors": strategy_errors,
            "pass_count_before": best_count,
            "pass_count_after": new_count,
            "resolved": resolved,
            "failures": [f["class"] for f in failures],
        })
        if reporter is not None:
            reporter(
                f"Repair attempt {number}: "
                f"{'kept' if keep else 'restored'} — pass count "
                f"{best_count} → {new_count}",
                done=number, total=max_attempts)
        if new_report.get("ok"):
            patch_module._commit_quietly(
                project, f"Weaverbird repair: attempt {number} fixed the suite")
            if "known-good/repair" not in vcs_module.list_tags(project):
                try:
                    vcs_module.tag_known_good(project, "known-good/repair")
                except vcs_module.VcsError:
                    pass
            return finish("repaired",
                          f"Attempt {number} made the test suite pass.")
        if keep:
            best_count = new_count
            patch_module._commit_quietly(
                project, f"Weaverbird repair: attempt {number} (kept, improving)")
            best_sha = patch_module._git_head(project) or best_sha
        else:
            if checkpoint_sha:
                vcs_module.reset_to_commit(project, checkpoint_sha)
            # The report on disk now describes the restored (worse)
            # attempt's game; re-run cheaply? No — restore the report
            # that matches the checkpoint by re-testing is expensive.
            # Instead note it: the next attempt re-reads the report
            # the loop re-runs at its start... but the file is stale.
            # Re-run the suite on the restored tree so records match.
            testing_module.run_tests(
                project, godot_path=godot_path,
                settings_path=settings_path, allow_repair=False,
                reporter=reporter)

    # Exhausted: restore the best state seen, never worse than start.
    current_report = testing_module.load_report(project)
    if not (current_report and current_report.get("ok")):
        if best_sha and patch_module._git_head(project) != best_sha:
            vcs_module.reset_to_commit(project, best_sha)
            testing_module.run_tests(
                project, godot_path=godot_path,
                settings_path=settings_path, allow_repair=False,
                reporter=reporter)
    final_report = testing_module.load_report(project)
    if final_report and final_report.get("ok"):
        if "known-good/repair" not in vcs_module.list_tags(project):
            try:
                vcs_module.tag_known_good(project, "known-good/repair")
            except vcs_module.VcsError:
                pass
        return finish("repaired", "The test suite passes.")
    blocking = classify_failures(final_report)
    top = blocking[0]["class"] if blocking else "unknown"
    return finish(
        "escalated",
        f"Repair attempts exhausted ({max_attempts}). The project was "
        f"restored to its best checkpoint and needs the user. Top "
        f"blocking failure: {top}.")


def _write_report_file(project: Path, result: dict, started: dict) -> None:
    lines = [
        "# Self-repair report",
        "",
        f"Outcome: **{result['outcome']}** — {result['note']}",
        "",
        f"Pass count when repair started: {started['pass_count']} "
        f"(base runs ok + win proven + lose proven + performance ok).",
        f"Pass count at the end: {result['final_pass_count']}.",
        "",
        "## Failures found at the start",
        "",
    ]
    if started["failures"]:
        for failure in started["failures"]:
            lines.append(f"- {failure['class']} — {failure.get('detail', '')}")
    else:
        lines.append("- none")
    lines += ["", "## Attempts", ""]
    if result["attempts"]:
        for attempt in result["attempts"]:
            lines.append(
                f"### Attempt {attempt['attempt']} — {attempt['outcome']}")
            for strategy in attempt.get("strategies", []):
                lines.append(f"- {strategy}")
            for error in attempt.get("strategy_errors", []):
                lines.append(f"- could not apply: {error}")
            if "pass_count_after" in attempt:
                lines.append(
                    f"- pass count {attempt['pass_count_before']} → "
                    f"{attempt['pass_count_after']}; resolved: "
                    + (", ".join(attempt.get("resolved", [])) or "none"))
            if attempt.get("detail"):
                lines.append(f"- {attempt['detail']}")
            lines.append("")
    else:
        lines.append("No attempts were made.")
        lines.append("")
    if result["outcome"] == "escalated":
        lines += [
            "## Needs the user",
            "",
            result["note"],
            "",
            "The factory stopped rather than guess: unknown failure "
            "signatures and exhausted strategies are escalated with "
            "the record above, and the game was left at its best "
            "checkpoint — never worse than when repair started.",
            "",
        ]
    lines.append(f"_Written {datetime.now(timezone.utc).isoformat(timespec='seconds')}._")
    lines.append("")
    (project / REPORT_NAME).write_text("\n".join(lines), encoding="utf-8")
