"""The world worker's library — the world's data, as data.

Split out of the old monolithic build stage in 0.7.0. The world
worker owns *where things are*: per-unit layouts (platformer level
geometry, arena round parameters, crate puzzle layouts) written
as ``game/world/world.json`` — units carrying geometry and
entities (platforms, spawn, objective positions, hazards, exits,
crates, pads). The mechanics worker's scripts interpret that
data at runtime; layouts are never baked into scripts.

The worker has its own gate, run before anything downstream:
structural validation of its output — platformer reachability
invariants (the generator's by-construction guarantees, checked
on the data), crate solvability through the generator's solver,
arena bounds sanity. A gate failure comes back as issues owned
by "world"; the orchestrator routes them here, and a retry
regenerates platformer levels with a different seed (see
generate.run_world_step).

Since 0.11.0 the world is built from the scope's PLAN (plan.py)
rather than a named preset: world_from_plan derives the units
from the plan's movement — top-down rounds with generated
layouts, 3D rounds on the ground plane, side-view levels,
grid puzzles — stamping the plan's entity behaviours onto the
units. world_from_config below remains for projects built by
older versions, whose worlds validate exactly as they always
did.
"""

from __future__ import annotations

import json
from pathlib import Path

from . import generate as generate_module
from . import plan as plan_module
from . import skeleton as skeleton_module

GAME_DIR_NAME = skeleton_module.GAME_DIR_NAME
WORLD_REL = f"{GAME_DIR_NAME}/world/world.json"
ARENA_SIZE = [1152, 648]

# Entity kinds each composition's world can contain. The mechanics
# worker checks its rule set against the kinds actually present.
ENTITY_KINDS = {
    "arena_collector": ["player", "collectible", "hazard"],
    "platformer": ["player", "platform", "collectible", "hazard", "exit"],
    "crate_pusher": ["player", "wall", "crate", "pad"],
    "arena_3d": ["player", "collectible", "hazard", "obstacle"],
}

# The 3D arena's ground plane (x, z extents in metres) and the
# layout constants the gate checks against.
GROUND_3D = [40, 24]
SPAWN_3D = [0, -9]


def world_path_for(project_dir) -> Path:
    return Path(project_dir) / "game" / "world" / "world.json"


def load_world(project_dir) -> dict | None:
    path = world_path_for(project_dir)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def write_world(project_dir, world_data: dict) -> Path:
    path = world_path_for(project_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(world_data, indent=2) + "\n",
                    encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# 2D arena layouts (0.10.0) — collectibles are placed by PATTERN,
# never in the dead-straight row the old fixed spot list produced
# (the first five spots all sat at y≈110). The world worker owns
# the positions now: each round's unit carries its collectibles,
# its hazards and its spawn, and the mechanics template reads
# them from world.json, falling back to its built-in spots only
# when a world file predates layouts.
#
# Placement is deterministic: a per-project seed (the config's
# world_seed, derived from the project title) plus the round
# index pick a pattern and drive every random draw, so the same
# project lays out identically on every re-run and a different
# project lays out differently. Patterns are tried in a fixed
# rotation; one whose positions fail the layout checks (the same
# checks the world gate runs) falls back deterministically to the
# next, and the fallbacks are recorded in the unit.

SPAWN_2D = [576, 470]
# The 2D arena's hazard patrols: centre + horizontal range. The
# mechanics template's built-in table is this same list — the
# world data is the source of truth when present.
HAZARD_SPOTS_2D = [
    {"pos": [400, 330], "range": 170},
    {"pos": [740, 452], "range": 140},
    {"pos": [300, 200], "range": 130},
    {"pos": [850, 240], "range": 120},
    {"pos": [576, 380], "range": 180},
]
LAYOUT_PATTERNS = ("ring", "arc", "scattered", "clusters", "zigzag")
# Collectibles stay inside this box (the arena rect is
# (24,24)-(1128,624); the box keeps a margin from its edges).
_LAYOUT_BOX = (84, 84, 1068, 564)
_LAYOUT_MIN_PAIRWISE = 70.0
_LAYOUT_MIN_SPAWN = 90.0
_LAYOUT_MIN_HAZARD = 55.0


def _dist(a, b) -> float:
    return ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5


def _dist_to_segment(point, a, b) -> float:
    """Distance from a point to the segment a-b."""
    ax, ay = a
    bx, by = b
    px, py = point
    dx, dy = bx - ax, by - ay
    if dx == 0 and dy == 0:
        return _dist(point, a)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy)
                          / (dx * dx + dy * dy)))
    return _dist(point, [ax + t * dx, ay + t * dy])


def arena_layout_issues(positions, hazards, spawn) -> list[str]:
    """The layout checks, shared by generation (choosing a
    pattern) and the world gate (checking one): every collectible
    inside the layout box, no two too close, none on the spawn,
    none on a hazard's patrol line."""
    issues: list[str] = []
    x0, y0, x1, y1 = _LAYOUT_BOX
    for pos in positions:
        if len(pos) != 2 or not (x0 <= pos[0] <= x1
                                 and y0 <= pos[1] <= y1):
            issues.append(f"collectible {pos} sits outside the "
                          "arena's layout box")
            break
    for i, pos in enumerate(positions):
        for other in positions[i + 1:]:
            if _dist(pos, other) < _LAYOUT_MIN_PAIRWISE:
                issues.append(
                    f"collectibles {pos} and {other} are closer "
                    f"than {_LAYOUT_MIN_PAIRWISE:.0f}px")
                return issues
    for pos in positions:
        if _dist(pos, spawn) < _LAYOUT_MIN_SPAWN:
            issues.append(f"collectible {pos} sits too close to "
                          "the player spawn")
            return issues
        for hazard in hazards:
            hx, hy = hazard["pos"]
            reach = hazard["range"]
            if _dist_to_segment(pos, [hx - reach, hy],
                                [hx + reach, hy]) < _LAYOUT_MIN_HAZARD:
                issues.append(
                    f"collectible {pos} sits too close to a "
                    "hazard's patrol line")
                return issues
    return issues


def _layout_positions(pattern: str, count: int, rng,
                      hazards=(), spawn=None) -> list:
    """One pattern's candidate positions for `count` collectibles
    (before the layout checks judge them). The scattered pattern
    takes the hazards and spawn so its sampling can respect the
    clearances point by point instead of failing whole sets."""
    import math

    x0, y0, x1, y1 = _LAYOUT_BOX
    cx, cy = 576.0, 324.0
    if pattern == "ring":
        # The arena's patrol lines and the spawn rule out a big
        # centred ring (its lower arc would sit on the first
        # hazard's line), so the ring lives in the freer
        # upper-right space, squashed vertically. The radius also
        # grows to whatever the pairwise spacing needs.
        import math as _math
        chord_min = 76.0 / (2 * _math.sin(_math.pi / max(count, 2)))
        radius = rng.uniform(max(120.0, chord_min), 150.0)
        start = rng.uniform(0, 2 * math.pi)
        positions = []
        for i in range(count):
            angle = start + i * 2 * math.pi / count \
                + rng.uniform(-0.1, 0.1)
            r = radius + rng.uniform(-12, 12)
            positions.append([round(712 + r * math.cos(angle)),
                              round(268 + r * 0.66 * math.sin(angle))])
        return positions
    if pattern == "arc":
        radius = rng.uniform(430, 510)
        base_y = 660.0
        positions = []
        for i in range(count):
            frac = i / max(count - 1, 1)
            angle = math.pi * (0.16 + 0.68 * frac) \
                + rng.uniform(-0.03, 0.03)
            positions.append(
                [round(cx + radius * math.cos(angle)
                       + rng.uniform(-12, 12)),
                 round(base_y - radius * math.sin(angle)
                       + rng.uniform(-12, 12))])
        return positions
    if pattern == "clusters":
        centres = []
        for _ in range(2 if count <= 6 else 3):
            for _try in range(50):
                centre = [rng.uniform(x0 + 90, x1 - 90),
                          rng.uniform(y0 + 80, y1 - 80)]
                if all(_dist(centre, c) >= 250 for c in centres):
                    centres.append(centre)
                    break
            else:
                centres.append(centre)
        # Points land one at a time around their centre; a point
        # too close to an already-placed one is redrawn, so a
        # cluster is a loose knot, not a pile.
        positions = []
        for i in range(count):
            centre = centres[i % len(centres)]
            for _try in range(30):
                point = [round(min(max(
                    centre[0] + rng.gauss(0, 46), x0), x1)),
                    round(min(max(
                        centre[1] + rng.gauss(0, 40), y0), y1))]
                if all(_dist(point, p) >= _LAYOUT_MIN_PAIRWISE
                       for p in positions):
                    break
            positions.append(point)
        return positions
    if pattern == "zigzag":
        amplitude = rng.uniform(120, 185)
        phase = rng.uniform(0, 2 * math.pi)
        turns = rng.uniform(1.2, 2.0)
        positions = []
        for i in range(count):
            frac = i / max(count - 1, 1)
            positions.append(
                [round(x0 + frac * (x1 - x0) + rng.uniform(-14, 14)),
                 round(cy + amplitude
                       * math.sin(phase + frac * turns * math.pi)
                       + rng.uniform(-12, 12))])
        return positions
    # scattered: rejection sampling with a healthy minimum
    # spacing; candidates that break the spawn or hazard
    # clearances are rejected on the spot.
    positions = []
    spacing = 115.0
    tries = 0
    while len(positions) < count and tries < 4000:
        tries += 1
        if tries % 800 == 0:
            spacing = max(72.0, spacing - 12.0)
        candidate = [round(rng.uniform(x0, x1)),
                     round(rng.uniform(y0, y1))]
        if spawn is not None and _dist(candidate, spawn) \
                < _LAYOUT_MIN_SPAWN:
            continue
        if any(_dist_to_segment(
                candidate,
                [h["pos"][0] - h["range"], h["pos"][1]],
                [h["pos"][0] + h["range"], h["pos"][1]])
                < _LAYOUT_MIN_HAZARD for h in hazards):
            continue
        if all(_dist(candidate, p) >= spacing for p in positions):
            positions.append(candidate)
    return positions


def _violating_indices(positions, hazards, spawn) -> set:
    """Indices of the positions breaking a layout check."""
    bad: set = set()
    x0, y0, x1, y1 = _LAYOUT_BOX
    for i, pos in enumerate(positions):
        if len(pos) != 2 or not (x0 <= pos[0] <= x1
                                 and y0 <= pos[1] <= y1):
            bad.add(i)
            continue
        if _dist(pos, spawn) < _LAYOUT_MIN_SPAWN:
            bad.add(i)
        for hazard in hazards:
            hx, hy = hazard["pos"]
            reach = hazard["range"]
            if _dist_to_segment(pos, [hx - reach, hy],
                                [hx + reach, hy]) < _LAYOUT_MIN_HAZARD:
                bad.add(i)
    for i, pos in enumerate(positions):
        for j in range(i + 1, len(positions)):
            if _dist(pos, positions[j]) < _LAYOUT_MIN_PAIRWISE:
                bad.add(i)
                bad.add(j)
    return bad


def _repair_strays(positions, hazards, spawn, rng):
    """Nudge a pattern's few stray points back inside the checks.

    A pattern draw often fails on one or two points (a ring point
    on a patrol line, a zigzag point near the spawn). When at
    most two points violate, each is jittered — small steps first
    — until it clears every check against the rest of the set, so
    the pattern's shape survives. A draw failing on more points
    is not repaired: it falls back to the next pattern, as
    designed. Returns the repaired positions or None."""
    positions = [list(p) for p in positions]
    for _round in range(30):
        bad = _violating_indices(positions, hazards, spawn)
        if not bad:
            return positions
        if len(bad) > 2:
            return None
        moved = False
        for i in sorted(bad):
            placed = None
            for scale in (40, 70, 100, 130):
                for _try in range(12):
                    candidate = [
                        round(min(max(positions[i][0]
                                      + rng.uniform(-scale, scale),
                                      _LAYOUT_BOX[0]), _LAYOUT_BOX[2])),
                        round(min(max(positions[i][1]
                                      + rng.uniform(-scale, scale),
                                      _LAYOUT_BOX[1]), _LAYOUT_BOX[3]))]
                    trial = list(positions)
                    trial[i] = candidate
                    if i not in _violating_indices(
                            trial, hazards, spawn):
                        placed = candidate
                        break
                if placed is not None:
                    break
            if placed is None:
                return None
            positions[i] = placed
            moved = True
        if not moved:
            return None
    return positions if not _violating_indices(
        positions, hazards, spawn) else None


def arena_layout(count: int, seed: int, round_index: int = 0,
                 patterns=LAYOUT_PATTERNS):
    """(positions, record) for one 2D arena round.

    Patterns are tried in a rotation seeded by the project; the
    first whose positions pass arena_layout_issues wins, and the
    record names it plus any patterns that fell back."""
    import random

    hazards = HAZARD_SPOTS_2D[:min(2 + round_index, 5)]
    order = [patterns[(seed + round_index + k) % len(patterns)]
             for k in range(len(patterns))]
    fallbacks: list[str] = []
    for name in order:
        rng = random.Random(
            f"arena-layout:{seed}:{round_index}:{name}")
        for _attempt in range(40):
            positions = _layout_positions(name, count, rng,
                                          hazards, SPAWN_2D)
            if len(positions) != count:
                continue
            if arena_layout_issues(positions, hazards, SPAWN_2D):
                positions = _repair_strays(
                    positions, hazards, SPAWN_2D, rng)
                if positions is None or arena_layout_issues(
                        positions, hazards, SPAWN_2D):
                    continue
            return positions, {"pattern": name, "seed": seed,
                               "fallbacks": list(fallbacks)}
        fallbacks.append(name)
    # Every pattern exhausted its draws (should not happen in an
    # arena this size): one long scattered session, whose sampling
    # respects the clearances point by point, as the last resort.
    rng = random.Random(f"arena-layout:{seed}:{round_index}:last")
    for _attempt in range(200):
        positions = _layout_positions("scattered", count, rng,
                                      hazards, SPAWN_2D)
        if len(positions) == count and not arena_layout_issues(
                positions, hazards, SPAWN_2D):
            return positions, {"pattern": "scattered", "seed": seed,
                               "fallbacks": list(fallbacks)}
    raise ValueError(
        f"no arena layout passed its checks for {count} "
        f"collectibles in round {round_index + 1}")


def world_from_config(preset: str, config: dict,
                      composition: dict | None) -> dict:
    """The world data for a preset, extracted from the tuned
    config the deterministic generator (plus optional brain
    tuning) produced. Units keep the exact shapes the mechanics
    templates have always consumed — the behavioural freeze: the
    same numbers, now living in world.json as well as the config.
    """
    units: list[dict] = []
    if preset == "platformer":
        for index, level in enumerate(config.get("levels", [])):
            units.append({"index": index, "kind": "level", **level})
    elif preset == "crate_pusher":
        for index, puzzle in enumerate(config.get("puzzles", [])):
            units.append({"index": index, "kind": "puzzle", **puzzle})
    elif preset == "arena_3d":
        units = _arena3d_units(config)
    else:  # arena_collector
        seed = int(config.get("world_seed", 1234))
        targets = int(config.get("targets_per_round", 5))
        # A scope-pinned count is the maker's spec for EVERY
        # round (D6): the difficulty ramp comes from hazards,
        # never from quietly requiring more collectibles than
        # the scope states. Unpinned, the count still escalates.
        pinned = "scope_numbers" in config
        for index in range(int(config.get("rounds", 0))):
            count = targets if pinned else min(targets + index, 12)
            hazards = [dict(h) for h in
                       HAZARD_SPOTS_2D[:min(2 + index, 5)]]
            positions, layout = arena_layout(count, seed, index)
            units.append({
                "index": index, "kind": "round",
                "round": index + 1,
                "targets_per_round": config.get("targets_per_round", 5),
                "targets_pinned": pinned,
                "round_seconds": config.get("round_seconds", 60),
                "arena": list(ARENA_SIZE),
                "spawn": list(SPAWN_2D),
                "collectibles": positions,
                "hazards": hazards,
                "layout": layout,
            })
    return {
        "preset": preset,
        "composition": composition,
        "bespoke": False,
        "entity_kinds": list(ENTITY_KINDS.get(preset, [])),
        "units": units,
    }


def _inside_obstacle(point, obstacle, pad: float) -> bool:
    """Is an (x, z) point inside an obstacle block, grown by pad?"""
    ox, oz = obstacle.get("pos", [0, 0])
    sx, sz = obstacle.get("size", [0, 0])
    return (abs(point[0] - ox) <= sx / 2 + pad
            and abs(point[1] - oz) <= sz / 2 + pad)


def _arena3d_units(config: dict) -> list[dict]:
    """The 3D arena's rounds, laid out on the ground plane.

    Deterministic (seeded from the config the build prepared):
    per round, collectible positions on the XZ plane, hazard
    patrol paths as waypoint pairs, and obstacle blocks — all
    inside the ground, nothing on the spawn, no collectible
    inside an obstacle. The gate (_validate_round3d) checks the
    same invariants on the data, so a sabotaged world fails
    before any 3D scene is built from it.
    """
    import random

    rounds = int(config.get("rounds", 0))
    base_seed = int(config.get("world_seed", 1234))
    targets = int(config.get("targets_per_round", 5))
    # Scope-pinned counts hold on every round (D6), exactly as
    # in the 2D arena; unpinned counts escalate as before.
    pinned = "scope_numbers" in config
    seconds = config.get("round_seconds", 60)
    gx, gz = GROUND_3D
    units: list[dict] = []
    for index in range(rounds):
        rng = random.Random(base_seed + index * 101)
        obstacles: list[dict] = []
        for _ in range(min(3 + index, 6)):
            for _attempt in range(40):
                size = [round(rng.uniform(2.0, 4.0), 1),
                        round(rng.uniform(2.0, 4.0), 1)]
                pos = [round(rng.uniform(-gx / 2 + 3, gx / 2 - 3), 1),
                       round(rng.uniform(-gz / 2 + 3, gz / 2 - 3), 1)]
                if (pos[0] - SPAWN_3D[0]) ** 2 \
                        + (pos[1] - SPAWN_3D[1]) ** 2 < 25:
                    continue
                candidate = {"pos": pos, "size": size,
                             "height": round(rng.uniform(1.0, 2.4), 1)}
                if any(_inside_obstacle(pos, other, 2.2)
                       for other in obstacles):
                    continue
                obstacles.append(candidate)
                break
        count = targets if pinned else min(targets + index, 12)
        collectibles: list[list[float]] = []
        for _ in range(count):
            for _attempt in range(60):
                pos = [round(rng.uniform(-gx / 2 + 2, gx / 2 - 2), 1),
                       round(rng.uniform(-gz / 2 + 2, gz / 2 - 2), 1)]
                if (pos[0] - SPAWN_3D[0]) ** 2 \
                        + (pos[1] - SPAWN_3D[1]) ** 2 < 6.5:
                    continue
                if any(_inside_obstacle(pos, ob, 0.8)
                       for ob in obstacles):
                    continue
                if any((pos[0] - c[0]) ** 2 + (pos[1] - c[1]) ** 2 < 4.8
                       for c in collectibles):
                    continue
                collectibles.append(pos)
                break
        # The layout record the 2D path has always written (D5):
        # an honest name for this placer (seeded scatter on the
        # ground plane), the round's seed, and the placement
        # gate's outcome — every collectible placed under the
        # spawn/obstacle/spacing checks above, or the shortfall
        # stated in numbers.
        layout3d = {
            "pattern": "scatter3d",
            "seed": base_seed + index * 101,
            "gate": ("passed" if len(collectibles) == count
                     else f"placed {len(collectibles)} of {count}"),
        }
        hazards: list[dict] = []
        for _ in range(min(2 + index, 4)):
            for _attempt in range(40):
                z = round(rng.uniform(-gz / 2 + 2.5, gz / 2 - 2.5), 1)
                centre = round(rng.uniform(-6, 6), 1)
                span = round(rng.uniform(7, 13), 1)
                waypoints = [[round(centre - span / 2, 1), z],
                             [round(centre + span / 2, 1), z]]
                # A patrol line may not sweep across the spawn.
                if abs(z - SPAWN_3D[1]) < 2.0 and \
                        waypoints[0][0] <= SPAWN_3D[0] <= waypoints[1][0]:
                    continue
                hazards.append({"waypoints": waypoints})
                break
        units.append({
            "index": index, "kind": "round3d",
            "round": index + 1,
            "targets_per_round": targets,
            "targets_pinned": pinned,
            "round_seconds": seconds,
            "ground": list(GROUND_3D),
            "spawn": list(SPAWN_3D),
            "collectibles": collectibles,
            "hazards": hazards,
            "obstacles": obstacles,
            "layout": layout3d,
        })
    return units


# ---------------------------------------------------------------------------
# Plan-built worlds (0.11.0) — the units a scope's plan produces.
# The shapes deliberately match the legacy units the runtime has
# always consumed (rounds with generated layouts, round3d units,
# levels, puzzles); what changed is who decides: the plan's
# movement, objective, entity behaviours and numbers — never a
# preset name.

def _plan_entity(plan: dict, kind: str) -> dict | None:
    for entity in plan.get("entities", []):
        if entity.get("kind") == kind:
            return entity
    return None


def _plan_goal_2d(collectibles: list) -> list:
    """A goal position for a reach plan: a layout-box corner that
    clears the collectibles and the spawn."""
    for cand in ([988.0, 130.0], [150.0, 130.0], [988.0, 520.0],
                 [576.0, 120.0]):
        if all(_dist(cand, c) >= 110.0 for c in collectibles) \
                and _dist(cand, SPAWN_2D) >= 200.0:
            return list(cand)
    return [988.0, 130.0]


def _plan_rounds_2d(plan: dict) -> list[dict]:
    numbers = plan.get("numbers", {})
    units = int(numbers.get("units", 5))
    seed = int(numbers.get("world_seed", 0))
    objective = plan.get("objective")
    pinned = bool(numbers.get("targets_pinned"))
    base = int(numbers.get("targets_per_round", 5))
    hazard = _plan_entity(plan, "hazard")
    behaviour = str((hazard or {}).get("behaviour", "patrol"))
    has_goal = _plan_entity(plan, "goal") is not None
    rounds: list[dict] = []
    for index in range(units):
        count = base if pinned else min(base + index, 12)
        unit: dict = {
            "index": index, "kind": "round", "round": index + 1,
            "targets_per_round": base,
            "targets_pinned": pinned,
            "round_seconds": numbers.get("round_seconds", 60),
            "arena": list(ARENA_SIZE),
            "spawn": list(SPAWN_2D),
        }
        if hazard is not None:
            unit["hazards"] = [
                {**spot, "behaviour": behaviour}
                for spot in HAZARD_SPOTS_2D[:min(2 + index, 5)]]
        if objective in ("collect", "reach"):
            positions, layout = arena_layout(count, seed, index)
            unit["collectibles"] = positions
            unit["layout"] = layout
        if has_goal:
            unit["goal"] = _plan_goal_2d(unit.get("collectibles") or [])
        rounds.append(unit)
    return rounds


def _plan_rounds_3d(plan: dict) -> list[dict]:
    numbers = plan.get("numbers", {})
    objective = plan.get("objective")
    hazard = _plan_entity(plan, "hazard")
    behaviour = str((hazard or {}).get("behaviour", "patrol"))
    has_goal = _plan_entity(plan, "goal") is not None
    shim = {
        "rounds": int(numbers.get("units", 5)),
        "world_seed": int(numbers.get("world_seed", 0)),
        "targets_per_round": int(numbers.get("targets_per_round", 5)),
        "round_seconds": numbers.get("round_seconds", 60),
    }
    if numbers.get("targets_pinned"):
        shim["scope_numbers"] = {"targets_per_round": {}}
    units = _arena3d_units(shim)
    import random as _random
    for unit in units:
        if hazard is None:
            unit["hazards"] = []
        else:
            for entry in unit.get("hazards", []):
                entry["behaviour"] = behaviour
        if objective == "survive":
            unit["collectibles"] = []
        if has_goal:
            rng = _random.Random(
                int(numbers.get("world_seed", 0))
                + int(unit.get("index", 0)) * 733)
            gx, gz = GROUND_3D
            goal = None
            taken = [list(c) for c in unit.get("collectibles") or []]
            taken += [list(h["waypoints"][0])
                      for h in unit.get("hazards", [])
                      if h.get("waypoints")]
            for _try in range(200):
                cand = [round(rng.uniform(-gx / 2 + 2, gx / 2 - 2), 1),
                        round(rng.uniform(-gz / 2 + 2, gz / 2 - 2), 1)]
                if _dist(cand, SPAWN_3D) < 8.0:
                    continue
                if any(_dist(cand, other) < 3.0 for other in taken):
                    continue
                if any(_inside_obstacle(cand, ob, 0.8)
                       for ob in unit.get("obstacles", [])):
                    continue
                goal = cand
                break
            unit["goal"] = goal or [16.0, 8.0]
    return units


def _plan_levels(plan: dict, attempt: int = 0) -> list[dict]:
    import random
    numbers = plan.get("numbers", {})
    units = int(numbers.get("units", 5))
    seed = int(numbers.get("world_seed", 0)) + attempt * 7919
    rng = random.Random(seed)
    levels = []
    for index in range(units):
        level = generate_module._platformer_level(rng, index)
        levels.append({"index": index, "kind": "level", **level})
    return levels


def _plan_puzzles(plan: dict, config: dict) -> list[dict]:
    numbers = plan.get("numbers", {})
    units = int(numbers.get("units", 5))
    multiplier = float(config.get(
        "move_budget_multiplier",
        numbers.get("move_budget_multiplier", 2.5)))
    puzzles = generate_module.crate_puzzles(
        units, budget_multiplier=multiplier)
    return [{"index": index, "kind": "puzzle", **puzzle}
            for index, puzzle in enumerate(puzzles)]


def plan_entity_issues(plan: dict) -> list[dict]:
    """The plan's declared entities must be able to serve its
    objective and fail rules — a collect plan with no collectible
    entity (or a deliver plan with no pushable) is a scope/plan
    disagreement the world gate refuses, rather than a game that
    quietly lacks its objective."""
    kinds = {e.get("kind") for e in plan.get("entities", [])}
    required: list[tuple[str, str]] = []
    objective = plan.get("objective")
    if objective == "collect":
        required.append(("collectible", "its objective is collect"))
    elif objective == "reach":
        required.append(("goal", "its objective is reach"))
    elif objective == "deliver":
        required.append(("pushable", "its objective is deliver"))
        required.append(("zone", "its objective is deliver"))
    elif objective == "survive":
        required.append(("hazard", "its objective is survive"))
    if "hazard_contacts" in plan.get("fail", []):
        required.append(("hazard", "its fail rule is hazard_contacts"))
    return [
        {"kind": "world_gate", "owner": "world",
         "detail": f"the plan declares no {kind} entity, but "
                   f"{why} — the scope and the plan disagree"}
        for kind, why in required if kind not in kinds]


def world_from_plan(plan: dict, config: dict | None = None,
                    attempt: int = 0) -> dict:
    """The world data for a scope's plan (0.11.0)."""
    config = config or {}
    if plan.get("bespoke_required"):
        return {
            "plan": None,
            "bespoke": True,
            "entity_kinds": ["bespoke"],
            "units": [{"index": 0, "kind": "bespoke"}],
        }
    movement = plan_module.plan_movement(plan)
    if movement == "top_down_2d":
        units = _plan_rounds_2d(plan)
    elif movement == "top_down_3d":
        units = _plan_rounds_3d(plan)
    elif movement == "side_view":
        units = _plan_levels(plan, attempt)
    elif movement == "grid_step":
        units = _plan_puzzles(plan, config)
    else:
        units = []
    return {
        "plan": plan,
        "entity_kinds": plan_module.entity_kinds(plan),
        "units": units,
    }


def bespoke_world(ctx: dict) -> dict:
    """A bespoke game's world is declared by its mechanics
    module, not by the factory: one open unit, no factory
    geometry. The module contract requires the module to expose
    its objective positions through bridge_state() instead."""
    return {
        "preset": None,
        "composition": None,
        "bespoke": True,
        "entity_kinds": ["bespoke"],
        "units": [{"index": 0, "kind": "bespoke"}],
    }


def world_manifest(world_data: dict) -> dict:
    """The manifest section recorded in the generation report and
    read by the mechanics and assets workers: what the world
    contains, in counts."""
    units = world_data.get("units", [])
    manifest = {
        "path": WORLD_REL,
        "bespoke": bool(world_data.get("bespoke")),
        "units": len(units),
        "unit_kinds": sorted({str(u.get("kind", "")) for u in units}),
        "entity_kinds": list(world_data.get("entity_kinds", [])),
    }
    if world_data.get("preset"):
        # Legacy worlds only: plan worlds carry movement and
        # objective below instead of a preset name.
        manifest["preset"] = world_data.get("preset")
    plan = world_data.get("plan")
    if isinstance(plan, dict):
        manifest["movement"] = plan.get("movement")
        manifest["objective"] = plan.get("objective")
    return manifest


def _issue(detail: str) -> dict:
    return {"kind": "world_gate", "detail": detail, "owner": "world"}


def validate_world(world_data: dict) -> list[dict]:
    """The world gate. Returns issue dicts (empty = the world is
    structurally sound and downstream may run)."""
    issues: list[dict] = []
    if not isinstance(world_data, dict):
        return [_issue("world data is not an object")]
    units = world_data.get("units")
    if not units:
        return [_issue("the world has no content units")]
    preset = world_data.get("preset")
    if world_data.get("bespoke"):
        return issues
    plan = world_data.get("plan")
    if isinstance(plan, dict):
        return _validate_plan_world(world_data, plan)
    for unit in units:
        index = unit.get("index", "?")
        if preset == "platformer":
            issues.extend(_validate_level(unit, index))
        elif preset == "crate_pusher":
            issues.extend(_validate_puzzle(unit, index))
        elif preset == "arena_collector":
            issues.extend(_validate_round(unit, index))
        elif preset == "arena_3d":
            issues.extend(_validate_round3d(unit, index))
        else:
            issues.append(_issue(f"unit {index}: unknown preset {preset!r}"))
    return issues


def _validate_round3d_survive(unit: dict, index) -> list[dict]:
    """The 3D gate for a survive round: no collectibles to count,
    but the ground, spawn, obstacles and patrol waypoints must
    all sit on the plane (the collect-round gate additionally
    requires its collectibles, which a survive round has none
    of by design)."""
    issues = []
    ground = unit.get("ground") or []
    if len(ground) != 2 or ground[0] <= 0 or ground[1] <= 0:
        return [_issue(f"round {index}: ground plane missing")]
    gx, gz = ground

    def inside(point, margin: float) -> bool:
        return (abs(point[0]) <= gx / 2 - margin
                and abs(point[1]) <= gz / 2 - margin)

    spawn = unit.get("spawn") or []
    if len(spawn) != 2 or not inside(spawn, 0.5):
        issues.append(_issue(f"round {index}: spawn outside the ground"))
    for obstacle in unit.get("obstacles") or []:
        pos = obstacle.get("pos") or []
        if len(pos) != 2 or not inside(pos, 0.0):
            issues.append(_issue(
                f"round {index}: an obstacle sits outside the ground"))
            break
    for hazard in unit.get("hazards") or []:
        for waypoint in hazard.get("waypoints") or []:
            if len(waypoint) != 2 or not inside(waypoint, 0.5):
                issues.append(_issue(
                    f"round {index}: a hazard waypoint leaves the ground"))
                return issues
    return issues


def _validate_plan_world(world_data: dict, plan: dict) -> list[dict]:
    """The gate for a plan-built world: the plan's units and
    entity kinds, then the per-movement structural checks (the
    same validators the legacy presets answer to)."""
    issues: list[dict] = []
    issues.extend(plan_entity_issues(plan))
    units = world_data.get("units") or []
    have = set(world_data.get("entity_kinds", []))
    for kind in plan_module.entity_kinds(plan):
        if kind not in have:
            issues.append(_issue(
                f"the world is missing the plan's {kind} entity kind"))
    numbers = plan.get("numbers", {})
    stated = numbers.get("units")
    if isinstance(stated, int) and len(units) != stated:
        issues.append(_issue(
            f"the world has {len(units)} units; the plan states "
            f"{stated}"))
    movement = plan_module.plan_movement(plan)
    objective = plan_module.plan_objective(plan)
    for unit in units:
        index = unit.get("index", "?")
        if movement == "top_down_2d":
            issues.extend(_validate_round(unit, index))
        elif movement == "top_down_3d":
            if objective == "survive":
                issues.extend(_validate_round3d_survive(unit, index))
            else:
                issues.extend(_validate_round3d(unit, index))
        elif movement == "side_view":
            issues.extend(_validate_level(unit, index))
        elif movement == "grid_step":
            issues.extend(_validate_puzzle(unit, index))
        else:
            issues.append(_issue(
                f"unit {index}: unknown movement {movement!r}"))
    return issues


def _validate_level(unit: dict, index) -> list[dict]:
    issues = []
    platforms = unit.get("platforms") or []
    if not platforms:
        issues.append(_issue(f"level {index}: no platforms"))
        return issues
    width = unit.get("width") or 0
    if width <= 0:
        issues.append(_issue(f"level {index}: no width"))
    spawn = unit.get("spawn") or []
    exit_pos = unit.get("exit") or []
    if len(spawn) != 2 or not (0 <= spawn[0] <= width):
        issues.append(_issue(f"level {index}: spawn missing or outside the level"))
    if len(exit_pos) != 2 or not (0 <= exit_pos[0] <= width):
        issues.append(_issue(f"level {index}: exit missing or outside the level"))
    if not unit.get("collectibles"):
        issues.append(_issue(f"level {index}: no collectibles"))
    for spike in unit.get("spikes") or []:
        if len(spike) != 3 or spike[0] < 0 or spike[0] + spike[2] > width:
            issues.append(_issue(f"level {index}: a spike pit sits outside the level"))
            break
    # Reachability invariant, checked on the data the same way the
    # generator constructs it: consecutive staircase steps must
    # overlap in x or start within 36px of the previous step's
    # end, and rise/drop by at most 64px — otherwise a missed jump
    # can strand the player under a step.
    ground_y = 560
    steps = sorted(
        (p for p in platforms if len(p) == 3 and p[1] != ground_y),
        key=lambda p: p[0])
    for prev, nxt in zip(steps, steps[1:]):
        gap = nxt[0] - (prev[0] + prev[2])
        if gap > 36:
            issues.append(_issue(
                f"level {index}: staircase gap of {gap}px exceeds the "
                "36px reachability invariant"))
            break
        if abs(nxt[1] - prev[1]) > 64:
            issues.append(_issue(
                f"level {index}: staircase step changes height by more "
                "than the 64px reachability invariant"))
            break
    return issues


def _validate_puzzle(unit: dict, index) -> list[dict]:
    grid = unit.get("grid") or []
    if not grid:
        return [_issue(f"puzzle {index}: no grid")]
    text = "\n".join(str(row) for row in grid)
    parsed = generate_module.parse_grid(text)
    if parsed["player"] is None:
        return [_issue(f"puzzle {index}: no player start in the grid")]
    if len(parsed["crates"]) != len(parsed["pads"]) or not parsed["crates"]:
        return [_issue(
            f"puzzle {index}: {len(parsed['crates'])} crates for "
            f"{len(parsed['pads'])} pads")]
    if generate_module.solve_puzzle(text) is None:
        return [_issue(f"puzzle {index}: the solver cannot solve it")]
    return []


def _validate_round3d(unit: dict, index) -> list[dict]:
    """The 3D round gate: everything inside the ground plane,
    and every collectible reachable on the XZ plane — which, on
    an open plane with block obstacles, means no collectible
    sits inside an obstacle (grown by the pickup radius)."""
    issues = []
    ground = unit.get("ground") or []
    if len(ground) != 2 or ground[0] <= 0 or ground[1] <= 0:
        return [_issue(f"round {index}: ground plane missing")]
    gx, gz = ground

    def inside(point, margin: float) -> bool:
        return (abs(point[0]) <= gx / 2 - margin
                and abs(point[1]) <= gz / 2 - margin)

    spawn = unit.get("spawn") or []
    if len(spawn) != 2 or not inside(spawn, 0.5):
        issues.append(_issue(f"round {index}: spawn outside the ground"))
    obstacles = unit.get("obstacles") or []
    for obstacle in obstacles:
        pos = obstacle.get("pos") or []
        if len(pos) != 2 or not inside(pos, 0.0):
            issues.append(_issue(
                f"round {index}: an obstacle sits outside the ground"))
            break
    collectibles = unit.get("collectibles") or []
    if not collectibles:
        issues.append(_issue(f"round {index}: no collectibles"))
    # The count the round actually requires (D6): a scope-pinned
    # round must hold exactly the pinned count; an unpinned round
    # the escalated one. Worlds written before the pin was
    # recorded carry no flag and follow the escalation, as their
    # generator built them.
    targets3d = unit.get("targets_per_round")
    if collectibles and isinstance(targets3d, (int, float)):
        round_index = unit.get("index", index)
        if not isinstance(round_index, int):
            round_index = 0
        expected3d = (int(targets3d) if unit.get("targets_pinned")
                      else min(int(targets3d) + round_index, 12))
        if len(collectibles) != expected3d:
            issues.append(_issue(
                f"round {index}: {len(collectibles)} collectibles "
                f"for a round that needs {expected3d}"))
    for point in collectibles:
        if len(point) != 2 or not inside(point, 0.8):
            issues.append(_issue(
                f"round {index}: a collectible sits outside the ground"))
            break
        if any(_inside_obstacle(point, ob, 0.8) for ob in obstacles):
            issues.append(_issue(
                f"round {index}: a collectible is inside an obstacle "
                "and cannot be reached"))
            break
    for hazard in unit.get("hazards") or []:
        for waypoint in hazard.get("waypoints") or []:
            if len(waypoint) != 2 or not inside(waypoint, 0.5):
                issues.append(_issue(
                    f"round {index}: a hazard waypoint leaves the ground"))
                return issues
    return issues


def _validate_round(unit: dict, index) -> list[dict]:
    issues = []
    targets = unit.get("targets_per_round")
    if not isinstance(targets, (int, float)) or not (1 <= targets <= 12):
        issues.append(_issue(
            f"round {index}: targets_per_round {targets!r} outside 1–12"))
    seconds = unit.get("round_seconds")
    if not isinstance(seconds, (int, float)) or not (15 <= seconds <= 300):
        issues.append(_issue(
            f"round {index}: round_seconds {seconds!r} outside 15–300"))
    arena = unit.get("arena") or []
    if len(arena) != 2 or arena[0] <= 0 or arena[1] <= 0:
        issues.append(_issue(f"round {index}: arena bounds missing"))
    # Layout checks (0.10.0): when a round carries collectible
    # positions, they must be the round's full set, inside the
    # arena, spaced from each other, the spawn and the hazard
    # patrol lines. Rounds written before layouts existed carry
    # no positions and skip this — the template's fallback spots
    # serve them.
    collectibles = unit.get("collectibles")
    if collectibles is not None:
        round_index = unit.get("index", index)
        if not isinstance(round_index, int):
            round_index = 0
        # A scope-pinned round needs exactly the pinned count
        # (D6); an unpinned round expects the escalation.
        expected = None
        if isinstance(targets, (int, float)):
            expected = (int(targets) if unit.get("targets_pinned")
                        else min(int(targets) + round_index, 12))
        if expected is not None and len(collectibles) != expected:
            issues.append(_issue(
                f"round {index}: {len(collectibles)} collectible "
                f"positions for a round that needs {expected}"))
        hazards = unit.get("hazards") or [
            dict(h) for h in
            HAZARD_SPOTS_2D[:min(2 + round_index, 5)]]
        spawn = unit.get("spawn") or SPAWN_2D
        for detail in arena_layout_issues(collectibles, hazards, spawn):
            issues.append(_issue(f"round {index}: {detail}"))
    return issues
