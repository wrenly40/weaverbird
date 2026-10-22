"""The patch loop — pipeline stage 10's back half.

An approved feedback item becomes a shipped patch, or it becomes
an honest plan. The rules:

- Only approved items, and only in-scope categories: **balance**
  (tune the numbers in game_config.json), **confusion** (rewrite
  the objective hint), and **bug** when the failure matches a known
  class with a known remedy (missing assets → regenerate them; a
  parse error in a generated script → regenerate the game).
  Anything else — requests, price, scope — is the maker's decision,
  never the factory's. A bug with no known remedy produces
  PATCH_PLAN.md and stops: plan-only is a valid outcome.
- Every applied patch must pass two gates before it ships: the
  full headless test stage, and a save-compatibility check — a
  save captured from the CURRENT build (through the game's own
  save system) must load in the patched build with its stats
  intact, proven by the game's shipped tests/savecheck.gd.
- Fail either gate and the patch is rejected: the working tree is
  restored to the pre-patch checkpoint and the item goes back to
  approved with the failure recorded.

A shipped patch bumps the game's version (game/VERSION, semver
patch increment), appends CHANGELOG.md, commits and tags
``known-good/patch-<version>``, and marks the item patched.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timezone
from pathlib import Path

from . import assets_gen as assets_module
from . import brain as brain_module
from . import feedback as feedback_module
from . import generate as generate_module
from . import godot as godot_module
from . import scope as scope_module
from . import skeleton as skeleton_module
from . import testing as testing_module
from . import vcs as vcs_module

GAME_DIR_NAME = skeleton_module.GAME_DIR_NAME
VERSION_NAME = "VERSION"
DEFAULT_VERSION = "0.1.0"
PATCH_LOG_NAME = "patch_log.json"
PLAN_NAME = "PATCH_PLAN.md"
SNAPSHOT_DIR = "save_snapshot"

# Patch-time bounds: generate.py's BOUNDS plus the fields a balance
# patch may touch that generation does not tune.
EXTRA_BOUNDS = {
    "round_seconds": (30.0, 240.0),
    "lives": (1, 9),
}


class PatchError(Exception):
    """Raised when a patch is refused before it starts."""


# ---------------------------------------------------------------------------
# The game's version + changelog + patch log

def version_path_for(project_dir) -> Path:
    return Path(project_dir) / GAME_DIR_NAME / VERSION_NAME


def game_version(project_dir) -> str:
    path = version_path_for(project_dir)
    if path.exists():
        text = path.read_text(encoding="utf-8").strip()
        if re.fullmatch(r"\d+\.\d+\.\d+", text):
            return text
    return DEFAULT_VERSION


def bump_version(version: str) -> str:
    major, minor, patch = (int(part) for part in version.split("."))
    return f"{major}.{minor}.{patch + 1}"


def _append_changelog(project: Path, version: str, item: dict,
                      change_summary: str, save_note: str) -> None:
    path = project / "CHANGELOG.md"
    entry = (
        f"\n## {version} — {date.today().isoformat()}\n\n"
        f"- Feedback [{item['id']}] ({item.get('source', '')}): "
        f"“{item.get('text', '')}”\n"
        f"- Change: {change_summary}\n"
        f"- Verified: the full headless test suite passed; "
        f"{save_note}\n"
    )
    if path.exists():
        text = path.read_text(encoding="utf-8")
        path.write_text(text.rstrip("\n") + "\n" + entry, encoding="utf-8")
    else:
        path.write_text("# Changelog\n" + entry, encoding="utf-8")


def _append_patch_log(project: Path, record: dict) -> None:
    path = project / ".weaverbird" / PATCH_LOG_NAME
    records = []
    if path.exists():
        try:
            records = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            records = []
    records.append(record)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(records, indent=2) + "\n", encoding="utf-8")


def load_patch_log(project_dir) -> list:
    path = Path(project_dir) / ".weaverbird" / PATCH_LOG_NAME
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except json.JSONDecodeError:
        return []


# ---------------------------------------------------------------------------
# Config changes

def config_path_for(project_dir) -> Path:
    return Path(project_dir) / GAME_DIR_NAME / "game_config.json"


def load_config(project_dir) -> dict:
    path = config_path_for(project_dir)
    if not path.exists():
        raise PatchError(
            "No game_config.json found — the real game has not been "
            "built yet, so there is nothing to patch.")
    return json.loads(path.read_text(encoding="utf-8"))


def save_config(project_dir, config: dict) -> None:
    config_path_for(project_dir).write_text(
        json.dumps(config, indent=2) + "\n", encoding="utf-8")


def patch_bounds(archetype: str) -> dict:
    bounds = dict(generate_module.BOUNDS.get(archetype, {}))
    bounds.update(EXTRA_BOUNDS)
    return bounds


def clamp_value(archetype: str, field: str, value):
    bounds = patch_bounds(archetype)
    if field not in bounds:
        return value
    lo, hi = bounds[field]
    value = min(max(float(value), lo), hi)
    current_is_int = field in ("targets_per_round", "lives")
    return int(round(value)) if current_is_int else round(value, 3)


def _apply_numeric_changes(config: dict, archetype: str,
                           proposals: dict) -> tuple[dict, list[str]]:
    """Validate + clamp proposed numbers onto a copy of config.
    Returns (new_config, human-readable change lines)."""
    new_config = dict(config)
    lines: list[str] = []
    for field, raw in proposals.items():
        if field not in patch_bounds(archetype) or field not in config:
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        value = clamp_value(archetype, field, value)
        old = config[field]
        if value != old:
            new_config[field] = value
            lines.append(f"{field} {old} → {value}")
    return new_config, lines


def _scale_budgets(config: dict, factor: float) -> tuple[dict, list[str]]:
    """Scale crate puzzle budgets, never below a solvable floor
    (the verified solution length plus a small margin)."""
    new_config = dict(config)
    puzzles = []
    changed = False
    for puzzle in config.get("puzzles", []):
        puzzle = dict(puzzle)
        floor = len(puzzle.get("solution", "")) + 5
        new_budget = int(min(max(int(puzzle["budget"] * factor), floor), 600))
        if new_budget != puzzle["budget"]:
            changed = True
        puzzle["budget"] = new_budget
        puzzles.append(puzzle)
    if changed:
        new_config["puzzles"] = puzzles
        return new_config, [f"puzzle move budgets × {factor}"]
    return config, []


_EASE_WORDS = ("too hard", "difficult", "impossible", "frustrating",
               "unfair", "brutal")
_HARDEN_WORDS = ("too easy", "boring", "trivial", "no challenge")


def deterministic_balance(archetype: str, config: dict,
                          text: str) -> tuple[dict, list[str], str]:
    """The nudge table: plain phrases → bounded numeric changes.
    Returns (new_config, change_lines, direction_note)."""
    lowered = text.lower()
    ease = any(word in lowered for word in _EASE_WORDS)
    harden = any(word in lowered for word in _HARDEN_WORDS)
    if "too slow" in lowered or "sluggish" in lowered:
        if archetype in ("arena_collector", "platformer"):
            new, lines = _apply_numeric_changes(
                config, archetype,
                {"player_speed": float(config.get("player_speed", 300)) * 1.15})
            return new, lines, "made the player faster (feedback: too slow)"
        new, lines = _scale_budgets(config, 1.15)
        return new, lines, "gave more moves per puzzle (feedback: too slow)"
    if "too fast" in lowered:
        if archetype == "arena_collector":
            new, lines = _apply_numeric_changes(
                config, archetype,
                {"hazard_speed": float(config.get("hazard_speed", 1.4)) * 0.85})
            return new, lines, "slowed the hazards (feedback: too fast)"
        if archetype == "crate_pusher":
            new, lines = _scale_budgets(config, 1.15)
            return new, lines, "gave more moves per puzzle (feedback: too fast)"
        new, lines = _apply_numeric_changes(
            config, archetype,
            {"gravity": float(config.get("gravity", 1400)) * 0.95})
        return new, lines, "softened gravity (feedback: too fast)"
    if ease and not harden:
        if archetype == "arena_collector":
            new, lines = _apply_numeric_changes(config, archetype, {
                "hazard_speed": float(config.get("hazard_speed", 1.4)) * 0.85,
                "round_seconds": float(config.get("round_seconds", 60)) * 1.15})
            return new, lines, "eased the game (feedback: too hard)"
        if archetype == "platformer":
            new, lines = _apply_numeric_changes(config, archetype, {
                "gravity": float(config.get("gravity", 1400)) * 0.95,
                "jump_strength": float(config.get("jump_strength", 545)) * 1.05,
                "lives": int(config.get("lives", 3)) + 1})
            return new, lines, "eased the game (feedback: too hard)"
        new, lines = _scale_budgets(config, 1.2)
        return new, lines, "eased the game — more moves per puzzle (feedback: too hard)"
    if harden and not ease:
        if archetype == "arena_collector":
            new, lines = _apply_numeric_changes(config, archetype, {
                "hazard_speed": float(config.get("hazard_speed", 1.4)) * 1.15,
                "round_seconds": float(config.get("round_seconds", 60)) * 0.9})
            return new, lines, "toughened the game (feedback: too easy)"
        if archetype == "platformer":
            new, lines = _apply_numeric_changes(config, archetype, {
                "gravity": float(config.get("gravity", 1400)) * 1.05,
                "jump_strength": float(config.get("jump_strength", 545)) * 0.97,
                "lives": int(config.get("lives", 3)) - 1})
            return new, lines, "toughened the game (feedback: too easy)"
        new, lines = _scale_budgets(config, 0.85)
        return new, lines, "toughened the game — fewer moves per puzzle (feedback: too easy)"
    return config, [], "no deterministic nudge matched the feedback wording"


_BRAIN_BALANCE_SYSTEM = (
    "You tune a generated game's numbers in response to one piece "
    "of player feedback. Answer with one JSON object only, mapping "
    "field names to new numeric values inside the given ranges. "
    "Change only what the feedback calls for; small, fair steps."
)


def brain_balance(project, archetype: str, config: dict, item: dict,
                  settings_path=None) -> tuple[dict, list[str], str]:
    """Mid-tier proposal, validated + clamped like every other.
    (new_config, lines, note) — on any failure the deterministic
    config comes back: (config, [], why)."""
    _, data = brain_module.resolve_settings(settings_path, project)
    status = brain_module.tier_status(data, "mid") if data else None
    if not status or not status.get("usable"):
        reason = status["reason"] if status else "no settings file found"
        return config, [], f"no usable mid-tier model ({reason})"
    bounds = {k: v for k, v in patch_bounds(archetype).items() if k in config}
    if not bounds:
        return config, [], "no tunable numeric fields for this archetype"
    prompt = (
        "Feedback: " + item.get("text", "") + "\n"
        "Archetype: " + archetype + "\n"
        "Current values and allowed ranges:\n"
        + json.dumps({k: {"current": config[k], "min": v[0], "max": v[1]}
                      for k, v in bounds.items()})
        + "\nReturn the JSON object now."
    )
    try:
        result = brain_module.complete(
            project, "mid",
            [{"role": "system", "content": _BRAIN_BALANCE_SYSTEM},
             {"role": "user", "content": prompt}],
            stage="patch", settings_path=settings_path, max_tokens=300)
    except brain_module.BrainBudgetError as exc:
        return config, [], f"brain refused by budget ({exc})"
    if not result.ok:
        return config, [], f"brain call failed ({result.error})"
    answers = brain_module.extract_json(result.text)
    new_config, lines = _apply_numeric_changes(config, archetype, answers)
    if not lines:
        return config, [], "brain replied but proposed no in-range change"
    note = (f"mid tier ({result.provider}/{result.model}) proposed, "
            "validated and clamped")
    if result.warning:
        note += " — " + result.warning
    return new_config, lines, note


def confusion_change(project, config: dict, item: dict,
                     settings_path=None, use_brain: bool = True) -> tuple[str, str]:
    """A clearer objective hint. Returns (new_hint, how).
    Deterministic floor: lead with the scope's win condition."""
    current = str(config.get("objective_hint", ""))
    win = ""
    scope_file = scope_module.scope_path_for(project)
    if scope_file.exists():
        values = {f.name: f.value
                  for f in scope_module.load_scope(scope_file).fields}
        win = values.get("Win condition", "").strip()
    deterministic = current
    if win:
        sentence = win.rstrip(".")
        candidate = f"Goal: {sentence[0].lower() + sentence[1:] if sentence else sentence}. {current}"
        deterministic = candidate[:180].strip()
    if use_brain:
        _, data = brain_module.resolve_settings(settings_path, project)
        status = brain_module.tier_status(data, "mid") if data else None
        if status and status.get("usable"):
            prompt = (
                "A player is confused by this game. Current on-screen "
                f"hint: {current!r}\nWin condition: {win or 'not stated'}\n"
                "Player feedback: " + item.get("text", "") + "\n"
                "Rewrite the hint in at most 140 characters, plain "
                "words, saying what to do and how to win. Answer with "
                "the hint text only.")
            try:
                result = brain_module.complete(
                    project, "mid",
                    [{"role": "user", "content": prompt}],
                    stage="patch", settings_path=settings_path,
                    max_tokens=120)
                if result.ok:
                    text = " ".join(result.text.split()).strip(" \"'")
                    if 10 <= len(text) <= 140 and text != current:
                        return text, (f"hint rewritten by the mid tier "
                                      f"({result.provider}/{result.model})")
            except brain_module.BrainBudgetError:
                pass
    if deterministic != current:
        return deterministic, "hint rewritten from the scope's win condition"
    return current, "no clearer hint could be produced"


# ---------------------------------------------------------------------------
# Bug remedies

def match_bug_remedy(project) -> tuple[str | None, str]:
    """Match the latest test report's failures to a known remedy:
    ("assets" | "rebuild" | None, what matched)."""
    report = testing_module.load_report(project)
    if not report:
        return None, "no test report on record to match a failure class against"
    errors: list[str] = []
    for run in report.get("runs", []):
        errors.extend(str(e) for e in run.get("errors", []))
    joined = "\n".join(errors)
    if re.search(r"(failed loading resource|cannot open file|"
                 r"res://assets|no such file)", joined, re.IGNORECASE):
        return "assets", "the test report shows missing/failed assets — regenerate them"
    if re.search(r"(parse error|script error)", joined, re.IGNORECASE) \
            and ".gd" in joined:
        return "rebuild", "the test report shows a script parse error — regenerate the game"
    return None, "no known failure class in the test report matches this bug"


def write_patch_plan(project, item: dict, reason: str) -> Path:
    scope_values = {}
    scope_file = scope_module.scope_path_for(Path(project))
    if scope_file.exists():
        scope_values = {f.name: f.value for f in
                        scope_module.load_scope(scope_file).fields}
    report = testing_module.load_report(Path(project))
    failing = []
    if report:
        for run in report.get("runs", []):
            if not run.get("ok"):
                failing.append(f"- {run.get('name')}: "
                               + "; ".join(run.get("errors", [])[:2]))
    lines = [
        f"# Patch plan — feedback [{item['id']}]",
        "",
        f"Feedback ({item.get('source', '')}): “{item.get('text', '')}”",
        f"Category: {item.get('category', '')} · severity {item.get('severity', '')}",
        "",
        "## Why this is a plan, not a patch",
        "",
        reason,
        "",
        "The factory only ships patches it can verify end-to-end. "
        "This one needs a code change the factory will not improvise.",
        "",
        "## What was checked",
        "",
        f"Game: {scope_values.get('Title (working)', 'untitled')} — "
        f"archetype on record; latest test verdict: "
        f"{'PASS' if report and report.get('ok') else 'FAIL' if report else 'no report'}.",
    ]
    if failing:
        lines += ["", "Failing runs in the latest test report:", ""] + failing
    lines += [
        "",
        "## Proposed change (for a human or a later factory stage)",
        "",
        "Reproduce the feedback against the current build, locate the "
        "cause in the generated game under game/, make the smallest "
        "change that addresses it, then re-run `weaverbird test run` "
        "and the save-compatibility check before shipping it as a patch.",
        "",
    ]
    path = Path(project) / PLAN_NAME
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Save compatibility

def userdata_dir(project) -> Path | None:
    game_dir = Path(project) / GAME_DIR_NAME
    try:
        info = godot_module.parse_project_godot(game_dir)
    except FileNotFoundError:
        return None
    name = info.get("name")
    if not name:
        return None
    return Path.home() / ".local" / "share" / "godot" / "app_userdata" / name


def capture_save(project, godot_path=None) -> Path:
    """Snapshot the CURRENT build's save, made by the game itself.

    Uses the existing user:// game_state.json when one is there;
    otherwise plays the competent win run (which records a run
    through the game's own SettingsStore) and snapshots what it
    wrote. Raises PatchError when no save can be produced."""
    project = Path(project)
    user_dir = userdata_dir(project)
    if user_dir is None:
        raise PatchError("Could not work out the game's save folder "
                         "from project.godot.")
    state_file = user_dir / "game_state.json"
    if not state_file.exists():
        game_dir = project / GAME_DIR_NAME
        godot_bin, _version = godot_module.find_godot(godot_path)
        if not godot_bin:
            raise PatchError("Godot was not found, so a pre-patch save "
                             "cannot be captured.")
        skeleton_module._run_godot(
            godot_bin, game_dir,
            ["--script", "tests/competent_win.gd"], timeout=420)
    if not state_file.exists():
        raise PatchError(
            "The current build produced no save file to check "
            "compatibility against — the patch cannot ship without one.")
    snapshot_dir = project / ".weaverbird" / SNAPSHOT_DIR
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    snapshot = snapshot_dir / "game_state.json"
    snapshot.write_bytes(state_file.read_bytes())
    return snapshot


_SAVECHECK_RE = re.compile(r"^SAVECHECK (\{.*\})\s*$", re.MULTILINE)


def run_savecheck(project, snapshot, godot_path=None) -> tuple[bool, dict, list]:
    """Boot the (patched) game with the pre-patch save in place and
    require the game's own store to read the saved stats back —
    verbatim. Returns (ok, stats_the_game_read, errors)."""
    import subprocess
    import time as time_module
    project = Path(project)
    game_dir = project / GAME_DIR_NAME
    savecheck = game_dir / "tests" / "savecheck.gd"
    if not savecheck.exists():
        from importlib import resources
        template = resources.files(__package__).joinpath(
            "game_templates", "shared", "savecheck.gd")
        savecheck.parent.mkdir(parents=True, exist_ok=True)
        savecheck.write_text(template.read_text(encoding="utf-8"),
                             encoding="utf-8")
    user_dir = userdata_dir(project)
    if user_dir is None:
        return False, {}, ["could not locate the game's save folder"]
    user_dir.mkdir(parents=True, exist_ok=True)
    (user_dir / "game_state.json").write_bytes(Path(snapshot).read_bytes())
    try:
        expected = json.loads(Path(snapshot).read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        expected = {}
    godot_bin, _version = godot_module.find_godot(godot_path)
    if not godot_bin:
        return False, {}, ["Godot was not found"]
    command = [godot_bin, "--headless", "--path", str(game_dir),
               "--script", "tests/savecheck.gd"]
    started = time_module.monotonic()
    try:
        result = subprocess.run(command, capture_output=True, text=True,
                                timeout=120)
        output = (result.stdout or "") + "\n" + (result.stderr or "")
        returncode = result.returncode
    except subprocess.TimeoutExpired:
        return False, {}, ["the save check timed out after 120 seconds"]
    _ = started  # duration not needed for the verdict
    errors = skeleton_module._error_digest(output)
    match = _SAVECHECK_RE.search(output)
    if returncode != 0 or errors or not match:
        return False, {}, errors or ["the save check did not report"]
    try:
        stats = json.loads(match.group(1))
    except json.JSONDecodeError:
        return False, {}, ["the save check report was unreadable"]
    if not stats.get("ok"):
        return False, stats, [str(stats.get("reason", "save check failed"))]
    for field in ("games_played", "wins", "best_targets"):
        if int(stats.get(field, -1)) != int(expected.get(field, 0)):
            return False, stats, [
                f"saved stat {field} changed across the patch: "
                f"{expected.get(field, 0)} before, {stats.get(field)} after"]
    return True, stats, []


# ---------------------------------------------------------------------------
# The loop itself

def _git_head(project: Path) -> str | None:
    import subprocess
    result = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(project),
                            capture_output=True, text=True)
    return result.stdout.strip() if result.returncode == 0 else None


def start_patch(project_dir, item_id: str, settings_path=None,
                godot_path=None, use_brain: bool = True) -> dict:
    """Run one approved feedback item through the patch loop.

    Returns an outcome dict: outcome is "patched" (with version),
    "planned" (PATCH_PLAN.md written), or "rejected" (gates failed,
    tree restored). Refusals (item not approved, category the maker
    must decide) raise PatchError.
    """
    project = Path(project_dir)
    item = feedback_module.get_item(project, item_id)
    if item is None:
        raise PatchError(f"No feedback item with id {item_id!r}.")
    if item.get("status") != "approved":
        raise PatchError(
            f"Item {item_id} is {item.get('status')!r}, not 'approved' — "
            "only approved items enter the patch loop.")
    category = item.get("category", "unknown")
    if category in ("request", "praise", "unknown"):
        raise PatchError(
            f"Item {item_id} is a {category} item — "
            + ("requests, scope and price are the maker's decision, "
               "never an automatic patch." if category == "request"
               else "there is nothing here for the patch loop to change."))
    config = load_config(project)
    archetype = config.get("archetype", "")
    if archetype not in generate_module.ARCHETYPES:
        gen_report = generate_module.load_report(project)
        archetype = (gen_report or {}).get("archetype", archetype)
    if archetype not in generate_module.ARCHETYPES:
        raise PatchError("The game's archetype is unknown, so no safe "
                         "patch can be computed.")

    plan_summary = ""
    change_lines: list[str] = []
    new_config = None
    remedy = None

    if category == "balance":
        if use_brain:
            candidate, lines, note = brain_balance(
                project, archetype, config, item,
                settings_path=settings_path)
            if lines:
                new_config, change_lines = candidate, lines
                plan_summary = note
        if new_config is None:
            candidate, lines, note = deterministic_balance(
                archetype, config, item.get("text", ""))
            if lines:
                new_config, change_lines = candidate, lines
                plan_summary = note
            else:
                remedy = None
                plan_reason = (
                    "The feedback is about balance, but neither the "
                    "model nor the deterministic nudge table produced "
                    "an in-bounds change for it (" + note + ").")
                path = write_patch_plan(project, item, plan_reason)
                feedback_module.update_item(
                    project, item_id, status="planned",
                    note="patch plan written — no safe automatic change")
                _commit_quietly(project, f"Weaverbird patch plan: {item_id}")
                return {"outcome": "planned", "item": item_id,
                        "plan": str(path), "reason": plan_reason}
    elif category == "confusion":
        new_hint, how = confusion_change(
            project, config, item, settings_path=settings_path,
            use_brain=use_brain)
        if new_hint == str(config.get("objective_hint", "")):
            plan_reason = ("The feedback is about confusion, but no "
                           "clearer objective hint could be produced.")
            path = write_patch_plan(project, item, plan_reason)
            feedback_module.update_item(
                project, item_id, status="planned",
                note="patch plan written — hint unchanged")
            _commit_quietly(project, f"Weaverbird patch plan: {item_id}")
            return {"outcome": "planned", "item": item_id,
                    "plan": str(path), "reason": plan_reason}
        new_config = dict(config)
        new_config["objective_hint"] = new_hint
        change_lines = [f"objective hint → “{new_hint}”"]
        plan_summary = how
    elif category == "bug":
        remedy, why = match_bug_remedy(project)
        if remedy is None:
            plan_reason = ("This bug does not match a failure class "
                           "with a verified remedy. " + why + ".")
            path = write_patch_plan(project, item, plan_reason)
            feedback_module.update_item(
                project, item_id, status="planned",
                note="patch plan written — no known remedy")
            _commit_quietly(project, f"Weaverbird patch plan: {item_id}")
            return {"outcome": "planned", "item": item_id,
                    "plan": str(path), "reason": plan_reason}
        plan_summary = why
        change_lines = [f"remedy applied: {remedy} ({why})"]

    # --- checkpoint, capture, apply --------------------------------
    vcs_module.init_repo(project)
    vcs_module.ensure_local_identity(project)
    _commit_quietly(project, f"Weaverbird patch checkpoint: {item_id}")
    pre_sha = _git_head(project)
    snapshot = capture_save(project, godot_path=godot_path)
    # The ledger is a record of real spend (the planning calls above
    # already wrote to it). A rollback must not unspend history, so
    # snapshot it now and restore it if the patch is rejected.
    ledger_path = project / ".weaverbird" / "ledger.jsonl"
    ledger_text = (ledger_path.read_text(encoding="utf-8")
                   if ledger_path.exists() else None)

    if new_config is not None:
        save_config(project, new_config)
    elif remedy == "assets":
        assets_module.generate_assets(project)
    elif remedy == "rebuild":
        generate_module.build_game(project, settings_path=settings_path)

    # --- the two gates ----------------------------------------------
    report = testing_module.run_tests(project, godot_path=godot_path,
                                      allow_repair=False)
    tests_ok = bool(report.get("ok"))
    save_ok, save_stats, save_errors = (False, {}, [])
    if tests_ok:
        save_ok, save_stats, save_errors = run_savecheck(
            project, snapshot, godot_path=godot_path)

    if tests_ok and save_ok:
        old_version = game_version(project)
        new_version = bump_version(old_version)
        version_path_for(project).write_text(new_version + "\n",
                                             encoding="utf-8")
        summary = "; ".join(change_lines) or plan_summary
        save_note = (f"save compatibility passed — a save from "
                     f"{old_version} loads with its stats intact "
                     f"(games played {save_stats.get('games_played', '?')}, "
                     f"wins {save_stats.get('wins', '?')}).")
        _append_changelog(project, new_version, item, summary, save_note)
        record = {
            "version": new_version, "item": item_id,
            "category": category, "at": datetime.now(timezone.utc)
                .isoformat(timespec="seconds"),
            "change": summary, "how": plan_summary,
            "feedback_text": item.get("text", ""),
        }
        _append_patch_log(project, record)
        feedback_module.update_item(
            project, item_id, status="patched", patch_version=new_version,
            note=summary)
        _commit_quietly(
            project, f"Weaverbird patch {new_version}: {item_id} ({category})")
        tag = f"known-good/patch-{new_version}"
        if tag not in vcs_module.list_tags(project):
            vcs_module.tag_known_good(project, tag)
        return {"outcome": "patched", "item": item_id,
                "version": new_version, "change": summary,
                "tests_ok": True, "save_ok": True}

    # --- rejected: restore, record, hand back ------------------------
    failures = []
    if not tests_ok:
        failing = [r.get("name") for r in report.get("runs", [])
                   if not r.get("ok")]
        failures.append("test suite failed (" + ", ".join(failing) + ")")
    if tests_ok and not save_ok:
        failures.append("save compatibility failed: "
                        + "; ".join(save_errors[:2]))
    reason = "; ".join(failures) or "a gate failed"
    if pre_sha:
        vcs_module.reset_to_commit(project, pre_sha)
    if ledger_text is not None:
        ledger_path.parent.mkdir(parents=True, exist_ok=True)
        ledger_path.write_text(ledger_text, encoding="utf-8")
    feedback_module.update_item(
        project, item_id, status="approved",
        note=f"patch rejected — {reason}; the tree was restored")
    _commit_quietly(project, f"Weaverbird patch rejected: {item_id}")
    return {"outcome": "rejected", "item": item_id, "reason": reason,
            "tests_ok": tests_ok, "save_ok": save_ok}


def _commit_quietly(project: Path, message: str) -> None:
    try:
        vcs_module.commit_all(project, message)
    except vcs_module.VcsError:
        pass  # nothing new to commit is a fine outcome here
