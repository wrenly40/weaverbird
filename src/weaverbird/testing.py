"""The headless test stage — pipeline stages "headless_test" and
the scripted half of "playtest".

Runs a generated game through a real suite in Godot, headless, and
writes ``.weaverbird/test_report.json`` — the evidence file the
finish check reads (it takes precedence over the skeleton report).

The suite:

- Base runs: import, main (menu) scene, game scene. Any Godot
  ERROR line fails them.
- Scripted players, shipped inside the generated game under
  ``game/tests/`` and driven through the game's real input and UI:
  a **competent** player who plays to a win and then, separately,
  to a loss (both through the game's real logic — steering and
  solution replay, never state forgery); a **breaker** who fuzzes
  inputs, spams pause and (in the crate game) shoves a crate into
  a corner to check the game notices; and a **first-time** player
  who starts from the menu's real Play button and follows only the
  on-screen hint. These are deterministic scripted players, not
  model-driven agents — the report says so. The model-driven
  playtester lives in modelplayer.py: it plays the same games
  through the bridge the build stage ships in game/tests/,
  deciding from screenshots and the game's honestly exposed state.
- Performance: during the competent win run, the player samples the
  loop's average FPS and the game-scene load time. The FPS target
  defaults to 30 and can be set in the global settings under
  ``testing: {target_fps: ...}``.

Stage rule: the run fails (the pipeline does not advance) when any
base run fails or the win/lose proofs fail. Breaker findings are
warnings unless a crash or an ERROR line occurred.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from . import brain as brain_module
from . import godot as godot_module
from . import ledger as ledger_module
from . import skeleton as skeleton_module
from . import state as state_module

GAME_DIR_NAME = skeleton_module.GAME_DIR_NAME
REPORT_NAME = "test_report.json"
DEFAULT_TARGET_FPS = 30

_WBTEST_RE = re.compile(r"^WBTEST (\{.*\})\s*$", re.MULTILINE)


class TestingError(Exception):
    """Raised when the test stage cannot run at all."""


def report_path_for(project_dir) -> Path:
    return Path(project_dir) / ".weaverbird" / REPORT_NAME


def load_report(project_dir):
    path = report_path_for(project_dir)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def _target_fps(project, settings_path=None) -> int:
    _, data = brain_module.resolve_settings(settings_path, project)
    if isinstance(data, dict):
        testing = data.get("testing")
        if isinstance(testing, dict):
            value = testing.get("target_fps")
            if isinstance(value, (int, float)) and value > 0:
                return int(value)
    return DEFAULT_TARGET_FPS


def _run(godot_bin: str, game_dir: Path, args: list, timeout: int) -> dict:
    command = [godot_bin, "--headless", "--path", str(game_dir), *args]
    started = time.monotonic()
    try:
        result = subprocess.run(command, capture_output=True, text=True,
                                timeout=timeout)
        output = (result.stdout or "") + "\n" + (result.stderr or "")
        returncode = result.returncode
    except subprocess.TimeoutExpired:
        output = ""
        returncode = None
    duration = round(time.monotonic() - started, 2)
    errors = skeleton_module._error_digest(output)
    if returncode is None:
        errors = [f"Timed out after {timeout} seconds."] + errors
    wbtest = None
    matches = _WBTEST_RE.findall(output)
    if matches:
        try:
            wbtest = json.loads(matches[-1])
        except json.JSONDecodeError:
            wbtest = None
    return {
        "returncode": returncode,
        "duration_seconds": duration,
        "errors": errors,
        "wbtest": wbtest,
    }


def _units_from_details(win_details, first_details) -> list[dict]:
    """Aggregate the bots' per-unit records into the report's
    ``units`` list: [{unit, source, completed, deaths, time_s,
    ...}] with source "competent" (the win run) or "first_time".

    Bots that predate per-unit recording simply contribute
    nothing — the balance lab reads an empty list as "re-run the
    tests with a current build", never as invented numbers.
    """
    units: list[dict] = []
    for source, details in (("competent", win_details),
                            ("first_time", first_details)):
        for record in (details or {}).get("units") or []:
            if not isinstance(record, dict) \
                    or not isinstance(record.get("unit"), (int, float)):
                continue
            entry = {
                "unit": int(record["unit"]),
                "source": source,
                "completed": bool(record.get("completed")),
            }
            for key in ("deaths", "time_s", "moves_used", "move_budget"):
                value = record.get(key)
                if isinstance(value, (int, float)):
                    entry[key] = value
            units.append(entry)
    units.sort(key=lambda u: (u["unit"], u["source"]))
    return units


def run_tests(project_dir, godot_path=None, settings_path=None,
              allow_repair: bool = True, reporter=None) -> dict:
    """Run the full suite for a project. Returns the test report.

    The report is written even when the suite fails — what failed
    is evidence too. Raises TestingError only when the stage cannot
    run at all (no game, no test scripts, no Godot).

    When the suite fails and a global settings file gives
    ``repair.max_attempts`` > 0 (default 3 when the section exists),
    the self-repair loop runs once at the end and the report that
    comes back is the post-repair one. Callers inside the repair
    loop pass allow_repair=False — repair never recurses.
    """
    project = Path(project_dir)
    game_dir = project / GAME_DIR_NAME
    if not (game_dir / "project.godot").exists():
        raise TestingError(
            "No game found — build the skeleton and the game first; "
            "there is nothing to test yet."
        )
    tests_dir = game_dir / "tests"
    needed = ["competent_win.gd", "competent_lose.gd", "breaker.gd",
              "first_time.gd"]
    missing = [name for name in needed if not (tests_dir / name).exists()]
    if missing:
        raise TestingError(
            "The generated game has no scripted players yet "
            f"(missing tests/{', tests/'.join(missing)}). Run the build "
            "stage first: 'weaverbird build game <dir>'."
        )
    godot_bin, version = godot_module.find_godot(godot_path)
    if not godot_bin:
        raise TestingError(
            "Godot was not found, so the test stage cannot run. "
            "Install Godot 4 or set WEAVERBIRD_GODOT to its path."
        )
    target_fps = _target_fps(project, settings_path)

    runs: list[dict] = []
    total_runs = 7  # 3 base runs + 4 scripted players
    steps_done = 0

    def note_run(entry: dict) -> None:
        nonlocal steps_done
        steps_done += 1
        if reporter is not None:
            reporter(
                f"Run {steps_done} of {total_runs}: {entry['name']} — "
                f"{'ok' if entry['ok'] else 'FAILED'}",
                done=steps_done, total=total_runs)

    def base_run(name: str, args: list, timeout: int = 120) -> dict:
        result = _run(godot_bin, game_dir, args, timeout)
        entry = {
            "name": name, "kind": "base",
            "ok": result["returncode"] == 0 and not result["errors"],
            "errors": result["errors"],
            "duration_s": result["duration_seconds"],
            "details": {},
        }
        runs.append(entry)
        note_run(entry)
        return entry

    def player_run(name: str, script: str, timeout: int) -> dict:
        result = _run(godot_bin, game_dir, ["--script", script], timeout)
        wb = result["wbtest"] or {}
        details = {k: v for k, v in wb.items()
                   if k not in ("player", "mode", "ok")}
        entry = {
            "name": name, "kind": "scripted",
            "player": wb.get("player"), "mode": wb.get("mode"),
            "ok": (result["returncode"] == 0 and not result["errors"]
                   and bool(wb.get("ok"))),
            "errors": result["errors"]
                      or ([wb.get("reason")] if wb and not wb.get("ok")
                          and wb.get("reason") else []),
            "duration_s": result["duration_seconds"],
            "details": details,
        }
        runs.append(entry)
        note_run(entry)
        return entry

    if reporter is not None:
        reporter(f"Running the headless test suite ({total_runs} runs: "
                 "import, menu, game scene, then the scripted players)",
                 done=0, total=total_runs)
    base_run("import", ["--import"])
    base_run("main scene", ["--quit-after", "45"])
    base_run("game scene", ["res://scenes/game.tscn", "--quit-after", "45"])
    win = player_run("competent win", "tests/competent_win.gd", timeout=420)
    lose = player_run("competent lose", "tests/competent_lose.gd", timeout=300)
    breaker = player_run("breaker", "tests/breaker.gd", timeout=240)
    first = player_run("first-time", "tests/first_time.gd", timeout=420)

    base_ok = all(r["ok"] for r in runs if r["kind"] == "base")
    win_proven = bool(win["ok"])
    lose_proven = bool(lose["ok"])
    # "No crashes" = every run completed its course with no Godot
    # errors — a run that quit(1) because its player failed to win
    # or lost is not crash-free evidence either.
    no_crashes = all(r["ok"] for r in runs)
    scenarios_pass = base_ok and win_proven and lose_proven

    warnings: list[str] = []
    for entry in (breaker, first):
        for finding in (entry["details"].get("findings") or []):
            warnings.append(f"{entry['name']}: {finding}")
        if not entry["ok"] and entry["name"] == "breaker":
            warnings.append(
                "breaker run reported a problem (see its run entry)")

    win_details = win["details"]
    avg_fps = win_details.get("fps_avg")
    load_ms = win_details.get("load_ms")
    performance = {
        "avg_fps": avg_fps,
        "load_time_s": (round(load_ms / 1000.0, 3)
                        if isinstance(load_ms, (int, float)) and load_ms
                        else None),
        "target_fps": target_fps,
        "ok": bool(isinstance(avg_fps, (int, float))
                   and avg_fps >= target_fps),
    }

    generation = None
    gen_path = project / ".weaverbird" / "generation_report.json"
    if gen_path.exists():
        try:
            generation = json.loads(gen_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            generation = None

    report = {
        "stage": "headless_test",
        "ran_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "godot_version": version,
        "archetype": generation.get("archetype") if generation else None,
        "scripted_players": True,
        "runs": runs,
        "scenarios_pass": scenarios_pass,
        "no_crashes": no_crashes,
        "winnable": win_proven,
        "losable": lose_proven,
        "win_proven": win_proven,
        "lose_proven": lose_proven,
        "performance": performance,
        "playtesters": {
            "competent": {
                "win": win["details"], "lose": lose["details"],
                "ok": win["ok"] and lose["ok"],
            },
            "breaker": {**breaker["details"], "ok": breaker["ok"]},
            "first_time": {**first["details"], "ok": first["ok"]},
        },
        "units": _units_from_details(win["details"], first["details"]),
        "warnings": warnings,
        "ok": scenarios_pass,
    }
    path = report_path_for(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    ledger_module.add_entry(project, "headless_test", "cheap", 0, 0.0)
    if reporter is not None:
        reporter("Test report written — verdict: "
                 + ("PASS" if scenarios_pass else "FAIL"))

    if allow_repair and not report.get("ok"):
        _settings_file, data = brain_module.resolve_settings(
            settings_path, project)
        attempts = None
        if isinstance(data, dict):
            section = data.get("repair")
            if isinstance(section, dict) and "max_attempts" in section:
                attempts = section.get("max_attempts")
            elif isinstance(section, dict):
                attempts = 3
        if isinstance(attempts, (int, float)) and attempts > 0:
            from . import repair as repair_module  # lazy: no cycle
            if reporter is not None:
                reporter("The suite failed — handing over to self-repair")
            try:
                outcome = repair_module.run_repair(
                    project, settings_path=settings_path,
                    godot_path=godot_path, max_attempts=int(attempts),
                    reporter=reporter)
            except repair_module.RepairError:
                outcome = None
            if outcome and outcome.get("outcome") == "repaired":
                refreshed = load_report(project)
                if refreshed is not None:
                    return refreshed
    return report


def digest_lines(report: dict) -> list[str]:
    """The human-readable verdict: one line per run, then a summary."""
    lines: list[str] = []
    for run in report.get("runs", []):
        mark = "✓" if run.get("ok") else "✗"
        extra = ""
        details = run.get("details", {})
        if run.get("kind") == "scripted":
            bits = []
            if details.get("time_s") is not None:
                bits.append(f"{details['time_s']}s of play")
            if details.get("deaths"):
                bits.append(f"{details['deaths']} deaths")
            if details.get("first_objective_s") is not None \
                    and details["first_objective_s"] >= 0:
                bits.append(
                    f"first objective at {details['first_objective_s']}s")
            if details.get("finished") is not None:
                bits.append("finished" if details["finished"]
                            else "did not finish")
            extra = " — " + ", ".join(bits) if bits else ""
        lines.append(
            f"{mark} {run.get('name')} ({run.get('duration_s', '?')}s"
            f" wall){extra}")
        for error in run.get("errors", [])[:3]:
            lines.append(f"    error: {error}")
    perf = report.get("performance", {})
    lines.append(
        f"Performance: {perf.get('avg_fps', '?')} avg FPS against a "
        f"{perf.get('target_fps', '?')} target; game scene loaded in "
        f"{perf.get('load_time_s', '?')}s.")
    lines.append(
        f"Win proven: {'yes' if report.get('win_proven') else 'no'} · "
        f"Lose proven: {'yes' if report.get('lose_proven') else 'no'}")
    for warning in report.get("warnings", []):
        lines.append(f"Warning: {warning}")
    lines.append(
        "Verdict: " + ("PASS — every base run clean, win and lose proven."
                         if report.get("ok") else
                         "FAIL — see the failing runs above. Full report: "
                         ".weaverbird/test_report.json"))
    return lines


# ---------------------------------------------------------------------------
# Pipeline wiring

def headless_test_stage_handler(project_dir, state=None) -> bool:
    """Leaving headless_test needs a passing test report on record."""
    project = Path(project_dir)
    report = load_report(project)
    if report and report.get("ok"):
        return True
    report = run_tests(project)
    if not report.get("ok"):
        raise TestingError(
            "The headless test suite failed — the pipeline stays at "
            "headless_test. " + " ".join(digest_lines(report)[-2:]))
    return True


def playtest_stage_handler(project_dir, state=None) -> bool:
    """Leaving playtest needs the scripted players' reports, which
    the test stage produces; run the suite if they are missing."""
    project = Path(project_dir)
    report = load_report(project)
    if report and report.get("playtesters"):
        return True
    report = run_tests(project)
    if not report.get("playtesters"):
        raise TestingError("The test suite produced no playtest reports.")
    return True


def register_stage_handlers() -> None:
    state_module.register_handler("headless_test", headless_test_stage_handler)
    state_module.register_handler("playtest", playtest_stage_handler)
