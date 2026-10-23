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
# Every named preset, including the 3D one (0.8.0). ARCHETYPES
# remains the historical 2D trio where a caller truly means it.
PRESET_NAMES = ARCHETYPES + ("arena_3d",)

_INT_RE = re.compile(r"\d+")
_ARCHETYPE_RE = re.compile(
    r"archetype\s*[:=]\s*(arena_3d|arena_collector|platformer|crate_pusher|arena|crate)",
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
    "arena_3d": {
        "player_speed": (7.0, 16.0),
        "hazard_speed": (0.6, 2.4),
        "targets_per_round": (3, 10),
    },
}

_HINTS = {
    "arena_collector":
        "Collect every glowing target before the time runs out. Avoid the red hazards!",
    "platformer":
        "Gather the stars and reach the glowing door. Mind the spikes!",
    "crate_pusher":
        "Push every crate onto a glowing pad. E undoes a step.",
    "arena_3d":
        "Collect every glowing orb before the time runs out. Avoid the patrolling hazards!",
    "bespoke":
        "Follow the on-screen objective.",
}


# ---------------------------------------------------------------------------
# Composition — the archetype-independent decision layer (0.7.0)
#
# A game is described by composing primitives, not by naming an
# archetype: how the player moves, what the objective is, how the
# run can fail, how the content is structured, and which
# interactions exist. The three historical archetypes are PRESETS —
# named compositions kept for convenience — and generation is
# driven by the composition. A scope whose core verb no primitive
# combination can express is "bespoke": a brain-written mechanics
# module behind the documented module contract (mechanics.py),
# never a silent fallback to an unrelated preset.

PRESETS: dict[str, dict] = {
    "arena_collector": {
        "dimension": "2d",
        "movement": "top_down",
        "objectives": ["collect"],
        "fail": ["timer", "hazard_contact"],
        "structure": "rounds",
        "interactions": ["avoid", "collect"],
    },
    "platformer": {
        "dimension": "2d",
        "movement": "platform",
        "objectives": ["collect", "reach_exit"],
        "fail": ["hazard_contact", "lives"],
        "structure": "levels",
        "interactions": ["avoid", "collect"],
    },
    "crate_pusher": {
        "dimension": "2d",
        "movement": "grid",
        "objectives": ["push_to_targets"],
        "fail": ["move_budget"],
        "structure": "levels",
        "interactions": ["push"],
    },
    # The 3D composition (0.8.0): the arena's primitives lifted
    # into a dimension — top-down movement and collecting on a 3D
    # ground plane, patrolling hazards, obstacle blocks. Flat
    # ground only: 3D platforming is not claimed anywhere.
    "arena_3d": {
        "dimension": "3d",
        "movement": "top_down",
        "objectives": ["collect"],
        "fail": ["timer", "hazard_contact"],
        "structure": "rounds",
        "interactions": ["avoid", "collect"],
    },
}

_PRESET_RE = re.compile(
    r"preset\s*[:=]\s*(arena_3d|arena_collector|platformer|crate_pusher|arena|crate)",
    re.IGNORECASE,
)
_COMPOSITION_RE = re.compile(r"composition\s*[:=]\s*([a-z_]+)", re.IGNORECASE)

# Positive evidence that a scope's core loop cannot be expressed by
# composing the current primitives. The matcher is deliberately
# conservative: bespoke requires one of these signals AND no
# primitive signal at all, so an ordinary scope (or a vague one)
# keeps the historical default instead of demanding a brain.
_BESPOKE_SIGNALS = (
    "negotiat", "dialogue", "conversation", "romance", "dating sim",
    "interrogat", "detective", "city build", "city-build",
    "farming sim", "trade empire", "compose music", "dance battle",
    "courtroom", "debate",
)
_PRIMITIVE_SIGNALS = (
    "platform", "jump", "side-scroll", "side scroll", "sidescroll",
    "puzzle", "sokoban", "crate", "push", "collect", "gather",
    "arena", "surviv", "reach", "race", "shoot", "dodge", "avoid",
    "run and", "runner", "wave",
)


def composition_for_preset(name: str) -> dict:
    """The composition a named preset stands for (a fresh copy)."""
    preset = _normalise_archetype(name)
    comp = PRESETS[preset]
    return {
        "dimension": comp.get("dimension", "2d"),
        "movement": comp["movement"],
        "objectives": list(comp["objectives"]),
        "fail": list(comp["fail"]),
        "structure": comp["structure"],
        "interactions": list(comp["interactions"]),
    }


def preset_for_composition(composition: dict | None) -> str | None:
    """The preset a composition matches exactly, or None."""
    if not isinstance(composition, dict):
        return None
    for name in PRESET_NAMES:
        comp = PRESETS[name]
        if (composition.get("dimension", "2d") == comp.get("dimension", "2d")
                and composition.get("movement") == comp["movement"]
                and list(composition.get("objectives") or []) == comp["objectives"]
                and composition.get("structure") == comp["structure"]):
            return name
    return None


_DIMENSION_3D_RE = re.compile(r"\b3\s*d\b")


def scope_dimension(values: dict) -> str:
    """The dimension the scope asks for: the Dimension field
    first, then a '3D' mention in the descriptive text. Default
    '2d' — the historical behaviour."""
    field = str(values.get("Dimension", "") or "").lower()
    if _DIMENSION_3D_RE.search(field):
        return "3d"
    if re.search(r"\b2\s*d\b", field):
        return "2d"
    text = " ".join(
        str(values.get(name, ""))
        for name in ("Genre / archetype", "Title (working)",
                     "One-line pitch",
                     "Core verb (what the player does most)")).lower()
    if _DIMENSION_3D_RE.search(text):
        return "3d"
    return "2d"


def _scope_text(values: dict) -> str:
    return " ".join(
        str(values.get(name, ""))
        for name in ("Genre / archetype",
                     "Core verb (what the player does most)",
                     "Win condition", "Lose / fail condition")
    ).lower()


def bespoke_reason(values: dict) -> str | None:
    """Why this scope needs bespoke mechanics, or None.

    Conservative by design (see _BESPOKE_SIGNALS): a bespoke signal
    with no primitive signal anywhere in the genre/verb/win/lose
    text. Anything else composes — or falls back to the arena
    preset exactly as detection always has.
    """
    text = _scope_text(values)
    hits = [signal for signal in _BESPOKE_SIGNALS if signal in text]
    if not hits:
        return None
    if any(signal in text for signal in _PRIMITIVE_SIGNALS):
        return None
    return (f"the scope's core loop ({hits[0]!r}) cannot be composed "
            "from the movement/objective/fail primitives")


def _apply_dimension(derived: dict, values: dict) -> dict:
    """Lift a derived composition into its dimension.

    The dimension is part of the composition (0.8.0): when the
    scope asks for 3D and the derived preset is the arena, the
    build becomes the arena's 3D composition. For any other
    preset a 3D request is recorded honestly in the reason and
    the game is built in 2D — 3D platforming and 3D puzzles are
    not claimed. A scope already naming the 3D preset (or an
    override) passes through untouched."""
    if derived.get("bespoke") or not derived.get("preset"):
        return derived
    if derived["preset"] == "arena_3d":
        return derived
    if scope_dimension(values) != "3d":
        return derived
    if derived["preset"] == "arena_collector":
        return {
            "composition": composition_for_preset("arena_3d"),
            "preset": "arena_3d", "bespoke": False,
            "reason": derived["reason"]
                      + "; the scope's dimension is 3D, so the "
                        "arena composition is built as its 3D "
                        "preset (arena_3d)",
        }
    return {
        **derived,
        "reason": derived["reason"]
                  + "; the scope asks for 3D, but only the arena "
                    "composition supports 3D today — built in 2D",
    }


def derive_composition(values: dict, override: str | None = None) -> dict:
    """The build's decision record: {composition, preset, bespoke,
    reason}.

    An explicit override, an ``archetype:``/``preset:`` note, or a
    ``composition: <preset-name>`` note in the scope wins; bespoke
    detection comes next; otherwise the historical archetype
    detection picks the preset whose composition the scope
    describes — and the scope's dimension then lifts an arena
    composition into its 3D preset when 3D is asked for. The
    derivation is recorded in the generation report and in
    game_config.json.
    """
    if override:
        preset = _normalise_archetype(override)
        return {"composition": composition_for_preset(preset),
                "preset": preset, "bespoke": False,
                "reason": f"explicit override ({override!r})"}
    for value in values.values():
        match = _PRESET_RE.search(value or "") or \
            _ARCHETYPE_RE.search(value or "")
        if match:
            preset = _normalise_archetype(match.group(1))
            return _apply_dimension(
                {"composition": composition_for_preset(preset),
                 "preset": preset, "bespoke": False,
                 "reason": f"the scope names the preset "
                           f"({match.group(1)!r})"}, values)
        match = _COMPOSITION_RE.search(value or "")
        if match and match.group(1).lower() in PRESETS:
            preset = match.group(1).lower()
            return _apply_dimension(
                {"composition": composition_for_preset(preset),
                 "preset": preset, "bespoke": False,
                 "reason": f"the scope names the composition "
                           f"({preset!r})"}, values)
    why_bespoke = bespoke_reason(values)
    if why_bespoke:
        return {"composition": None, "preset": None, "bespoke": True,
                "reason": why_bespoke}
    preset, why = detect_archetype(values)
    return _apply_dimension(
        {"composition": composition_for_preset(preset),
         "preset": preset, "bespoke": False,
         "reason": f"derived from the scope — {why}"}, values)


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
               "sokoban": "crate_pusher", "arena-3d": "arena_3d",
               "arena3d": "arena_3d"}
    name = aliases.get(name, name)
    if name not in PRESET_NAMES:
        raise GenerationError(
            f"Unknown archetype {name!r}. Known: {', '.join(PRESET_NAMES)}."
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


# ---------------------------------------------------------------------------
# Scope-stated target counts (0.10.0, finding F7)
#
# The scope's Win condition often states the win target outright —
# "Collect 10 stars each round" — while generation quietly used
# its default of 5, and nothing ever compared the two. Now the
# number is parsed, used, and recorded, so the finish check can
# reconcile what the scope said against what was built.
#
# The patterns are deliberately conservative — a target count is
# claimed ONLY from:
#   * a collecting verb followed by a number: "collect 10",
#     "gather 7", "grab 3", "pick up 5";
#   * a number followed by a collectible noun: "10 stars",
#     "7 targets", "500 points to win", "3 orbs".
# Anything else (a bare number, "3 rounds", "100%") makes no claim
# here; finish.py reports numeric-but-unmatched win text plainly
# rather than guessing.

TARGET_COUNT_FIELDS = ("Win condition", "Lose / fail condition")
_TARGET_VERB_RE = re.compile(
    r"\b(?:collect|gather|grab|pick\s+up)\s+(\d{1,4})\b", re.IGNORECASE)
_TARGET_NOUN_RE = re.compile(
    r"\b(\d{1,4})\s+(?:stars?|targets?|points?|orbs?|coins?|gems?"
    r"|items?|crystals?)\b", re.IGNORECASE)
# The world gate's own range for targets per round: a scope number
# outside it is clamped, and the clamping is recorded, never silent.
TARGET_COUNT_MIN = 1
TARGET_COUNT_MAX = 12


def scope_target_count(values: dict) -> dict | None:
    """The target count the scope states, or None when it states
    none (in the conservative patterns above).

    Returns {"stated": the number as written, "used": the number
    generation applies (stated clamped to the world gate's range),
    "field": the scope field it came from, "matched": the exact
    text that matched}."""
    for field in TARGET_COUNT_FIELDS:
        text = str(values.get(field, "") or "")
        if not text:
            continue
        for pattern in (_TARGET_VERB_RE, _TARGET_NOUN_RE):
            match = pattern.search(text)
            if match:
                stated = int(match.group(1))
                return {
                    "stated": stated,
                    "used": min(max(stated, TARGET_COUNT_MIN),
                                TARGET_COUNT_MAX),
                    "field": field,
                    "matched": match.group(0),
                }
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


def _palette_field_hexes(values: dict) -> list[str]:
    """Hex colours written in the scope's Palette field alone, in
    order, normalised to #RRGGBB. The style lock may pin palette
    roles by position: 1 accent, 2 target, 3 hazard, 4 platform,
    5 crate. Roles it does not pin are derived from the accent."""
    found = skeleton_module._HEX_RE.findall(values.get("Palette", "") or "")
    hexes = []
    for digits in found:
        if len(digits) == 3:
            digits = "".join(ch * 2 for ch in digits)
        hexes.append("#" + digits.upper())
    return hexes


def _palette(values: dict) -> dict:
    accent, target, accent_hex = skeleton_module._scope_colours(values)
    target_hex = _hex_of(target)
    pinned = _palette_field_hexes(values)

    def pin(index: int, derived: str) -> str:
        return pinned[index] if len(pinned) > index else derived

    return {
        "accent_hex": accent_hex,
        "target_hex": target_hex,
        "hazard_hex": pin(2, _shift_hex(accent_hex, 0.5, saturation=0.72,
                                        lightness=0.55)),
        "platform_hex": pin(3, _shift_hex(accent_hex, 0.0, saturation=0.18,
                                          lightness=0.30)),
        "crate_hex": pin(4, _shift_hex(accent_hex, 0.06, saturation=0.48,
                                       lightness=0.40)),
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
    staircase of floating platforms carrying the stars.

    Traversable by construction means more than "each step is within
    jump reach of the previous one": a player who misses a jump must
    be able to fall, land, and climb again. So the staircase is a
    true stair — consecutive steps overlap in x or start within
    36px of the previous step's end, each step rises or drops by at
    most 64px (the generated jump clears ~106px; its landing window
    for a 64px rise spans roughly 40–177px of travel), and the first
    step is within jump reach of the ground (rise <= 80, landing
    window roughly 55–163px of travel). A fall
    therefore lands on a lower step or on the ground next to the
    staircase base — never in a pocket under a platform that is
    only reachable via a perfect no-fall chain.

    The ground is built after the staircase for the mirror-image
    reason: a spike pit must be jumpable from the ground, and a
    low step (top below ~396px, so its underside is within a
    ground jump's head arc) hovering over the pit or its takeoff
    turns that jump into a head-bonk and a death. Pits are only
    cut where no low step hangs over them."""
    width = min(1500 + index * 90, 2100)
    ground_y = 560

    # The staircase first (see the docstring for its invariants).
    steps: list[list[int]] = []
    collectibles = []
    step_x = rng.randint(190, 240)
    step_y = rng.randint(480, 492)
    while step_x < width - 300:
        w = rng.randint(112, 148)
        steps.append([step_x, step_y, w])
        collectibles.append([step_x + w // 2, step_y - 30])
        step_y = int(max(296, min(492, step_y + rng.choice(
            [-64, -44, -24, 0, 24, 44, 64]))))
        step_x = step_x + w + rng.randint(-24, 36)
    collectibles.append([width - 170, 528])
    # A ground jump's head peaks ~418px (feet 542 - 106 rise - 18
    # half-height), so a step is "low" — able to bonk that jump —
    # when its underside (top + 22) is below 418, i.e. top > 396.
    low_ranges = [(sx, sx + w) for sx, sy, w in steps if sy > 396]

    def pit_blocked(a: int, b: int) -> bool:
        """Would a pit spanning [a, b] sit under a low step's arc?

        The jump that crosses a pit takes off up to ~160px before
        it, so the step must clear that whole approach span."""
        return any(a - 160 < rx1 and rx0 < b for rx0, rx1 in low_ranges)

    segments: list[tuple[int, int]] = []
    x = 0
    while x < width:
        seg_len = rng.randint(300, 430) if not segments else rng.randint(230, 370)
        end = min(x + seg_len, width)
        if end >= width:
            segments.append((x, width))
            break
        pit_w = rng.randint(60, 105)
        if pit_blocked(end, end + pit_w):
            # No pit here: the ground runs on under the low step.
            end = min(end + pit_w + rng.randint(60, 120), width)
            segments.append((x, end))
            if end >= width:
                break
            x = end
            continue
        segments.append((x, end))
        x = end + pit_w
    if segments[-1][1] < width:
        last = segments[-1]
        segments[-1] = (last[0], width)
    # The exit and the final star stand on the final ground
    # segment: if that segment rolled short (under 200px, so the
    # star at width-170 could hang over the preceding pit), merge
    # it into its predecessor, dropping the last pit.
    if len(segments) > 1 and width - segments[-1][0] < 200:
        segments[-2:] = [(segments[-2][0], width)]
    platforms = [[sx, ground_y, ex - sx] for sx, ex in segments]
    spikes = []
    for (sx, ex), (nx, _ne) in zip(segments, segments[1:]):
        if nx > ex:
            spikes.append([ex, 600, nx - ex])
    if not spikes:  # guarantee at least one pit per level
        placed = False
        for sx, ex in segments:
            a = sx + 40
            while a + 84 <= ex - 20:
                if not pit_blocked(a, a + 84):
                    platforms.remove([sx, ground_y, ex - sx])
                    platforms.append([sx, ground_y, a - sx])
                    platforms.append([a + 84, ground_y, ex - (a + 84)])
                    platforms.sort(key=lambda p: p[0])
                    spikes.append([a, 600, 84])
                    placed = True
                    break
                a += 20
            if placed:
                break
        if not placed:
            raise ValueError(
                "could not place a single spike pit clear of low steps")
    platforms.extend(steps)
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
        # The seed the world worker lays the rounds out from
        # (0.10.0 layout patterns): derived from the project's
        # identity, so the same project re-lays-out identically
        # and another project gets another layout.
        import zlib
        seed = zlib.crc32(
            values.get("Title (working)", "").encode("utf-8")) + units
        config.update({
            "rounds": units,
            "targets_per_round": 5,
            "round_seconds": round_seconds,
            "player_speed": 330.0,
            "hazard_speed": 1.4,
            "world_seed": seed,
        })
        _apply_scope_target_count(config, values)
    elif archetype == "arena_3d":
        # The same arena tuning, in 3D units (metres, metres per
        # second), plus the seed the world worker lays the ground
        # plane out from.
        import zlib
        seed = zlib.crc32(
            values.get("Title (working)", "").encode("utf-8")) + units
        config.update({
            "rounds": units,
            "targets_per_round": 5,
            "round_seconds": round_seconds,
            "player_speed": 11.0,
            "hazard_speed": 1.4,
            "world_seed": seed,
        })
        _apply_scope_target_count(config, values)
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


def _apply_scope_target_count(config: dict, values: dict) -> None:
    """A scope-stated target count beats the default (F7): when
    the Win/Lose text states one, targets_per_round IS that
    number (clamped to the world gate's range), and the
    derivation is recorded in the config — it lands in
    game_config.json and the generation report, where the finish
    check reconciles it."""
    found = scope_target_count(values)
    if found is None:
        return
    config["targets_per_round"] = found["used"]
    config["scope_numbers"] = {
        "targets_per_round": {
            "stated": found["stated"],
            "used": found["used"],
            "field": found["field"],
            "matched": found["matched"],
        }
    }


def units_delivered(archetype: str, config: dict) -> int:
    if archetype == "platformer":
        return len(config.get("levels", []))
    if archetype == "crate_pusher":
        return len(config.get("puzzles", []))
    return int(config.get("rounds", 0))


def config_dimension(archetype: str) -> str:
    """The dimension a preset's game is built in."""
    return PRESETS.get(archetype, {}).get("dimension", "2d")


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
    # A scope-stated target count is the maker's spec, not a feel
    # knob: the brain tunes around it, never over it (F7).
    pinned = {"targets_per_round"} if "scope_numbers" in config else set()
    wanted = {key: {"current": config.get(key), "min": lo, "max": hi}
              for key, (lo, hi) in bounds.items()
              if key not in pinned
              and (key in config or key == "move_budget_multiplier")}
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
        if key in pinned or key not in answers:
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
# The build summary — what WILL be built, before anything runs
#
# The project page shows this ahead of the first run (0.10.0): the
# composition generation derives from the scope, and the key
# numbers it will use, each flagged "from your scope" or "default"
# — so a scope-stated number (F7) is visible before generation,
# not discovered after it. Everything here is derived from the
# scope plus the same defaults deterministic_config uses; nothing
# is built and nothing is spent.

_UNIT_WORDS = {"platformer": "levels", "crate_pusher": "puzzles"}


def build_summary(project_dir) -> dict:
    """The pre-build summary for a project (see the note above).

    Tolerant of a missing or partial scope: the composition is
    still derived from whatever text exists (the same derivation
    generation will run), and `complete` says how firm it is."""
    from . import world as world_module

    project = Path(project_dir)
    values: dict = {}
    parsed = None
    scope_file = scope_module.scope_path_for(project)
    if scope_file.exists():
        try:
            parsed = scope_module.load_scope(scope_file)
        except scope_module.ScopeError:
            parsed = None
    if parsed is not None:
        values = _values(parsed)
    derived = derive_composition(values)
    preset = derived["preset"]
    units = content_units(values)
    palette = _palette(values)
    minutes = skeleton_module._session_minutes(
        values.get("Session length (one sitting)", ""))
    round_seconds = skeleton_module._round_seconds(minutes)

    numbers: list[dict] = []
    unit_word = _UNIT_WORDS.get(preset or "", "rounds")
    numbers.append({
        "label": f"Content units ({unit_word})", "value": units,
        "source": "scope" if _INT_RE.search(
            values.get("Content counts (levels / waves / rooms / "
                       "items, as applicable)", "") or "") else "default",
    })
    if preset in ("arena_collector", "arena_3d"):
        found = scope_target_count(values)
        numbers.append({
            "label": "Targets per round",
            "value": found["used"] if found else 5,
            "source": "scope" if found else "default",
            "note": (f"your scope says {found['stated']}"
                     if found and found["stated"] != found["used"]
                     else ""),
        })
        numbers.append({
            "label": "Round time limit", "value": f"{round_seconds}s",
            "source": "scope" if minutes else "default", "note": "",
        })
        numbers.append({
            "label": "Hazards",
            "value": ("2 patrols in round 1, +1 per round, up to 4, "
                      "plus obstacle blocks" if preset == "arena_3d"
                      else "2 in round 1, +1 per round, up to 5"),
            "source": "default", "note": "",
        })
    elif preset == "platformer":
        lives = _scope_int(
            values,
            ("Lose / fail condition",
             "Difficulty target (who it is for, how hard)"),
            r"(\d+)\s*(?:lives|life)")
        numbers.append({
            "label": "Lives", "value": lives or 3,
            "source": "scope" if lives else "default", "note": "",
        })
        numbers.append({
            "label": "Hazards", "value": "spike pits in every level",
            "source": "default", "note": "",
        })
    composition = derived["composition"] or {}
    return {
        "scope_present": parsed is not None,
        "complete": bool(parsed and parsed.is_complete),
        "approved": bool(parsed and parsed.is_approved),
        "title": values.get("Title (working)", ""),
        "preset": preset,
        "bespoke": bool(derived["bespoke"]),
        "reason": derived["reason"],
        "composition": derived["composition"],
        "dimension": composition.get("dimension", "2d"),
        "units": units,
        "unit_word": unit_word,
        "numbers": numbers,
        "palette": palette,
        "style": values.get(
            "Style sample chosen (id / link to approved sample)", ""),
        "asset_kinds": list(
            world_module.ENTITY_KINDS.get(preset or "", [])),
    }


# ---------------------------------------------------------------------------
# Writing the game

def _template_text(*parts: str) -> str:
    resource = resources.files(__package__).joinpath("game_templates", *parts)
    return resource.read_text(encoding="utf-8")


TEMPLATE_DIRS = {
    "arena_collector": "arena",
    "platformer": "platformer",
    "crate_pusher": "crate",
    "arena_3d": "arena3d",
}


def _archetype_files(archetype: str) -> dict:
    """Generated game files: {path relative to game/, template parts}."""
    tdir = TEMPLATE_DIRS[archetype]
    files = {
        "scripts/asset_loader.gd": ("shared", "asset_loader.gd"),
        "tests/savecheck.gd": ("shared", "savecheck.gd"),
        "tests/model_bridge.gd": ("shared", "model_bridge.gd"),
        "tests/competent_win.gd": (tdir, "competent_win.gd"),
        "tests/competent_lose.gd": (tdir, "competent_lose.gd"),
        "tests/breaker.gd": (tdir, "breaker.gd"),
        "tests/first_time.gd": (tdir, "first_time.gd"),
        "smoke/smoke.gd": (tdir, "competent_win.gd"),
        "scripts/game.gd": (tdir, "game.gd"),
    }
    if archetype in ("arena_collector", "platformer"):
        files["scripts/player.gd"] = (tdir, "player.gd")
    if archetype == "arena_3d":
        # The 3D player, and the game scene itself: the skeleton's
        # scene has a Node2D root, so the mechanics worker writes
        # the Node3D scene the 3D game needs.
        files["scripts/player.gd"] = (tdir, "player.gd")
        files["scenes/game.tscn"] = (tdir, "game.tscn")
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


def prepare_build(project_dir, archetype: str | None = None,
                  settings_path=None, use_brain: bool = True,
                  reporter=None) -> dict:
    """The shared start of the world/mechanics/assembly steps:
    scope gates, the composition derivation, the palette, and the
    (deterministic, optionally brain-tuned) config. Returns the
    build context the three steps consume; the world worker
    persists it (build_context_path) so the mechanics and
    assembly workers re-run alone without re-deriving — or
    re-spending — anything.
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
    derived = derive_composition(values, override=archetype)
    palette = _palette(values)
    units = content_units(values)
    if derived["bespoke"]:
        config = {"archetype": "bespoke", **palette,
                  "objective_hint": _HINTS["bespoke"]}
        brain_used, brain_note = False, "bespoke mechanics — no preset tuning"
        if reporter is not None:
            reporter(f"Bespoke mechanics: {derived['reason']}; "
                     f"{units} content unit(s) requested")
    else:
        chosen = derived["preset"]
        config, _extras = deterministic_config(chosen, values, palette, units)
        if reporter is not None:
            reporter(f"Composition: {chosen} preset ({derived['reason']}); "
                     f"{units} content unit(s) from the scope")
        brain_used = False
        brain_note = "brain not attempted"
        if use_brain:
            config, brain_used, brain_note = brain_proposal(
                project, chosen, config, values, settings_path=settings_path)
            if reporter is not None:
                reporter(f"Tuning: {'proposed by the model' if brain_used else 'deterministic'} "
                         f"— {brain_note}")
    return {
        "values": values,
        "preset": derived["preset"],
        "composition": derived["composition"],
        "bespoke": derived["bespoke"],
        "reason": derived["reason"],
        "palette": palette,
        "units": units,
        "config": config,
        "brain_used": brain_used,
        "brain_note": brain_note,
    }


def build_context_path(project_dir) -> Path:
    return Path(project_dir) / ".weaverbird" / "build_context.json"


def save_build_context(project_dir, ctx: dict) -> None:
    path = build_context_path(project_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(ctx, indent=2) + "\n", encoding="utf-8")


def load_build_context(project_dir) -> dict | None:
    path = build_context_path(project_dir)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) and "config" in data else None


def run_world_step(project_dir, ctx: dict, attempt: int = 0):
    """The world worker's core: build + write + gate the world
    data. Returns (world_data, issues). On a retry (attempt > 0)
    the platformer levels are regenerated with a shifted seed —
    the one worker that genuinely self-corrects from its gate
    issues."""
    from . import world as world_module

    project = Path(project_dir)
    if attempt > 0 and ctx.get("preset") == "platformer":
        import zlib
        seed = zlib.crc32(
            ctx["values"].get("Title (working)", "").encode("utf-8")) \
            + ctx["units"] + attempt * 7919
        rng = random.Random(seed)
        ctx["config"]["levels"] = [
            _platformer_level(rng, i) for i in range(ctx["units"])]
    if ctx.get("bespoke"):
        world_data = world_module.bespoke_world(ctx)
    else:
        world_data = world_module.world_from_config(
            ctx["preset"], ctx["config"], ctx["composition"])
    world_module.write_world(project, world_data)
    issues = world_module.validate_world(world_data)
    return world_data, issues


def run_mechanics_step(project_dir, ctx: dict, world_data: dict):
    """The mechanics worker's core: write the scripts that
    interpret the world data. Returns (written, issues)."""
    from . import mechanics as mechanics_module

    project = Path(project_dir)
    written = mechanics_module.write_mechanics(project, ctx)
    issues = mechanics_module.check_coverage(world_data, ctx)
    return written, issues


def run_assembly_step(project_dir, ctx: dict, world_data: dict,
                      written: list, godot_path=None,
                      validate: bool = True, reporter=None) -> dict:
    """The assembly worker's core: wire shell + mechanics + world
    + assets into the runnable game — write game_config.json, run
    the full Godot validation suite, commit and tag
    known-good/build. Returns the generation report."""
    from . import world as world_module

    project = Path(project_dir)
    game_dir = project / GAME_DIR_NAME
    config = dict(ctx["config"])
    config["composition"] = ctx.get("composition")
    config["preset"] = ctx.get("preset")
    config["dimension"] = (ctx.get("composition") or {}).get(
        "dimension", "2d")
    written = list(written)
    config_rel = f"{GAME_DIR_NAME}/game_config.json"
    (game_dir / "game_config.json").write_text(
        json.dumps(config, indent=2) + "\n", encoding="utf-8")
    if config_rel not in written:
        written.append(config_rel)
    world_rel = f"{GAME_DIR_NAME}/world/world.json"
    if world_rel not in written:
        written.append(world_rel)
    written.sort()
    chosen = ctx.get("preset") or "bespoke"
    if reporter is not None:
        reporter(f"Assembled the {chosen} game ({len(written)} files)")

    report = {
        "stage": "build",
        "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "archetype": chosen,
        "archetype_reason": ctx["reason"],
        "preset": ctx.get("preset"),
        "composition": ctx.get("composition"),
        "bespoke": bool(ctx.get("bespoke")),
        "world_manifest": world_module.world_manifest(world_data),
        "brain_used": ctx.get("brain_used", False),
        "brain_note": ctx.get("brain_note", ""),
        "config": {k: v for k, v in config.items()
                   if k not in ("levels", "puzzles")},
        "units_delivered": (1 if ctx.get("bespoke") else
                            units_delivered(chosen, ctx["config"])),
        "units_requested": ctx["units"],
        "files": written,
        "validation": {"ran": False, "passed": False},
        "known_good_tag": None,
        "commit": None,
    }

    if validate:
        report["validation"] = skeleton_module.validate_project(
            project, godot_path=godot_path, reporter=reporter)

    for rel in written:
        provenance_module.add_record(
            project, rel, "code", provider=PROVENANCE_PROVIDER,
            note=f"weaverbird {chosen} generator")
    if not ctx.get("brain_used"):
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
    if validate and reporter is not None:
        reporter("Committed and tagged known-good/build")
    _write_report(project, report)
    return report


def build_game(project_dir, archetype: str | None = None, godot_path=None,
               validate: bool = True, settings_path=None,
               use_brain: bool = True, reporter=None) -> dict:
    """Generate the real game for a project. Returns the report.

    The composition of the three generation workers — world,
    mechanics, assembly — run in order, exactly as the
    orchestrator runs them; this entry point (and the CLI's
    `build game`) keeps its historical behaviour. On success the
    game is validated in Godot, committed and tagged
    ``known-good/build``.
    """
    project = Path(project_dir)
    ctx = prepare_build(project, archetype=archetype,
                        settings_path=settings_path, use_brain=use_brain,
                        reporter=reporter)
    save_build_context(project, ctx)
    world_data, issues = run_world_step(project, ctx)
    if issues:
        raise GenerationError(
            "The world worker's gate failed: "
            + "; ".join(i["detail"] for i in issues))
    written, issues = run_mechanics_step(project, ctx, world_data)
    if issues:
        raise GenerationError(
            "The mechanics worker's gate failed: "
            + "; ".join(i["detail"] for i in issues))
    return run_assembly_step(project, ctx, world_data, written,
                             godot_path=godot_path, validate=validate,
                             reporter=reporter)


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
