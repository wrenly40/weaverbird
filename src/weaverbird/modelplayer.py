"""The model-driven playtester — the fourth player, on top of the
scripted three.

Where the scripted players are deterministic bots, this one plays
the way the platform always intended a "real play test" to work: a
vision model (bring-your-own-key, like every Weaverbird model call)
looks at a screenshot of the game, reads the honestly-exposed game
state, and chooses one action at a time.

The contract between the factory and the game is the bridge
(``game/tests/model_bridge.gd``, shipped by the build stage): with
``--model-bridge`` the game writes ``bridge/state.json`` and
``bridge/shot.png`` every decision tick, pauses, and applies the
action the driver writes to ``bridge/action.json`` as real input.

This module is the driver: it launches the game under a virtual
display (screenshots need a renderer — Xvfb), loops
state → model → action, stops at the win/lose screens, the
decision cap, or three consecutive invalid decisions, and writes
``.weaverbird/model_playtest.json`` plus a "model" entry in the
test report's playtesters. Every confusion the model flags (and
every stuck loop the driver detects) is also filed as a feedback
item, source "model-playtest", category confusion — the model's
confusion becomes the feedback loop's input, by design.

Without a usable vision tier configured, the command says exactly
what is missing and exits without touching the game.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from . import brain as brain_module
from . import feedback as feedback_module
from . import generate as generate_module
from . import godot as godot_module
from . import patch as patch_module
from . import scope as scope_module
from . import skeleton as skeleton_module

GAME_DIR_NAME = skeleton_module.GAME_DIR_NAME
REPORT_NAME = "model_playtest.json"
DEFAULT_MAX_DECISIONS = 40
DEFAULT_TIER = "mid"
DECISION_TIMEOUT = 120  # seconds per decision before "wait" is forced
STATE_WAIT_TIMEOUT = 180  # seconds to wait for the game's next state

ACTION_SETS = {
    "arena_collector": ["move_left", "move_right", "move_up",
                        "move_down", "pause", "wait", "start"],
    "platformer": ["move_left", "move_right", "jump", "pause",
                   "wait", "start"],
    "crate_pusher": ["move_left", "move_right", "move_up", "move_down",
                     "interact", "wait", "start"],
}


class ModelPlayerError(Exception):
    """Raised when the model playtest cannot run at all."""


def report_path_for(project_dir) -> Path:
    return Path(project_dir) / ".weaverbird" / REPORT_NAME


def settings_for(project, settings_path=None) -> tuple[str, int]:
    """(tier, max_decisions) from the global settings' playtest
    section, with the documented defaults."""
    _file, data = brain_module.resolve_settings(settings_path, project)
    tier, cap = DEFAULT_TIER, DEFAULT_MAX_DECISIONS
    if isinstance(data, dict):
        section = data.get("playtest")
        if isinstance(section, dict):
            if isinstance(section.get("model_tier"), str) \
                    and section["model_tier"] in brain_module.TIERS:
                tier = section["model_tier"]
            value = section.get("model_max_decisions")
            if isinstance(value, (int, float)) and value > 0:
                cap = int(value)
    return tier, cap


def missing_brain_message(project, tier: str, settings_path=None) -> str | None:
    """None when the tier is usable; otherwise the exact, plain
    description of what is missing."""
    settings_file, data = brain_module.resolve_settings(settings_path, project)
    if not settings_file:
        return (f"no global settings file found — create one and set a "
                f"vision-capable model on the {tier} tier (models.{tier} "
                "with provider, model, and the api_key_env variable set). "
                "No model was called and the game was not touched.")
    status = brain_module.tier_status(data, tier)
    if status["usable"]:
        return None
    return (f"the {tier} tier is not usable: {status['reason']} "
            f"The model playtester needs the {tier} tier (set "
            f"playtest.model_tier to choose another) configured with a "
            "vision-capable model — one that accepts image input "
            "(OpenAI-compatible or Anthropic), with its key environment "
            "variable set. No model was called and the game was not touched.")


def _progress_key(state: dict) -> str:
    parts = [str(state.get("scene", ""))]
    for key in ("collected_this_round", "total_collected", "stars",
                "crates_on_pads", "moves_used", "round", "level",
                "puzzle", "player_pos", "player_cell"):
        if key in state:
            parts.append(f"{key}={state[key]}")
    return "|".join(parts)


def _default_brain_fn(project, tier, settings_path):
    def call(prompt: str, image_path, _state) -> str:
        result = brain_module.complete_with_image(
            project, tier, prompt, image_path, stage="playtest",
            settings_path=settings_path, max_tokens=220)
        if not result.ok:
            raise ModelPlayerError(result.error)
        return result.text
    return call


def run_model_playtest(project_dir, settings_path=None, godot_path=None,
                       max_decisions: int | None = None,
                       tier: str | None = None, brain_fn=None,
                       decision_timeout: int = DECISION_TIMEOUT,
                       reporter=None) -> dict:
    """Play one run. Returns the outcome dict (also written to
    .weaverbird/model_playtest.json).

    ``brain_fn(prompt, image_path, state) -> reply text`` is the
    seam tests use to drive the bridge with a scripted policy; the
    default calls the configured vision tier through brain.py.
    Raises ModelPlayerError when the run cannot start (no game, no
    bridge, no display) or the brain is not configured.
    ``reporter`` (optional) narrates progress for the job runner.
    """
    project = Path(project_dir)
    game_dir = project / GAME_DIR_NAME
    if not (game_dir / "project.godot").exists():
        raise ModelPlayerError(
            "No game found — build the skeleton and the game first.")
    if not (game_dir / "tests" / "model_bridge.gd").exists():
        raise ModelPlayerError(
            "The game has no model bridge (tests/model_bridge.gd) — "
            "rebuild the game with the build stage so the bridge ships "
            "with it, then try again.")
    settings_tier, settings_cap = settings_for(project, settings_path)
    tier = tier or settings_tier
    if max_decisions is None:
        max_decisions = settings_cap
    if brain_fn is None:
        missing = missing_brain_message(project, tier, settings_path)
        if missing:
            raise ModelPlayerError(missing)
        brain_fn = _default_brain_fn(project, tier, settings_path)

    godot_bin, _version = godot_module.find_godot(godot_path)
    if not godot_bin:
        raise ModelPlayerError("Godot was not found, so the game "
                               "cannot be launched.")
    use_xvfb = not os.environ.get("DISPLAY")
    if use_xvfb and not shutil.which("xvfb-run"):
        raise ModelPlayerError(
            "No display is available and xvfb-run was not found — the "
            "model playtester needs a rendering display to take "
            "screenshots. Run it on a machine with a display or Xvfb.")

    gen_report = generate_module.load_report(project) or {}
    archetype = gen_report.get("archetype") or ""
    scope_values = {}
    scope_file = scope_module.scope_path_for(project)
    if scope_file.exists():
        scope_values = {f.name: f.value
                        for f in scope_module.load_scope(scope_file).fields}
    title = scope_values.get("Title (working)", project.name)

    user_dir = patch_module.userdata_dir(project)
    if user_dir is None:
        raise ModelPlayerError("Could not work out the game's data "
                               "folder from project.godot.")
    bridge_dir = user_dir / "wb_bridge"
    bridge_dir.mkdir(parents=True, exist_ok=True)
    for stale in ("state.json", "action.json", "shot.png"):
        (bridge_dir / stale).unlink(missing_ok=True)

    work_dir = project / ".weaverbird" / "model_playtest"
    shots_dir = work_dir / "shots"
    shots_dir.mkdir(parents=True, exist_ok=True)
    log_path = work_dir / "godot.log"

    command = [godot_bin, "--path", str(game_dir), "--", "--model-bridge"]
    if use_xvfb:
        command = ["xvfb-run", "-a", "-s", "-screen 0 1280x800x24"] + command
    if reporter is not None:
        reporter(f"Model playtest started — the {tier} tier plays "
                 f"“{title}” from screenshots (up to {max_decisions} "
                 "decisions)", done=0, total=max_decisions)
    log_file = open(log_path, "w", encoding="utf-8")
    process = subprocess.Popen(
        command, stdout=log_file, stderr=subprocess.STDOUT,
        start_new_session=True)

    transcript: list[dict] = []
    confusion_notes: list[str] = []
    decisions = 0
    invalid_streak = 0
    outcome = "cap"
    finished = False
    result_kind = ""
    started = time.monotonic()
    last_decision_seen = 0
    progress_history: list[str] = []

    def write_action(action: str) -> None:
        tmp = bridge_dir / "action.json.tmp"
        tmp.write_text(json.dumps({"action": action}), encoding="utf-8")
        tmp.replace(bridge_dir / "action.json")

    try:
        while decisions < max_decisions:
            # Wait for the bridge's next state.
            state = None
            wait_started = time.monotonic()
            while time.monotonic() - wait_started < STATE_WAIT_TIMEOUT:
                if process.poll() is not None:
                    break
                state_path = bridge_dir / "state.json"
                if state_path.exists():
                    try:
                        candidate = json.loads(
                            state_path.read_text(encoding="utf-8"))
                    except (json.JSONDecodeError, OSError):
                        candidate = None
                    if candidate and int(candidate.get("decision", 0)) \
                            > last_decision_seen:
                        state = candidate
                        break
                time.sleep(0.2)
            if state is None:
                outcome = "game_exited" if process.poll() is not None \
                    else "stuck"
                break
            last_decision_seen = int(state.get("decision", 0))
            scene = str(state.get("scene", ""))
            if scene.endswith("win.tscn"):
                finished, result_kind, outcome = True, "win", "finished"
                break
            if scene.endswith("game_over.tscn"):
                finished, result_kind, outcome = True, "lose", "finished"
                break

            actions = state.get("actions") or ACTION_SETS.get(
                archetype, ["wait", "start"])
            shot_src = bridge_dir / "shot.png"
            shot_name = f"shot-{last_decision_seen:03d}.png"
            shot_dest = shots_dir / shot_name
            if shot_src.exists():
                shutil.copyfile(shot_src, shot_dest)
            hint = state.get("hint", "")
            prompt = (
                f"You are playtesting a game called “{title}”. "
                f"Objective hint shown in the game: {hint or 'none'}\n"
                "The screenshot shows the game right now. The game's "
                "own state (positions, counts, lives, timer) is:\n"
                + json.dumps({k: v for k, v in state.items()
                              if k != "actions"}, indent=1)
                + "\nChoose exactly one action from: "
                + ", ".join(actions)
                + '.\nAnswer with one JSON object only: {"action": '
                '"<one of them>", "reason": "<short>", "confused": '
                '<true|false>, "note": "<what is unclear, if anything>"}'
            )
            decision_started = time.monotonic()
            reply = ""
            timed_out = False
            try:
                reply = brain_fn(prompt, shot_dest if shot_dest.exists()
                                 else shot_src, state) or ""
            except ModelPlayerError:
                raise
            except Exception as exc:  # a failing brain ends the run plainly
                raise ModelPlayerError(
                    f"The model call failed: {type(exc).__name__}: {exc}")
            if time.monotonic() - decision_started > decision_timeout:
                timed_out = True
            parsed = brain_module.extract_json(reply)
            action = str(parsed.get("action", ""))
            reason = str(parsed.get("reason", ""))[:200]
            confused = bool(parsed.get("confused"))
            note = str(parsed.get("note", ""))[:200]
            if timed_out:
                action = "wait"
                reason = reason or "decision timed out — waited"
                invalid_streak += 1
            elif action not in actions:
                invalid_streak += 1
                reason = (f"model chose {action!r}, not in the action "
                          f"set — waited instead")
                action = "wait"
            else:
                invalid_streak = 0
            decisions += 1
            if confused and note:
                confusion_notes.append(note)
            progress_history.append(_progress_key(state))
            if len(progress_history) >= 5 and \
                    len(set(progress_history[-5:])) == 1:
                stuck_note = ("no progress for 5 decisions at "
                              + (scene or "the current screen"))
                if stuck_note not in confusion_notes:
                    confusion_notes.append(stuck_note)
            transcript.append({
                "decision": last_decision_seen,
                "scene": scene,
                "action": action,
                "reason": reason,
                "confused": confused,
                "note": note,
                "shot": shot_name if shot_dest.exists() else "",
                "state": {k: v for k, v in state.items()
                          if k not in ("actions", "decision")},
            })
            if reporter is not None and decisions % 5 == 0:
                reporter(f"Decision {decisions} of up to "
                         f"{max_decisions}: {action}"
                         + (f" — confused: {note}" if confused and note
                            else ""),
                         done=decisions, total=max_decisions)
            if invalid_streak >= 3:
                outcome = "invalid"
                write_action("wait")
                break
            write_action(action)
        else:
            outcome = "cap"
    finally:
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(process.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        log_file.close()

    elapsed = round(time.monotonic() - started, 1)
    deaths = 0
    for entry in transcript:
        state_deaths = entry["state"].get("deaths")
        if isinstance(state_deaths, (int, float)):
            deaths = max(deaths, int(state_deaths))
    if result_kind == "lose":
        deaths = max(deaths, 1)

    record = {
        "ran_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "tier": tier,
        "archetype": archetype,
        "outcome": outcome,
        "finished": finished,
        "result": result_kind,
        "decisions": decisions,
        "max_decisions": max_decisions,
        "deaths": deaths,
        "time_s": elapsed,
        "confusion_notes": confusion_notes,
        "transcript": transcript,
    }
    path = report_path_for(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")

    # Fold the run into the test report's playtesters, when one is
    # on record (the test stage owns the report's other sections).
    from . import testing as testing_module
    test_report = testing_module.load_report(project)
    if test_report is not None:
        test_report.setdefault("playtesters", {})["model"] = {
            "finished": finished,
            "result": result_kind,
            "decisions": decisions,
            "deaths": deaths,
            "time_s": elapsed,
            "confusion": confusion_notes,
            "ok": finished,
        }
        testing_module.report_path_for(project).write_text(
            json.dumps(test_report, indent=2) + "\n", encoding="utf-8")

    # Confusion becomes feedback — the designed loop.
    for note in confusion_notes:
        _item, created = feedback_module.add_item(
            project, source="model-playtest",
            text=f"Model playtester was confused: {note} "
                 f"(in “{title}”, after {decisions} decisions)")
        if created:
            feedback_module.update_item(
                project, _item["id"], category="confusion",
                status="triaged", cluster="model playtest")
    if reporter is not None:
        reporter(f"Model playtest ended: {outcome} after {decisions} "
                 f"decisions, {deaths} deaths",
                 done=decisions, total=max_decisions)
    return record
