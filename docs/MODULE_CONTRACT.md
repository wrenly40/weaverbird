# The bespoke mechanics module contract

When a game's scope describes a core loop that Weaverbird's
composition primitives (movement × objective × fail × structure ×
interactions) cannot express, the factory does not force it into
an unrelated preset. The mechanics worker marks the build
**bespoke** and — with the user's own brain configured — a model
writes ONE GDScript module implementing this contract. Without a
brain, the orchestrator stops at the plan and says so plainly.

The module lives at `game/scripts/bespoke_mechanics.gd` in the
generated game. A factory-written host (`game/scripts/game.gd`,
from `game_templates/bespoke/`) owns the shell around it: menus,
save, settings, pause, the HUD hint, and the win/lose scene
changes. The module owns the game itself.

## The interface

The module `extends Node2D` and defines:

- `setup(game, config)` — called once when the game scene starts.
  `game` is the host node (signal the end of the run through it,
  below); `config` is the game's `game_config.json` as a
  Dictionary (palette hexes under `accent_hex` / `target_hex` /
  `hazard_hex`, the objective line under `objective_hint`).
- `bridge_state() -> Dictionary` — the facts the scripted
  testers and the model playtester may see: honestly exposed,
  the same facts a player has. It MUST include:
  - `player_pos`: `[x, y]` — where the player is;
  - `objective_pos`: `[x, y]` — where the current objective is
    (the scripted competent player steers toward it with the
    standard input actions);
  - `hint`: the one-line objective shown on screen;
  - plus the counts that matter for this game (collected, lives,
    seconds left, moves used...).
- `bridge_actions() -> Array` — the actions the model playtester
  may choose from, a subset of: `start`, `wait`, `move_left`,
  `move_right`, `move_up`, `move_down`, `jump`, `interact`,
  `pause`.

## Signalling the end of the run

Win and lose are signalled ONLY through the host:

- `game.signal_win(collected, total, message)` — the host records
  the result in RunInfo and changes to the win scene.
- `game.signal_lose(reason)` — same, to the game-over scene.

The module must have a real fail condition reachable without
player input being *correct* (a timer, lives, a budget): the
scripted lose player starts the game and does nothing, and the
run must still end.

## Rules

- The player is moved with the standard input actions
  (`move_left`, `move_right`, `move_up`, `move_down`, `interact`
  — the bridge's `jump` maps to `interact`), exactly what a
  keyboard produces. No teleporting the testers, no reading
  their files: the game is played, not negotiated with.
- Everything the module needs from the factory arrives through
  `setup`'s config. It writes no files and reaches no network.
- One module, one file. Menus, save and settings are the
  shell's and already work; the module never reimplements them.

## The stub

`src/weaverbird/templates/bespoke_mechanics.gd.stub` is the
contract as code — the starting text a brain is given, and the
shape the mechanics worker checks for (`setup(` and
`bridge_state(` must be present) before the module is assembled
and held to the same assembly validation and full test stage as
any composed game.

Honest status: the mocked-brain path (a known-good fixture
module) is tested end to end. The live-model path is
untested-by-design — no model keys exist in the factory's own
test environment — and a module it produces still has to pass
assembly validation and the full test stage before anything
ships.
