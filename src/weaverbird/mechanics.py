"""The mechanics worker's library — player + game logic.

Split out of the old monolithic build stage in 0.7.0. The
mechanics worker owns *how the game plays*: the player controller
and game-logic scripts that interpret ``game/world/world.json``
at runtime (the generated games load the world data — rounds,
levels, puzzles — rather than having layouts baked into code),
plus the scripted test players shipped in ``game/tests/``.

Since 0.11.0 there is exactly one runtime to write: the generic
interpreter in game_templates/runtime/, which executes any game
PLAN (plan.py) — four movements, four objectives, three fail
rules, entity behaviours carried by the world data. The legacy
per-preset template sets below serve only projects built before
0.11.0 when their mechanics are re-written.

Its gate is a rule-coverage check, run before assembly: every
entity kind present in the world data must have a handler in
this worker's rule set for the runtime, or the run comes
back with issues owned by "mechanics".

Bespoke path: when the scope's core mechanic cannot be expressed
in the primitive vocabulary (see plan.bespoke_required), the
mechanics are bespoke: one GDScript module written by the user's
own brain (frontier tier first, then mid) against the documented
contract in docs/MODULE_CONTRACT.md, stored at
``game/scripts/bespoke_mechanics.gd`` and hosted by the
factory-written shell in game_templates/bespoke/. Without a
usable brain the worker refuses plainly — it never substitutes
a different game.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from . import brain as brain_module
from . import generate as generate_module
from . import skeleton as skeleton_module

GAME_DIR_NAME = skeleton_module.GAME_DIR_NAME
BESPOKE_MODULE_REL = f"{GAME_DIR_NAME}/scripts/bespoke_mechanics.gd"

# The rule set: entity kind -> the handler that interprets it,
# per preset. The gate checks the world's entity kinds against
# this table.
RULES: dict[str, dict[str, str]] = {
    "arena_collector": {
        "player": "scripts/player.gd — top-down movement from the "
                  "standard input actions, speeds from game_config.json",
        "collectible": "scripts/marker.gd targets; collecting the "
                       "last target of a round clears it",
        "hazard": "scripts/marker.gd hazards on patrol paths; "
                  "contact ends the run",
    },
    "platformer": {
        "player": "scripts/player.gd — run/jump physics from "
                  "game_config.json (speed, jump strength, gravity)",
        "platform": "scripts/game.gd level loader — builds static "
                    "bodies from the world data's platform list",
        "collectible": "scripts/star.gd — collecting every star in "
                       "a level arms the exit",
        "hazard": "scripts/game.gd spike zones from the world data; "
                  "contact costs a life",
        "exit": "scripts/door_draw.gd exit door from the world "
                "data; reaching it with every star advances",
    },
    "crate_pusher": {
        "player": "scripts/game.gd — grid movement, one cell per "
                  "press, undo on the interact action",
        "wall": "scripts/game.gd — walls from the puzzle grids "
                "block movement and pushes",
        "crate": "scripts/game.gd push rules — a crate moves when "
                 "the cell beyond is free; cornered crates are "
                 "reported as a deadlock, never a soft-lock",
        "pad": "scripts/game.gd — a puzzle is solved when every "
               "pad holds a crate, inside the move budget",
    },
    "arena_3d": {
        "player": "scripts/player.gd — a CharacterBody3D steered "
                  "by the standard input actions on the ground "
                  "plane, speed from game_config.json; the camera "
                  "rig in scripts/game.gd follows it",
        "collectible": "scripts/game.gd — spinning Area3D orbs at "
                       "the world data's positions; body contact "
                       "collects, and the last orb of a round "
                       "clears it",
        "hazard": "scripts/game.gd — Area3D hazards patrolling "
                  "the world data's waypoint paths; contact ends "
                  "the run",
        "obstacle": "scripts/game.gd — StaticBody3D boxes built "
                    "from the world data's blocks (position, "
                    "size, height), materials from the palette",
    },
    "bespoke": {
        "bespoke": "scripts/bespoke_mechanics.gd — the "
                   "brain-written module behind the module "
                   "contract, hosted by scripts/game.gd",
    },
}

# The generic runtime's rule set (0.11.0): entity kind -> the
# handler in game_templates/runtime/ that interprets it. The
# coverage gate checks plan-built worlds against this table.
RUNTIME_RULES: dict[str, str] = {
    "player": "scripts/player.gd — the runtime's controller for "
              "the plan's movement (free top-down, gravity and "
              "jump, or grid-stepped by scripts/game.gd)",
    "collectible": "scripts/marker.gd (top-down) / scripts/star.gd "
                   "(side view) pickups in group 'targets'; their "
                   "behaviour comes from the world data",
    "hazard": "scripts/marker.gd hazards in group 'hazards' (2D) "
              "or the hazard areas of scripts/game3d.gd (3D), "
              "moving per the behaviour the plan assigned",
    "goal": "scripts/game.gd goal area in group 'goal' — touching "
            "it completes a reach plan, or a collect plan once "
            "everything is collected",
    "pushable": "scripts/game.gd grid rules — a pushable moves "
                "when the cell beyond is free",
    "zone": "scripts/game.gd — a deliver unit is solved when "
            "every zone holds a pushable, inside the move budget",
    "wall": "scripts/game.gd — walls from the puzzle grids block "
            "movement and pushes",
    "platform": "scripts/game.gd level loader — static bodies "
                "built from the world data's platform list",
    "obstacle": "scripts/game3d.gd — static blocks built from the "
                "world data, materials from the palette",
    "ambient": "scripts/marker.gd ambient markers — decoration "
               "that wanders; neither collectible nor hazard",
}


def _runtime_files(movement: str) -> dict:
    """The generic runtime's files: {path relative to game/,
    template parts}. One set for every plan; the 3D movement
    swaps in its scene and controllers."""
    files = {
        "scripts/asset_loader.gd": ("shared", "asset_loader.gd"),
        "tests/savecheck.gd": ("shared", "savecheck.gd"),
        "tests/model_bridge.gd": ("shared", "model_bridge.gd"),
        "tests/competent_win.gd": ("runtime", "competent_win.gd"),
        "tests/competent_lose.gd": ("runtime", "competent_lose.gd"),
        "tests/breaker.gd": ("runtime", "breaker.gd"),
        "tests/first_time.gd": ("runtime", "first_time.gd"),
        "smoke/smoke.gd": ("runtime", "competent_win.gd"),
    }
    if movement == "top_down_3d":
        files["scripts/game.gd"] = ("runtime", "game3d.gd")
        files["scripts/player.gd"] = ("runtime", "player3d.gd")
        files["scenes/game.tscn"] = ("runtime", "game3d.tscn")
    else:
        files["scripts/game.gd"] = ("runtime", "game.gd")
        files["scripts/player.gd"] = ("runtime", "player.gd")
        files["scripts/marker.gd"] = ("runtime", "marker.gd")
        files["scripts/star.gd"] = ("runtime", "star.gd")
        files["scripts/door_draw.gd"] = ("runtime", "door_draw.gd")
    return files


class MechanicsError(Exception):
    """Raised when the mechanics worker cannot produce its scripts."""


def template_files(ctx: dict) -> dict:
    """Generated game files for a build context:
    {path relative to game/, template parts}."""
    if ctx.get("bespoke"):
        files = {
            "scripts/asset_loader.gd": ("shared", "asset_loader.gd"),
            "tests/savecheck.gd": ("shared", "savecheck.gd"),
            "tests/model_bridge.gd": ("shared", "model_bridge.gd"),
            "tests/competent_win.gd": ("bespoke", "competent_win.gd"),
            "tests/competent_lose.gd": ("bespoke", "competent_lose.gd"),
            "tests/breaker.gd": ("bespoke", "breaker.gd"),
            "tests/first_time.gd": ("bespoke", "first_time.gd"),
            "smoke/smoke.gd": ("bespoke", "competent_win.gd"),
            "scripts/game.gd": ("bespoke", "game.gd"),
        }
        return files
    plan = ctx.get("plan")
    if isinstance(plan, dict):
        return _runtime_files(str(plan.get("movement") or ""))
    return generate_module._archetype_files(ctx["preset"])


def write_mechanics(project_dir, ctx: dict) -> list[str]:
    """Write the mechanics scripts for a build context. Returns
    the written paths (relative to the project). For a bespoke
    build the brain-written module must already exist — writing
    it is write_bespoke_module's job, so a module is never
    silently overwritten by a rebuild."""
    project = Path(project_dir)
    game_dir = project / GAME_DIR_NAME
    written: list[str] = []
    for rel, parts in sorted(template_files(ctx).items()):
        dest = game_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(generate_module._template_text(*parts),
                        encoding="utf-8")
        written.append(f"{GAME_DIR_NAME}/{rel}")
    if ctx.get("bespoke"):
        module = project / BESPOKE_MODULE_REL
        if not module.exists():
            raise MechanicsError(
                "This game needs bespoke mechanics, but no module "
                f"exists at {BESPOKE_MODULE_REL} yet — with a brain "
                "configured, the mechanics worker writes it from "
                "the scope (see docs/MODULE_CONTRACT.md); without "
                "one, the build cannot proceed.")
        written.append(BESPOKE_MODULE_REL)
    written.sort()
    return written


def check_coverage(world_data: dict, ctx: dict) -> list[dict]:
    """The mechanics gate: every entity kind in the world data
    must have a handler in the rule set. Issues are owned by
    "mechanics"."""
    plan = world_data.get("plan")
    if not isinstance(plan, dict):
        plan = ctx.get("plan") if isinstance(ctx.get("plan"), dict) \
            else None
    if plan is not None and not ctx.get("bespoke"):
        rules = RUNTIME_RULES
        whose = "the generic runtime"
    else:
        preset = "bespoke" if ctx.get("bespoke") else ctx.get("preset")
        rules = RULES.get(preset or "", {})
        whose = f"the {preset} mechanics"
    issues = []
    for kind in world_data.get("entity_kinds", []):
        if kind not in rules:
            issues.append({
                "kind": "unhandled_entity",
                "detail": f"the world contains entity kind {kind!r}, "
                          f"which {whose} have no handler for",
                "owner": "mechanics",
            })
    return issues


# ---------------------------------------------------------------------------
# The bespoke path: one brain-written module behind the contract

_BESPOKE_SYSTEM = (
    "You write ONE GDScript module for a Godot 4 game, implementing "
    "the module contract exactly: the file extends Node2D, defines "
    "setup(game, config), bridge_state() -> Dictionary (including "
    "player_pos, objective_pos and hint), and bridge_actions() -> "
    "Array. Win and lose are signalled ONLY by calling "
    "game.signal_win(collected, total, message) or "
    "game.signal_lose(reason). The player is moved with the "
    "standard input actions (move_left, move_right, move_up, "
    "move_down, interact). Answer with the GDScript code only — "
    "no prose, no markdown fences."
)


def bespoke_prompt(values: dict) -> str:
    brief = "; ".join(
        f"{name} = {values.get(name, '')}" for name in (
            "Title (working)", "One-line pitch", "Genre / archetype",
            "Core verb (what the player does most)", "Win condition",
            "Lose / fail condition",
            "Difficulty target (who it is for, how hard)"))
    return (f"Game scope: {brief}\nThe contract stub to implement:\n"
            + _stub_text() + "\nWrite the module now.")


def _stub_text() -> str:
    """The contract stub shipped in the package templates."""
    from . import template_text
    try:
        return template_text("bespoke_mechanics.gd.stub")
    except Exception:
        return ""


def _strip_fences(text: str) -> str:
    text = text.strip()
    match = re.search(r"```(?:gdscript|gd)?\n(.*?)```", text, re.DOTALL)
    if match:
        return match.group(1).strip() + "\n"
    return text + ("\n" if not text.endswith("\n") else "")


def write_bespoke_module(project_dir, settings_path=None, brain_fn=None,
                         reporter=None) -> dict:
    """Write game/scripts/bespoke_mechanics.gd from the scope,
    using the user's brain. ``brain_fn`` is the test seam: a
    callable taking the prompt and returning the module source.
    Without a brain (and no brain_fn) this raises MechanicsError
    with the plain explanation — the orchestrator checks first
    and stops at the plan instead.

    The live-model path is untested by design (no keys exist in
    the factory's own test environment); the mocked-brain path is
    covered end to end by the test suite.
    """
    project = Path(project_dir)
    scope_file = project / "SCOPE.md"
    from . import scope as scope_module
    parsed = scope_module.load_scope(scope_file)
    values = {f.name: f.value for f in parsed.fields}
    prompt = bespoke_prompt(values)
    if brain_fn is not None:
        source = brain_fn(prompt)
        note = "module written by the injected brain function (test seam)"
    else:
        _, data = brain_module.resolve_settings(settings_path, project)
        tier = None
        for candidate in ("frontier", "mid"):
            status = brain_module.tier_status(data, candidate) if data else None
            if status and status["usable"]:
                tier = candidate
                break
        if tier is None:
            raise MechanicsError(
                "Bespoke mechanics need a configured brain (frontier "
                "or mid tier), and none is usable — see "
                "'weaverbird brain status'. The factory will not "
                "substitute a different game for this scope.")
        try:
            result = brain_module.complete(
                project, tier,
                [{"role": "system", "content": _BESPOKE_SYSTEM},
                 {"role": "user", "content": prompt}],
                stage="build", settings_path=settings_path,
                max_tokens=2400)
        except brain_module.BrainBudgetError as exc:
            raise MechanicsError(f"brain refused by budget ({exc})") from exc
        if not result.ok:
            raise MechanicsError(f"brain call failed ({result.error})")
        source = result.text
        note = f"module written by the {tier} tier ({result.provider}/{result.model})"
    source = _strip_fences(source)
    if "func setup(" not in source or "func bridge_state(" not in source:
        raise MechanicsError(
            "The brain's module does not implement the contract "
            "(setup / bridge_state missing) — refusing to ship it.")
    dest = project / BESPOKE_MODULE_REL
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(source, encoding="utf-8")
    if reporter is not None:
        reporter(f"Bespoke mechanics module written ({note})")
    return {"path": BESPOKE_MODULE_REL, "note": note,
            "bytes": len(source.encode("utf-8"))}
