"""Real game generation — pipeline stage "build".

The skeleton (stage 2) is one generic grey-box loop for every game.
This stage is where the game the scope actually describes gets
built. It picks an archetype from the scope — arena collector,
platformer, or crate pusher — generates that game's core into
``<project>/game/`` on top of the skeleton's shell (menus, settings,
pause and save are the skeleton's and stay), tunes it from a config
derived from the scope, and validates the result with the same
Godot headless discipline as the skeleton.

Design rules, in keeping with the factory's token economy:

- Everything is deterministic unless a model is configured. When a
  bring-your-own-key brain is usable, the mid tier may propose a few
  tuning numbers as strict JSON; every value is validated and
  clamped against the same bounds the deterministic config uses,
  and any failure falls back to the deterministic config. The game
  reads its tuning from ``game/game_config.json`` at runtime —
  tuning is data, not template surgery.
- Content units come from the scope's content counts (first integer,
  clamped 1–20): platformer levels, arena rounds, crate puzzles.
- Crate puzzles ship only when a solver in this module has verified
  them solvable; the verified solution (and, for the first puzzle, a
  sabotage line that corners a crate, when one exists) travels in
  the config so the scripted playtesters can play them.
"""

from __future__ import annotations

import colorsys
import json
import random
import re
from collections import deque
from datetime import datetime, timezone
from importlib import resources
from pathlib import Path

from . import brain as brain_module
from . import ledger as ledger_module
from . import provenance as provenance_module
from . import scope as scope_module
from . import skeleton as skeleton_module
from . import state as state_module
from . import vcs as vcs_module

GAME_DIR_NAME = skeleton_module.GAME_DIR_NAME
REPORT_NAME = "generation_report.json"
KNOWN_GOOD_TAG = "known-good/build"
PROVENANCE_PROVIDER = "weaverbird-build"
ARCHETYPES = ("arena_collector", "platformer", "crate_pusher")

_INT_RE = re.compile(r"\d+")
_ARCHETYPE_RE = re.compile(
    r"archetype\s*[:=]\s*(arena_collector|platformer|crate_pusher|arena|crate)",
    re.IGNORECASE,
)

# Bounds every config value is clamped to — the brain's proposals
# get exactly the same treatment as the deterministic defaults.
BOUNDS = {
    "arena_collector": {
        "player_speed": (220.0, 420.0),
        "hazard_speed": (0.6, 2.4),
        "targets_per_round": (3, 10),
    },
    "platformer": {
        "player_speed": (200.0, 360.0),
        "jump_strength": (420.0, 660.0),
        "gravity": (1100.0, 1800.0),
    },
    "crate_pusher": {
        "move_budget_multiplier": (1.5, 4.0),
    },
}

_HINTS = {
    "arena_collector":
        "Collect every glowing target before the time runs out. Avoid the red hazards!",
    "platformer":
        "Gather the stars and reach the glowing door. Mind the spikes!",
    "crate_pusher":
        "Push every crate onto a glowing pad. E undoes a step.",
}


class GenerationError(Exception):
    """Raised when game generation is refused or validation fails."""


# ---------------------------------------------------------------------------
# Archetype selection and scope-derived config

def _values(parsed_scope) -> dict:
    return {f.name: f.value for f in parsed_scope.fields}


def detect_archetype(values: dict, override: str | None = None) -> tuple[str, str]:
    """(archetype, why) — deterministic from the scope text.

    An explicit override (CLI flag first, then an ``archetype: X``
    note in any scope field) beats detection; otherwise the genre,
    core verb and win/lose text decide.
    """
    if override:
        return _normalise_archetype(override), f"explicit override ({override!r})"
    for value in values.values():
        match = _ARCHETYPE_RE.search(value or "")
        if match:
            return (
                _normalise_archetype(match.group(1)),
                f"the scope names it ('archetype: {match.group(1)}')",
            )
    text = " ".join(
        str(values.get(name, ""))
        for name in ("Genre / archetype",
                     "Core verb (what the player does most)",
                     "Win condition", "Lose / fail condition")
    ).lower()
    if any(word in text for word in ("platform", "jump", "side-scroll",
                                     "side scroll", "sidescroll",
                                     "side-scroller")):
        return "platformer", "the scope's genre/verb/win text mentions platforms or jumping"
    if any(word in text for word in ("puzzle", "sokoban", "crate", "push")):
        return "crate_pusher", "the scope's genre/verb/win text mentions puzzles, crates or pushing"
    return "arena_collector", "no platform or puzzle signal in the scope — the arena collector is the default"


def _normalise_archetype(name: str) -> str:
    name = str(name).strip().lower()
    aliases = {"arena": "arena_collector", "crate": "crate_pusher",
               "sokoban": "crate_pusher"}
    name = aliases.get(name, name)
    if name not in ARCHETYPES:
        raise GenerationError(
            f"Unknown archetype {name!r}. Known: {', '.join(ARCHETYPES)}."
        )
    return name


def content_units(values: dict) -> int:
    """Content units from the scope's content counts (first integer)."""
    raw = values.get(
        "Content counts (levels / waves / rooms / items, as applicable)", "")
    match = _INT_RE.search(raw or "")
    if not match:
        return 5
    return int(min(max(int(match.group(0)), 1), 20))


def _scope_int(values: dict, fields: tuple, pattern: str):
    for name in fields:
        match = re.search(pattern, values.get(name, "") or "", re.IGNORECASE)
        if match:
            return int(match.group(1))
    return None


def _hex_of(rgb) -> str:
    return "#%02X%02X%02X" % tuple(int(round(c * 255)) for c in rgb)


def _shift_hex(hex_value: str, hue_shift: float, saturation=None,
               lightness=None) -> str:
    digits = hex_value.lstrip("#")
    r, g, b = (int(digits[i:i + 2], 16) / 255 for i in (0, 2, 4))
    hue, light, sat = colorsys.rgb_to_hls(r, g, b)
    hue = (hue + hue_shift) % 1.0
    if saturation is not None:
        sat = saturation
    if lightness is not None:
        light = lightness
    r2, g2, b2 = colorsys.hls_to_rgb(hue, light, sat)
    return _hex_of((r2, g2, b2))


def _palette(values: dict) -> dict:
    accent, target, accent_hex = skeleton_module._scope_colours(values)
    target_hex = _hex_of(target)
    return {
        "accent_hex": accent_hex,
        "target_hex": target_hex,
        "hazard_hex": _shift_hex(accent_hex, 0.5, saturation=0.72,
                                 lightness=0.55),
        "platform_hex": _shift_hex(accent_hex, 0.0, saturation=0.18,
                                   lightness=0.30),
        "crate_hex": _shift_hex(accent_hex, 0.06, saturation=0.48,
                                lightness=0.40),
    }


# ---------------------------------------------------------------------------
# Crate puzzles: built-in layouts, a solver, and a saboteur

CRATE_LAYOUTS: list[str] = [
    # One crate, one pad — the gentle first puzzle.
    "\n".join([
        "#########",
        "#       #",
        "#  $.   #",
        "#  @    #",
        "#       #",
        "#########",
    ]),
    # Two crates straight above their pads.
    "\n".join([
        "##########",
        "#        #",
        "# $$     #",
        "# ..     #",
        "#  @     #",
        "#        #",
        "##########",
    ]),
    # Three crates in a row, each above its pad.
    "\n".join([
        "###########",
        "#         #",
        "#  $ $ $  #",
        "#  . . .  #",
        "#    @    #",
        "#         #",
        "###########",
    ]),
    # Two drops, plus a long push along the bottom corridor.
    "\n".join([
        "##########",
        "#        #",
        "# $    $ #",
        "# .    . #",
        "#    @   #",
        "# $    . #",
        "#        #",
        "##########",
    ]),
    # A pair to drop and a loner on the right.
    "\n".join([
        "###########",
        "#         #",
        "#  $$     #",
        "#  ..  $  #",
        "#      .  #",
        "#   @     #",
        "#         #",
        "###########",
    ]),
]

_MOVE_DIRS = {"u": (0, -1), "d": (0, 1), "l": (-1, 0), "r": (1, 0)}


def parse_grid(text: str) -> dict:
    walls, pads, crates = set(), set(), []
    player = None
    rows = text.strip("\n").splitlines()
    for y, row in enumerate(rows):
        for x, ch in enumerate(row):
            cell = (x, y)
            if ch == "#":
                walls.add(cell)
            elif ch == ".":
                pads.add(cell)
            elif ch == "$":
                crates.append(cell)
            elif ch == "*":
                crates.append(cell)
                pads.add(cell)
            elif ch == "@":
                player = cell
            elif ch == "+":
                player = cell
                pads.add(cell)
    return {"walls": walls, "pads": pads, "crates": tuple(sorted(crates)),
            "player": player, "rows": rows}


def _cornered(crate, walls, pads) -> bool:
    if crate in pads:
        return False
    x, y = crate
    up = (x, y - 1) in walls
    down = (x, y + 1) in walls
    left = (x - 1, y) in walls
    right = (x + 1, y) in walls
    return (up or down) and (left or right)


def _reachable(player, walls, crates) -> dict:
    """BFS over player walks: cell -> move string from `player`."""
    blocked = set(walls) | set(crates)
    seen = {player: ""}
    queue = deque([player])
    while queue:
        cell = queue.popleft()
        for name, (dx, dy) in _MOVE_DIRS.items():
            nxt = (cell[0] + dx, cell[1] + dy)
            if nxt in blocked or nxt in seen:
                continue
            seen[nxt] = seen[cell] + name
            queue.append(nxt)
    return seen


def solve_puzzle(grid_text: str, max_states: int = 400_000):
    """A verified solution (full move string) for a layout, or None.

    Breadth-first over pushes, with cornered crates pruned — the
    classic Sokoban search, small because the layouts are small.
    The returned string includes the walks between pushes, so the
    scripted players can replay it key by key.
    """
    parsed = parse_grid(grid_text)
    walls, pads = parsed["walls"], parsed["pads"]
    start_crates = parsed["crates"]
    start_player = parsed["player"]
    if start_player is None:
        return None

    def canonical(crates, player):
        reach = _reachable(player, walls, crates)
        return (crates, min(reach))

    start_key = canonical(start_crates, start_player)
    parents: dict = {start_key: None}
    detail: dict = {start_key: (start_crates, start_player, "")}
    queue = deque([start_key])
    states = 0
    while queue:
        key = queue.popleft()
        crates, player, moves = detail[key]
        if set(crates) == set(pads) and len(crates) == len(pads):
            return moves
        states += 1
        if states > max_states:
            return None
        reach = _reachable(player, walls, crates)
        crate_set = set(crates)
        for crate in crates:
            for name, (dx, dy) in _MOVE_DIRS.items():
                stand = (crate[0] - dx, crate[1] - dy)
                beyond = (crate[0] + dx, crate[1] + dy)
                if stand not in reach:
                    continue
                if beyond in walls or beyond in crate_set:
                    continue
                if _cornered(beyond, walls, pads):
                    continue
                new_crates = tuple(sorted(
                    (crate_set - {crate}) | {beyond}))
                new_player = crate
                new_moves = moves + reach[stand] + name
                new_key = canonical(new_crates, new_player)
                if new_key in parents:
                    continue
                parents[new_key] = key
                detail[new_key] = (new_crates, new_player, new_moves)
                queue.append(new_key)
    return None


def find_sabotage(grid_text: str, max_states: int = 80_000):
    """A short move line that corners a crate, or None.

    Breadth-first over single moves (walks and pushes together),
    looking for any state where a crate sits in a corner off its
    pad. Used by the breaker playtester as its sabotage line.
    """
    parsed = parse_grid(grid_text)
    walls, pads = parsed["walls"], parsed["pads"]
    start = (parsed["player"], parsed["crates"])
    if start[0] is None:
        return None
    parents: dict = {start: None}
    detail: dict = {start: ""}
    queue = deque([start])
    states = 0
    while queue:
        player, crates = queue.popleft()
        moves = detail[(player, crates)]
        states += 1
        if states > max_states:
            return None
        crate_set = set(crates)
        for name, (dx, dy) in _MOVE_DIRS.items():
            nxt = (player[0] + dx, player[1] + dy)
            if nxt in walls:
                continue
            new_crates = crates
            if nxt in crate_set:
                beyond = (nxt[0] + dx, nxt[1] + dy)
                if beyond in walls or beyond in crate_set:
                    continue
                new_crates = tuple(sorted((crate_set - {nxt}) | {beyond}))
                if any(_cornered(c, walls, pads) for c in new_crates):
                    return moves + name
            state = (nxt, new_crates)
            if state in parents:
                continue
            parents[state] = (player, crates)
            detail[state] = moves + name
            queue.append(state)
    return None


def _mirror_grid(text: str) -> str:
    return "\n".join(row[::-1] for row in text.strip("\n").splitlines())


def crate_puzzles(units: int, budget_override: int | None = None,
                  budget_multiplier: float = 2.5) -> list[dict]:
    """Verified puzzle dicts for the config: layout, solution,
    sabotage (first puzzle only) and a move budget per puzzle."""
    puzzles: list[dict] = []
    for index in range(units):
        base = CRATE_LAYOUTS[index % len(CRATE_LAYOUTS)]
        text = base if index < len(CRATE_LAYOUTS) else _mirror_grid(base)
        solution = solve_puzzle(text)
        if solution is None:
            raise GenerationError(
                f"Built-in crate layout {index + 1} has no verified "
                "solution — refusing to ship an unsolvable puzzle."
            )
        budget = budget_override or max(
            40, int(len(solution) * budget_multiplier) + 20)
        puzzles.append({
            "grid": text.strip("\n").splitlines(),
            "solution": solution,
            "sabotage": (find_sabotage(text) or "") if index == 0 else "",
            "budget": budget,
        })
    return puzzles


# ---------------------------------------------------------------------------
# Platformer levels: procedurally varied, reachable by construction

def _platformer_level(rng: random.Random, index: int) -> dict:
    """One level. Ground segments with spike pits between them and a
    staircase of floating platforms carrying the stars. Steps are
    kept within jump reach (height gain <= 80, gaps <= 105 against a
    ~100px jump), so the level is traversable by construction."""
    width = min(1500 + index * 90, 2100)
    ground_y = 560
    segments: list[tuple[int, int]] = []
    x = 0
    while x < width:
        seg_len = rng.randint(300, 430) if not segments else rng.randint(230, 370)
        end = min(x + seg_len, width)
        segments.append((x, end))
        if end >= width:
            break
        x = end + rng.randint(60, 105)
    if segments[-1][1] < width:
        last = segments[-1]
        segments[-1] = (last[0], width)
    platforms = [[sx, ground_y, ex - sx] for sx, ex in segments]
    spikes = []
    for (sx, ex), (nx, _ne) in zip(segments, segments[1:]):
        if nx > ex:
            spikes.append([ex, 600, nx - ex])
    if not spikes:  # guarantee at least one pit per level
        sx, ex = segments[len(segments) // 2]
        mid = (sx + ex) // 2
        platforms.remove([sx, ground_y, ex - sx])
        platforms.append([sx, ground_y, mid - sx])
        platforms.append([mid + 84, ground_y, ex - (mid + 84)])
        platforms.sort(key=lambda p: p[0])
        spikes.append([mid, 600, 84])
        segments = [(p[0], p[0] + p[2]) for p in platforms
                    if p[1] == ground_y]
    collectibles = []
    px = rng.randint(190, 240)
    py = 472
    while px < width - 300:
        w = rng.randint(112, 148)
        platforms.append([px, py, w])
        collectibles.append([px + w // 2, py - 30])
        py = max(258, min(478, py + rng.choice([-72, -44, 0, 44, 72])))
        px += rng.randint(205, 255)
    collectibles.append([width - 170, 528])
    return {
        "platforms": platforms,
        "collectibles": collectibles,
        "spikes": spikes,
        "spawn": [70, 500],
        "exit": [width - 70, 516],
        "width": width,
    }


# ---------------------------------------------------------------------------
# Config assembly (deterministic, then an optional brain proposal)

def deterministic_config(archetype: str, values: dict, palette: dict,
                         units: int) -> tuple[dict, dict]:
    """(config, extras) — extras carries solver output for crates."""
    config: dict = {"archetype": archetype, **palette,
                    "objective_hint": _HINTS[archetype]}
    extras: dict = {}
    minutes = skeleton_module._session_minutes(
        values.get("Session length (one sitting)", ""))
    round_seconds = skeleton_module._round_seconds(minutes)
    if archetype == "arena_collector":
        config.update({
            "rounds": units,
            "targets_per_round": 5,
            "round_seconds": round_seconds,
            "player_speed": 330.0,
            "hazard_speed": 1.4,
        })
    elif archetype == "platformer":
        lives = _scope_int(
            values,
            ("Lose / fail condition", "Difficulty target (who it is for, how hard)"),
            r"(\d+)\s*(?:lives|life)",
        ) or 3
        lose_text = values.get("Lose / fail condition", "").lower()
        timer = round_seconds if "time" in lose_text else 0
        import zlib
        seed = zlib.crc32(values.get("Title (working)", "").encode("utf-8")) + units
        rng = random.Random(seed)
        config.update({
            "lives": lives,
            "player_speed": 280.0,
            "jump_strength": 545.0,
            "gravity": 1400.0,
            "timer_seconds": timer,
            "levels": [_platformer_level(rng, i) for i in range(units)],
        })
    else:  # crate_pusher
        budget_override = _scope_int(
            values,
            ("Win condition", "Difficulty target (who it is for, how hard)",
             "Session length (one sitting)"),
            r"(\d+)\s*moves",
        )
        puzzles = crate_puzzles(units, budget_override=budget_override)
        config["puzzles"] = puzzles
        extras["puzzle_solutions"] = [p["solution"] for p in puzzles]
    return config, extras


def units_delivered(archetype: str, config: dict) -> int:
    if archetype == "platformer":
        return len(config.get("levels", []))
    if archetype == "crate_pusher":
        return len(config.get("puzzles", []))
    return int(config.get("rounds", 0))


_BRAIN_SYSTEM = (
    "You tune a small set of numbers for a generated game. Answer "
    "with one JSON object only — no prose. Keys are the exact names "
    "given; values are numbers inside the given ranges. Choose "
    "values that make the game feel fair and lively for its scope."
)


def brain_proposal(project, archetype: str, config: dict,
                     values: dict, settings_path=None) -> tuple[dict, bool, str]:
    """Ask the mid tier for tuning numbers; validate + clamp them.

    Returns (config, used, note). Any failure — no brain, unusable
    tier, bad JSON, budget refusal — returns the deterministic
    config untouched with used=False and the reason in the note.
    """
    _, data = brain_module.resolve_settings(settings_path, project)
    status = brain_module.tier_status(data, "mid") if data else None
    if not status or not status["usable"]:
        reason = status["reason"] if status else "no settings file found"
        return config, False, f"no usable mid-tier model ({reason})"
    bounds = BOUNDS[archetype]
    wanted = {key: {"current": config.get(key), "min": lo, "max": hi}
              for key, (lo, hi) in bounds.items() if key in config
              or key == "move_budget_multiplier"}
    prompt = (
        "Game archetype: " + archetype + "\n"
        "Scope in brief: " + "; ".join(
            f"{name} = {values.get(name, '')}" for name in (
                "Genre / archetype", "Core verb (what the player does most)",
                "Difficulty target (who it is for, how hard)")) + "\n"
        "Tune these numbers (name: {current, min, max}):\n"
        + json.dumps(wanted) + "\nReturn the JSON object now."
    )
    try:
        result = brain_module.complete(
            project, "mid",
            [{"role": "system", "content": _BRAIN_SYSTEM},
             {"role": "user", "content": prompt}],
            stage="build", settings_path=settings_path, max_tokens=240)
    except brain_module.BrainBudgetError as exc:
        return config, False, f"brain refused by budget ({exc})"
    if not result.ok:
        return config, False, f"brain call failed ({result.error})"
    answers = brain_module._parse_extraction(result.text)
    tuned = dict(config)
    applied = []
    for key, (lo, hi) in bounds.items():
        if key not in answers:
            continue
        try:
            value = float(answers[key])
        except (TypeError, ValueError):
            continue
        value = min(max(value, lo), hi)
        if key == "move_budget_multiplier":
            if archetype == "crate_pusher" and tuned.get("puzzles"):
                tuned["puzzles"] = [
                    dict(p, budget=max(40, int(len(p["solution"]) * value) + 20))
                    for p in tuned["puzzles"]
                ]
                applied.append(key)
            continue
        if key in tuned:
            tuned[key] = int(value) if isinstance(tuned[key], int) else value
            applied.append(key)
    if not applied:
        return config, False, "brain replied but no usable numbers were in range"
    note = ("mid tier (%s/%s) proposed, validated and clamped: %s"
            % (result.provider, result.model, ", ".join(sorted(applied))))
    if result.warning:
        note += " " + result.warning
    return tuned, True, note


# ---------------------------------------------------------------------------
# Writing the game

def _template_text(*parts: str) -> str:
    resource = resources.files(__package__).joinpath("game_templates", *parts)
    return resource.read_text(encoding="utf-8")


TEMPLATE_DIRS = {
    "arena_collector": "arena",
    "platformer": "platformer",
    "crate_pusher": "crate",
}


def _archetype_files(archetype: str) -> dict:
    """Generated game files: {path relative to game/, template parts}."""
    tdir = TEMPLATE_DIRS[archetype]
    files = {
        "scripts/asset_loader.gd": ("shared", "asset_loader.gd"),
        "tests/competent_win.gd": (tdir, "competent_win.gd"),
        "tests/competent_lose.gd": (tdir, "competent_lose.gd"),
        "tests/breaker.gd": (tdir, "breaker.gd"),
        "tests/first_time.gd": (tdir, "first_time.gd"),
        "smoke/smoke.gd": (tdir, "competent_win.gd"),
        "scripts/game.gd": (tdir, "game.gd"),
    }
    if archetype in ("arena_collector", "platformer"):
        files["scripts/player.gd"] = (tdir, "player.gd")
    if archetype == "arena_collector":
        files["scripts/marker.gd"] = (tdir, "marker.gd")
    if archetype == "platformer":
        files["scripts/star.gd"] = (tdir, "star.gd")
        files["scripts/door_draw.gd"] = (tdir, "door_draw.gd")
    return files


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


def _write_report(project_dir, report: dict) -> None:
    path = report_path_for(project_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")


def build_game(project_dir, archetype: str | None = None, godot_path=None,
               validate: bool = True, settings_path=None,
               use_brain: bool = True) -> dict:
    """Generate the real game for a project. Returns the report.

    Requires the skeleton to exist (``game/project.godot``) and the
    scope to be complete and approved. On success the game is
    validated in Godot, committed and tagged ``known-good/build``.
    Raises GenerationError with a plain reason otherwise (the
    report is still written when validation fails).
    """
    project = Path(project_dir)
    game_dir = project / GAME_DIR_NAME
    if not (game_dir / "project.godot").exists():
        raise GenerationError(
            "No skeleton game found — build the skeleton first "
            f"('weaverbird skeleton build {project}'), then generate "
            "the real game on top of it."
        )
    scope_file = scope_module.scope_path_for(project)
    if not scope_file.exists():
        raise GenerationError(f"No scope file found at {scope_file}.")
    parsed = scope_module.load_scope(scope_file)
    if not parsed.is_complete:
        missing = ", ".join(f.name for f in parsed.missing_fields)
        raise GenerationError(
            f"Scope is incomplete, so the game cannot be generated — "
            f"still missing: {missing}."
        )
    if not parsed.is_approved:
        raise GenerationError(
            "The scope is complete but not approved yet, so the game "
            "cannot be generated."
        )

    values = _values(parsed)
    chosen, why = detect_archetype(values, override=archetype)
    palette = _palette(values)
    units = content_units(values)
    config, _extras = deterministic_config(chosen, values, palette, units)

    brain_used = False
    brain_note = "brain not attempted"
    if use_brain:
        config, brain_used, brain_note = brain_proposal(
            project, chosen, config, values, settings_path=settings_path)

    written: list[str] = []
    file_map = _archetype_files(chosen)
    for rel, parts in sorted(file_map.items()):
        dest = game_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(_template_text(*parts), encoding="utf-8")
        written.append(f"{GAME_DIR_NAME}/{rel}")
    config_rel = f"{GAME_DIR_NAME}/game_config.json"
    (game_dir / "game_config.json").write_text(
        json.dumps(config, indent=2) + "\n", encoding="utf-8")
    written.append(config_rel)
    written.sort()

    report = {
        "stage": "build",
        "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "archetype": chosen,
        "archetype_reason": why,
        "brain_used": brain_used,
        "brain_note": brain_note,
        "config": {k: v for k, v in config.items()
                   if k not in ("levels", "puzzles")},
        "units_delivered": units_delivered(chosen, config),
        "units_requested": units,
        "files": written,
        "validation": {"ran": False, "passed": False},
        "known_good_tag": None,
        "commit": None,
    }

    if validate:
        report["validation"] = skeleton_module.validate_project(
            project, godot_path=godot_path)

    for rel in written:
        provenance_module.add_record(
            project, rel, "code", provider=PROVENANCE_PROVIDER,
            note=f"weaverbird {chosen} generator")
    if not brain_used:
        ledger_module.add_entry(project, "build", "cheap", 0, 0.0)
    _write_report(project, report)

    if validate and not report["validation"]["passed"]:
        digest = skeleton_module._failure_digest(report["validation"])
        raise GenerationError(
            "Godot validation failed, so the generated game is not "
            f"known-good and was not tagged. {digest} "
            f"Full report: {report_path_for(project)}"
        )

    vcs_module.init_repo(project)
    vcs_module.ensure_local_identity(project)
    try:
        report["commit"] = vcs_module.commit_all(
            project, f"Weaverbird build: {chosen} game from the approved scope")
    except vcs_module.VcsError:
        report["commit"] = None
    if validate and KNOWN_GOOD_TAG not in vcs_module.list_tags(project):
        vcs_module.tag_known_good(project, KNOWN_GOOD_TAG)
    if validate:
        report["known_good_tag"] = KNOWN_GOOD_TAG
    _write_report(project, report)
    return report


# ---------------------------------------------------------------------------
# Pipeline wiring

def build_stage_handler(project_dir, state=None) -> bool:
    """Handler for leaving the build stage: the generated game must
    exist and be validated (build it if it is not)."""
    project = Path(project_dir)
    report = load_report(project)
    if (
        report
        and report.get("validation", {}).get("passed")
        and (project / GAME_DIR_NAME / "game_config.json").exists()
    ):
        return True
    build_game(project)
    return True


def register_stage_handlers() -> None:
    state_module.register_handler("build", build_stage_handler)
