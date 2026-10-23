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
"""

from __future__ import annotations

import json
from pathlib import Path

from . import generate as generate_module
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
        for index in range(int(config.get("rounds", 0))):
            units.append({
                "index": index, "kind": "round",
                "round": index + 1,
                "targets_per_round": config.get("targets_per_round", 5),
                "round_seconds": config.get("round_seconds", 60),
                "arena": list(ARENA_SIZE),
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
        count = min(targets + index, 12)
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
            "round_seconds": seconds,
            "ground": list(GROUND_3D),
            "spawn": list(SPAWN_3D),
            "collectibles": collectibles,
            "hazards": hazards,
            "obstacles": obstacles,
        })
    return units


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
    return {
        "path": WORLD_REL,
        "preset": world_data.get("preset"),
        "bespoke": bool(world_data.get("bespoke")),
        "units": len(units),
        "unit_kinds": sorted({str(u.get("kind", "")) for u in units}),
        "entity_kinds": list(world_data.get("entity_kinds", [])),
    }


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
    return issues
