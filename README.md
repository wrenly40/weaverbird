# Weaverbird

Weaverbird is an open-source game factory that sits on top of Godot.
You start with a prompt. The factory turns it into a scope, asks
questions until the scope is complete, then builds the game, tests it,
has AI players play it, publishes it through store connectors with
your permission, and keeps polishing it from player feedback.

It is a full end-to-end platform, being built in stages. The v0.1
release was the infrastructure — the backbone the rest of the platform
plugs into. v0.2 added the skeleton builder, which turns an approved
scope into a playable grey-box Godot game and proves it runs by
running Godot against it. v0.3 added the brain, the app, and the
finish check: a bring-your-own-key model layer that runs the scope
interview, a local web app over the same core, and a finish check
where the factory marks its own definition-of-done items — only
where it holds real evidence — and writes the report the approval
stage reads. This release (v0.4) makes the factory actually build
games: three archetype generators chosen from the scope, asset
generation (vector sprites, a real icon, synthesised audio), and a
headless test stage where scripted players — competent, breaker,
first-time — play the game for real, so the finish check can now
mark its whole checklist from evidence. This release (v0.5) closes
the loop after the game exists: player feedback is fetched and
triaged, approved items become patches that must pass the test
stage and a save-compatibility check before they ship as a tagged
version; failing test runs can repair themselves within strict
bounds; and a model-driven playtester — your own vision model,
playing through a bridge inside the game — joins the scripted
players, filing its confusions straight back into the feedback
queue. v0.6 put every app action on a background job runner and
added the balance lab and the asset studio. This release (v0.7)
rebuilds the factory's insides around the user's direction: every
step is an independent worker coordinated by an orchestrator, and
generation composes primitives instead of picking an archetype —
the archetypes survive as presets, and scopes no composition can
express take a bespoke, brain-written mechanics path. Publishing
stays last, on purpose: the store connectors are still to come.

## Quickstart

```
./dist/weaverbird
```

That is the whole quickstart. Running Weaverbird starts the app and
opens it in your browser. The folder you run it in is the workspace —
every project in it is on the dashboard. (From a clone of this
repository, `./build.sh` first produces the one self-contained
executable, `dist/weaverbird`.)

```
./dist/weaverbird update          # keep Weaverbird current
./dist/weaverbird update --check  # just say whether an update exists
./dist/weaverbird version         # print the version
./dist/weaverbird help            # the app launch + the command list
```

`weaverbird update` reads the latest GitHub release of
wrenly40/weaverbird. Running as the standalone executable, it
downloads the release's `weaverbird` asset, proves the new file runs
and reports the new version, and only then swaps it in — the old one
is kept as `weaverbird.bak`. Running as a native install, it takes
the platform's native asset instead (`weaverbird-linux`,
`weaverbird-windows.exe`, `weaverbird-macos.zip`), falling back to
the zipapp asset when a release carries no native build. From a source checkout it pulls and
rebuilds instead (and refuses while the tree is dirty); other
installs get the exact upgrade command printed. For this to work
end-to-end, releases are published on GitHub with the built
executable attached as the `weaverbird` asset.

Inside the executable lives everything the app needs: the core
library, the scope and settings templates, and the connector
registry. Its one library dependency is bundled in too. The
executable works from any working directory, on any copy of the app.
Launch flags: `--workspace <dir>` (another workspace), `--port <n>`,
and `--no-open` (serve without opening a browser — for servers and
tests).

You also need Python 3.11 or newer to run it, Godot to build games
with it, and git for versioning. (No Python at all? The native
builds — `weaverbird-linux`, `weaverbird-windows.exe`,
`weaverbird-macos.zip` on each release — carry their interpreter
with them; see `docs/RELEASING.md`.) Store accounts, signing keys and
model-provider keys stay yours, in your own vault — those are external
by nature, not missing pieces of the app.

A secondary route, if you prefer it: `python3 -m pip install .` gives
you a `weaverbird` command the same way.

### Your first game, in the app

Run Weaverbird in a folder that will hold your games — that folder
is the workspace:

```
mkdir games && cd games
../dist/weaverbird        # the app opens in your browser
```

On the dashboard, **New project** starts a game: give it a name,
check the folder name suggested from it, and — if you already know
what you want to make — write a starting prompt describing the game
in plain words. Create, and you land in the scope interview. The
interview reads your prompt first and fills in everything it states
(the opening recap says what it took), then asks only about what is
missing, in small groups. One group shows generated style samples;
the style you pick is locked into the scope, and can be changed
later only by choosing Change style on the picked card. When every
required field is filled, approve the scope on the same page and
the factory builds from there — skeleton, assets, the game itself,
tests and the finish check, each with its own card on the project
page.

Budgets and models live in one global settings file, and the app
can start that too: **Settings → Create settings file** writes the
template into the workspace root, and the same page edits the whole
file — budgets, and the model ladder: provider, model, and the
*name* of the environment variable that holds each key. Key values
are never typed into the app. With no settings file the factory
still works — interviews run deterministically and no model is
called.

### The same flow, as commands

Everything the app does exists as commands over the same core, for
scripting:

```
./dist/weaverbird init my-game --with-settings
./dist/weaverbird scope questions my-game
./dist/weaverbird scope check my-game
./dist/weaverbird scope approve my-game --by <your name>
./dist/weaverbird skeleton build my-game
./dist/weaverbird assets generate my-game
./dist/weaverbird build game my-game
./dist/weaverbird test run my-game
./dist/weaverbird finish check my-game
```

The skeleton command generates the grey-box Godot project into
`my-game/game/`, validates it with Godot, and tags it known-good in
the project's git history. Advancing the pipeline out of the scope
stage (`weaverbird pipeline advance my-game`) runs the same builder
through the pipeline's gates. The finish check marks the
definition-of-done items the evidence supports; `weaverbird finish
report my-game` writes the full `FINISH_REPORT.md`.

### The brain is bring-your-own-key

Weaverbird ships no keys and calls no model on its own account. You
configure a model ladder — cheap, mid, frontier — in the global
settings file, and each tier names the environment variable that
holds your key. The key is read from the environment when a call is
made and is never written to any file, log, ledger or report.

```yaml
models:
  cheap:    { provider: openai,    model: "gpt-4o-mini",      api_key_env: OPENAI_API_KEY }
  mid:      { provider: openai,    model: "gpt-4o",           api_key_env: OPENAI_API_KEY }
  frontier: { provider: anthropic, model: "claude-sonnet-5",  api_key_env: ANTHROPIC_API_KEY }
```

```sh
export OPENAI_API_KEY=...        # your key, in your shell only
export ANTHROPIC_API_KEY=...
./dist/weaverbird brain check            # per tier: provider, model, key present (yes/no)
./dist/weaverbird brain check --live     # one tiny test call per usable tier
./dist/weaverbird scope interview my-game --prompt "A tiny 2D game about a bee collecting stars before winter"
```

Two provider kinds are supported: `openai` — OpenAI-compatible chat
completions, which also covers OpenRouter, Together, and local
servers such as Ollama or LM Studio via `base_url` (a local server
needs no key) — and `anthropic`, the Messages API. An optional
`cost_per_1k_tokens` per tier prices the ledger entries; token counts
are always recorded from the provider's own response. Calls are
refused once a project reaches the global per-game token cap, and a
call that crosses the cap says so, loudly. With no key or no config,
everything degrades gracefully: commands that can work without a
model still work, and say plainly that no model is configured.

## The pipeline

Every game moves through the same stages, one at a time, and cannot
skip ahead:

scope → skeleton → assets → build → headless test → playtest →
finished → approval → publish → live

Each stage has a gate. Where the factory can check a gate itself, it
does: the pipeline will not leave the scope stage until the scope file
is complete and approved, and a game cannot be called finished until
every item on the definition-of-done checklist is true.

Since v0.7 the work inside those stages is done by independent
workers — one per step, each runnable on its own — coordinated by an
orchestrator that plans the steps, skips the ones whose inputs have
not changed, and routes failures back to the worker that owns them.
See "New in v0.7" below.

See `docs/PLATFORM.md` for the full platform map — what each stage
takes in, what it produces, and what is built now versus later — and
`docs/DESIGN.md` for the full design.

## What works today

New in v0.10:

- Exports (`weaverbird export <dir>`, and the Exports card on a
  finished project's page in the app). A registered **export
  worker** packages a finished game as local zip files in the
  project's `exports/` folder — Linux, Windows and macOS builds
  via Godot's headless export, plus a zip of the game source.
  It ensures the project's `export_presets.cfg` covers the
  desktop targets (adding only what is missing, never editing a
  maker's own presets), runs each export, verifies every
  artifact, and writes `EXPORT_REPORT.md` with each target's
  status, size and sha256. A target that cannot run — no Godot,
  no export templates, a Godot error — is reported **blocked**
  with the exact reason, never silently skipped. Desktop builds
  are unsigned: signing and notarisation stay the maker's own
  step. Android and iOS are not exported (they need SDK and
  keystore setup). **Exports produce local files only;
  publishing to stores is not part of this release.**
- Scope numbers are reconciled. When the scope's win condition
  states a count ("Collect 10 stars"), generation uses it
  instead of the default, records the derivation, and the
  finish report checks the built numbers against the scope's.
  The project page shows a **Build summary** before anything
  runs — the composition, the key numbers flagged "from your
  scope" or "default", the style lock and the planned asset
  kinds — so a surprise surfaces before the build, not after.
- World layouts vary. Collectibles are placed by deterministic
  patterns (ring, arc, scattered, clusters, zigzag) chosen from
  a per-project seed and recorded in `world.json`; the world
  gate still checks every placement, and a pattern that fails
  falls back to the next, on the record.
- A bundled demo project: `weaverbird demo` (or the dashboard
  button) installs a small finished game — scope, source,
  reports — into the workspace, ready to look around, re-test
  and export.

New in v0.8:

- The scope interview is a conversation (`src/weaverbird/interview.py`).
  One session engine drives both front ends: the app's scope page
  is a chat — message thread on the left, the scope filling in
  live on the right, the Approve button enabling exactly when
  every REQUIRED field is filled — and `weaverbird interview
  <dir>` runs the same conversation in the terminal. The existing
  form stays as "Edit as form". Without a brain, the engine asks
  each group of questions in plain language, captures free-text
  or `Field: value` answers conservatively, and reflects every
  captured field back before moving on; it never re-asks a field
  the prompt or an earlier answer already filled (the stored
  prompt is run through the existing extraction first). With a
  brain configured, the cheap tier reads the transcript and the
  still-empty fields each turn and returns strict JSON; the
  engine validates every value against the scope schema and
  drops anything unknown or invalid (it is re-asked, never
  silently written), and any brain failure falls back to the
  deterministic question for that turn. When the conversation
  reaches the style lock, the factory generates sample cards —
  small scenes drawn by the asset code from four candidate
  palettes — and picking one (click, or type its name) writes
  the style lock and the palette into the scope. Turns run as
  jobs under the scope worker, so a slow brain call shows
  progress like everything else.
- Games have a dimension. The composition now carries
  `dimension: 2d | 3d` (from the scope's Dimension field, or a
  "3D" mention in the genre/title/pitch), defaulting to 2d with
  every existing behaviour unchanged. One 3D preset ships:
  **arena_3d**, the arena composition on a 3D ground plane — a
  `CharacterBody3D` player under a follow camera, spinning
  collectible orbs, patrolling hazards and obstacle blocks, all
  primitive meshes coloured from the style-lock palette with an
  ambient-lit environment (no external assets). The world
  worker emits the 3D world data and gates it on the XZ plane;
  the assets worker emits a material palette spec instead of
  sprites (the icon and audio are generated as usual); the
  shell, the bridge facts (positions gain a y the testers
  ignore) and the scripted testers all work as they do in 2D,
  and the full test stage passes on a generated 3D game with
  win and lose proven. Asking for 3D with any other composition
  is recorded honestly in the derivation and built in 2D — 3D
  covers one flat-ground preset today; 3D platforming is not
  claimed.
- Native installers. `./build-native.sh` freezes Weaverbird
  with PyInstaller into `dist/native/weaverbird-<platform>` —
  the same app with its Python bundled in, so the machine
  running it needs no Python at all (the zipapp stays the
  default build). The manual GitHub workflow builds all three
  platforms (the macOS artefact is a zipped `.app` bundle), and
  `weaverbird update` on a native install prefers its
  platform's native asset, falling back to the zipapp asset,
  with the same verify-then-swap and `.bak` semantics. See
  `docs/RELEASING.md` for how a release is cut — and why the
  workflow stays manual (macOS minutes bill at 10×).

New in v0.7:

- Every step is an independent worker (`src/weaverbird/workers.py`).
  Scope gate, skeleton, world, mechanics, assets, assembly,
  testing, model-playtest, balance, repair, feedback, patch and
  finish each sit behind one contract: declared inputs, declared
  outputs, named dependencies, and a run that returns a result
  with artifacts, metrics and issues (each issue naming the
  worker that owns it). `weaverbird workers list` shows the
  registry. Every run writes a record to
  `.weaverbird/workers/<name>.json`, including a hash of the
  worker's inputs. The old build stage is split in three: the
  **world worker** writes the world's data (layouts, rounds,
  puzzles) to `game/world/world.json` and gates it (platformer
  reachability invariants, crate solvability through the solver,
  arena bounds) before anything downstream runs; the
  **mechanics worker** writes the player and game-logic scripts
  that interpret that data at runtime, and gates on every entity
  kind in the world having a handler; the **assembly worker**
  wires shell + mechanics + world + assets into the runnable
  game, validates it in Godot and tags it known-good. Asset
  generation now takes its requests from the composition and the
  world manifest — the entity kinds present decide the sprites,
  the movement and fail kinds decide the sounds.
- The orchestrator (`src/weaverbird/orchestrator.py`,
  `weaverbird orchestrate <dir> [--to STAGE] [--force]`, and the
  app's Plan card). It plans the worker DAG for a project
  (`.weaverbird/plan.json`), runs it through the job runner
  from the app or synchronously from the CLI, and skips any
  worker whose inputs have not changed since its last good run
  — touching an input re-runs exactly its dependents. When a
  gate fails, the issues go back to the owning worker for one
  bounded retry (the world worker genuinely self-corrects: a
  failing platformer level is regenerated with a different
  seed); still failing, the plan stops and escalates with the
  owner and the issues. Pipeline advance runs through the same
  worker adapters, so the CLI, the app and the orchestrator
  cannot diverge.
- Generation is composition-based, and the factory is archetype
  independent. A game is described by composing primitives —
  movement (top-down, platform, grid), objectives (collect,
  reach the exit, push to targets, survive), fail conditions,
  structure (levels, rounds) and interactions — derived
  deterministically from the scope, with the derivation recorded
  in `game_config.json`. The three archetypes are now **presets**:
  named compositions you can pick with a `preset:` note in the
  scope. When a scope's core loop cannot be composed from the
  primitives at all (the matcher is deliberately conservative),
  the build is **bespoke**: with a brain configured, a model
  writes one GDScript module against the documented contract in
  `docs/MODULE_CONTRACT.md`, hosted by a factory shell and held
  to the same assembly validation and test stage as any other
  game. Without a brain, the orchestrator stops at the plan and
  says plainly what it cannot compose yet.

New in v0.6:

- The job runner (`src/weaverbird/jobs.py`). Every action in
  the app — build skeleton, generate assets, build game, run
  tests, finish check and report, repair, patch shipping,
  feedback fetch/triage, the model playtest, and the balance
  and studio actions below — now runs as a background job
  instead of freezing the page. Jobs persist as JSON under
  `<workspace>/.weaverbird-app/jobs/`, run on a small pool (2
  at a time; two jobs for the same project serialise), and
  report at step boundaries, so the app shows a live card:
  status, elapsed time, a progress bar (steps done of steps
  expected, or an indeterminate bar when a stage cannot know),
  a scrolling event log, and a Cancel button. Cancelling is
  cooperative — it lands between steps, never mid-Godot-run,
  and the record says so. A job a dead server leaves behind is
  marked `interrupted` on the next start, honestly, not
  resumed. Each record also names the `worker` that ran the
  action (skeleton, assets, build, testing, finish, repair,
  patch, feedback, balance, model-playtest) — the planned
  orchestrator will key its plan view off it. From the shell:
  `weaverbird jobs <workspace>` and `weaverbird jobs show
  <workspace> <id>`. The dashboard gained a Jobs strip, and
  each project page lists its job history.
- The balance lab (`weaverbird balance show|suggest|apply|
  compare <dir>`, and the app's Balance view). The competent
  and first-time scripted players now record per-unit stats —
  per level, round or puzzle: completed, deaths, time, moves
  against budget where the game counts moves — and the test
  report aggregates them under `units`. The lab reads that
  table only (never the game's kind; a metric a game does not
  record shows as unavailable) and flags, with the numbers:
  difficulty spikes, walls the competent player cannot pass,
  first-time stalls, and breezes (everything completed, no
  deaths — maybe too easy). Suggestions use only the patch
  loop's bounded knobs: ease, harden, or a clearer objective
  hint for a stall. Applying one files a synthetic feedback
  record (source `balance-lab`) and goes through the full
  patch gates — test stage plus save-compatibility — with the
  version bump and a changelog entry citing the lab numbers;
  if the gates fail, nothing ships. Compare runs a checkpointed
  A/B (candidate config in, test stage, original restored
  exactly) and writes `.weaverbird/balance_report.json` with
  side-by-side metrics and a plain verdict; it costs up to two
  full test runs, and says so.
- The asset studio (`weaverbird assets list <dir>`,
  `weaverbird assets regenerate <dir> <name>`, and the app's
  Assets view). Generation is now a
  registry of named generators — one per sprite, the icon,
  one synthesiser per audio file — and the studio lists every
  asset from the registry, the assets manifest and the
  provenance log, with sizes and missing files flagged (an
  asset on record that no current generator produces is still
  listed, marked not regenerable). Regenerating redraws exactly
  one file from the current palette — audio re-synthesised —
  and records it in the provenance log, at zero tokens. The
  palette editor writes the palette hexes back into SCOPE.md,
  shows a before/after swatch strip, offers Regenerate all
  assets, and notes that a game rebuild picks up the
  palette-driven template colours. The gallery renders SVGs as
  images, plays audio with the browser's own controls, shows
  the icon at three sizes, and serves files through a
  read-only route confined to the project's `game/assets`
  directory plus the icon.

New in v0.5:

- The feedback + patch loop (`weaverbird feedback …`,
  `weaverbird patch start <dir> --item <id>`, and the app's
  Feedback view). Feedback lands in `.weaverbird/feedback.json`
  from three sources: GitHub Issues of the project's linked repo
  (through the `gh` CLI), itch.io comments when `ITCH_API_KEY` is
  set (best-effort — a failed source is recorded, never fatal),
  and a manual inbox (`feedback add`, or files dropped in
  `feedback-inbox/`). Triage classifies and clusters
  deterministically (bug / balance / confusion / request /
  praise), ranked by mentions × severity; a configured cheap
  brain tier may refine it under validation. Only items you
  approve can patch, and only in-scope ones: balance items tune
  `game_config.json` within the generator's bounds, confusion
  items rewrite the objective hint, known-class bugs get their
  matching remedy — anything else becomes a PATCH_PLAN.md and
  stops, and requests always need your decision. A patch ships
  only if it passes the full test stage AND a save-compatibility
  check (a save captured from the pre-patch build must load in
  the patched build with its stats intact). Shipping bumps the
  game version in `game/VERSION`, appends `CHANGELOG.md`, and
  tags `known-good/patch-<version>`; failing either gate restores
  the pre-patch checkpoint exactly.
- Self-repair (`weaverbird repair run <dir>`, or automatically
  after a failed test run when `repair.max_attempts` > 0 in the
  global settings, default 3). Failures are classified —
  crash_or_error, win_not_proven, lose_not_proven,
  performance_low, breaker_stuck, missing_assets — and each
  attempt applies one strategy from a fixed menu (bounded config
  nudges, asset/game regeneration for known crash classes)
  behind a git checkpoint. A change survives only if the re-run
  strictly improves; otherwise the checkpoint is restored.
  Success tags `known-good/repair`; running out of attempts
  restores the best state and writes REPAIR_REPORT.md saying
  what needs you. The project is never left worse than it
  started — that is proven by a test that sabotages a game into
  an unwinnable state and watches repair bring it back.
- The model-driven playtester (`weaverbird playtest model
  <dir>`), the fourth player. Generated games ship a bridge
  (`game/tests/model_bridge.gd`): launched with
  `--model-bridge`, the game publishes its honestly exposed
  state and a screenshot every decision tick, pauses, and
  applies the chosen action as real input. The driver asks a
  vision-capable tier of your own brain (BYOK, ledgered) for one
  strict-JSON action at a time, stops at the win/lose screens,
  the decision cap, or repeated invalid replies, and writes
  `.weaverbird/model_playtest.json` plus a "model" entry in the
  test report. Its confusion notes are filed as feedback items,
  so the loop above consumes them. With no vision tier
  configured it says exactly what is missing and touches
  nothing. The scripted players remain the deterministic gate;
  the model player is judgement on top — verified here with a
  scripted policy driving the real bridge, since no live model
  call can be made without a key.
- The app grew the matching surfaces: a Feedback view per
  project (fetch, triage, add, approve/dismiss, ship-as-patch),
  a repair button on failing test reports, and a Model playtest
  button that reports the missing-brain message cleanly. The
  finish report now lists shipped patches.

New in v0.4:

- Real game generation (`weaverbird build game <dir>`, and the
  pipeline's build stage). The scope's genre, core verb and win/lose
  text pick the archetype — or say `archetype: platformer` in a scope
  field, or pass `--archetype`: **arena collector** (rounds of
  targets against the clock, hazards that grow per round),
  **platformer** (gravity, run and jump with a coyote window,
  procedurally varied levels with star staircases, spike pits,
  lives), and **crate pusher** (grid puzzles with move budgets and
  undo — every shipped puzzle verified solvable by the solver in
  this codebase, its verified solution recorded in the config). The
  scope's content counts become levels, rounds or puzzles; all
  tuning lives in `game/game_config.json`, which the games read at
  runtime. When a brain is configured, the mid tier may propose
  tuning numbers — every one validated and clamped to the same
  bounds as the deterministic defaults, and any failure falls back
  to them silently. The build validates in Godot (import, menu,
  game, and a flow smoke that plays the game to a win), then
  commits and tags `known-good/build`.
- Asset generation (`weaverbird assets generate <dir>`, and the
  pipeline's assets stage). Deterministic, no model: flat vector
  sprites drawn from the scope's style-lock palette (player, target,
  hazard, platform tile, spikes, exit door, crate, pad, wall,
  background), a real game icon with the archetype's motif — not
  the skeleton placeholder — and audio synthesised with the standard
  library: click, collect, hurt, jump, push, a win jingle, a lose
  tone, and a looping music track. The games use the assets when
  present and fall back to code-drawn shapes when not. Every file
  gets a provenance record as it is made.
- The headless test stage (`weaverbird test run <dir>`, and the
  pipeline's headless_test and playtest stages). Base runs (import,
  menu scene, game scene — any Godot error fails them) plus three
  scripted players shipped inside the generated game: a **competent**
  player who plays to a win and then to a loss through the game's
  real input and logic (steering, or replaying the verified crate
  solutions as real key presses); a **breaker** who fuzzes inputs,
  spams the pause menu, and — in the crate game — shoves a crate
  into a corner to check the game notices (it does, and tells the
  player); and a **first-time** player who presses the menu's real
  Play button and follows only the on-screen hint. Performance is
  measured on the win run (average FPS against a target that
  defaults to 30 and can be set under `testing:` in the global
  settings, plus scene load time). The verdict lands in
  `.weaverbird/test_report.json`. These players are deterministic
  scripted bots; the model-driven, vision-using playtester that
  joins them arrived in v0.5 (above).
- The finish check now closes its checklist. With a test report on
  record, the lose half of "winnable and losable" is marked from
  the competent player's two runs, performance from the measured
  numbers, content counts from the generation report checked
  against the scope, and the icon/polish gate from the assets
  report plus the test report's clean runs — each still with its
  evidence written next to the mark, and anything short stays open
  with the numbers in the reason. The finish report describes the
  generated game, its assets and its test verdict alongside
  everything from before.
- The app grew with the stages: the project view shows the chosen
  archetype and what was delivered, a test summary card (verdict,
  win/lose proven, performance, the three players' results), and
  working Generate assets / Build game / Run tests buttons, enabled
  exactly when the CLI would allow them.

From v0.3:

- The brain (`weaverbird brain check`, `weaverbird scope interview
  <dir> --prompt "..."`). A bring-your-own-key model layer over the
  standard library only — see "The brain is bring-your-own-key"
  above. Its first job is the scope interview: the prompt is stored
  in the scope file, the cheap tier extracts answers for the empty
  REQUIRED fields (it never invents and never overwrites a filled
  field), and the remaining questions are printed. Every call is
  written to the project's ledger with the provider's real token
  counts before the result comes back, and the global per-game token
  cap is enforced before a call is made.
- The app (bare `weaverbird` — there is no separate launch command).
  A local web app over the same library, in the shipped visual
  identity: a dashboard of the workspace's projects with their stage
  and spend, the factory's own status (GitHub, Godot, brain per
  tier) and the global budget; a project view with the pipeline
  stepper, the definition of done with its evidence, the ledger and
  the provenance log, and working actions — build skeleton, run
  finish check, write finish report, advance pipeline; the scope
  interview as a form, with prompt extraction when a brain is
  configured and approval when the scope is complete; and a settings
  view (budgets editable, everything else read-only, key values
  never shown). The app binds to 127.0.0.1 only and has no
  authentication, because it never leaves the machine it runs on.
  Alongside it, the CLI is now minimalist on the surface — bare
  launch, `update`, `version`, `help` — with the project commands
  kept underneath for scripting (`weaverbird help` lists them).
- Self-update (`weaverbird update [--check]`) from GitHub releases,
  as described in the quickstart: verify-before-swap for the
  standalone executable with a `weaverbird.bak` rollback, pull +
  rebuild for a source checkout, printed instructions otherwise.
- The finish check and report (`weaverbird finish check <dir>`,
  `weaverbird finish report <dir>`). The factory marks its own
  definition-of-done checkboxes, but only where it holds evidence —
  the skeleton's Godot validation runs, the generated game's menu,
  settings and save scripts, the flow smoke's win — and stores the
  evidence next to each mark. What it cannot evidence stays open with
  a plain reason — in v0.3 that honestly included the lose path,
  performance, the scope's content counts and the polish gate,
  which the v0.4 stages above now evidence for real. A test
  report at `.weaverbird/test_report.json`, when one exists, takes
  precedence.
  The report is the approval stage's input document: what was built,
  the checklist with its evidence, what everything cost, the
  provenance summary with the store disclosure draft, and the
  decisions left for the human.

From v0.2:

- The skeleton builder (`weaverbird skeleton build <dir>`, and the
  pipeline's skeleton stage). From a complete, approved scope it
  generates a complete Godot 4 project into `<dir>/game/`: main menu
  with the scope's title and pitch, settings with a saved volume
  slider, pause, win and game-over screens, save/load of settings
  and run stats, and a genuinely playable grey-box loop — move with
  WASD or arrows, collect every target before the timer ends, avoid
  the hazards. Title, target count, round length and colours are
  taken from the scope where it states them. Everything is generated
  from templates in code: no model is called, and the stage writes a
  zero-cost entry to the ledger to prove it. Every generated file is
  recorded in the provenance log.
- Real validation, not just generation. The builder runs Godot
  headless against the project it just made — an import pass, a run
  of the menu scene, a run of the game scene, and a flow smoke run
  shipped in the project (`game/smoke/smoke.gd`) that plays
  menu → game → win by itself. Any Godot error fails the build with
  a short digest, the pipeline does not advance, and the results are
  written to `.weaverbird/skeleton_report.json`. A passing build is
  committed to the project's git and tagged `known-good/skeleton`.

From v0.1, the infrastructure:

- Global settings with budgets. Token and cost caps live in one
  global settings file, never in a single game's scope. Settings are
  checked for the required budget fields before any build.
- The scope file. Created from the prompt, filled in during the
  interview, and checked field by field. The factory lists exactly
  which REQUIRED fields are still missing and turns them into grouped
  questions. A complete scope can be approved and frozen; an
  incomplete one cannot.
- The connector registry. Every outside service — GitHub, itch,
  Steam, Google Play, Apple signing, model and asset providers,
  feedback sources — declares what it may do on its own, what needs
  your approval, and what only you can do. Anything not declared
  fails closed: it is treated as needing approval.
- The token ledger. Every project keeps a ledger of spend by stage
  and by model tier, which can be checked against the global budgets
  (fine, warning, or over).
- The provenance log. Every asset records how it was made — in code,
  by a model, or from a licensed library. From those records the
  factory drafts the AI-content disclosure a store like Steam asks
  for, and it can list assets that have no record.
- Pipeline state. Each project's stage, history and
  definition-of-done checklist, kept on disk and moved only in order,
  only through its gates. Stage handlers can be registered by future
  modules that do the work of a stage.
- Local git helpers. A commit per iteration, tags on known-good
  states, rollback to a tag. A GitHub check reports the logged-in
  account name — never a token.
- Godot detection and a `project.godot` reader.
- A command-line front end over all of the above (`doctor`, `init`,
  `scope`, `brain`, `settings`, `connectors`, `ledger`, `provenance`,
  `pipeline`, `skeleton`, `finish`, `app`).

## Platform stages still to build

These are stages of the platform on the roadmap, in the order the
pipeline runs — not things left out of scope:

- More composition primitives (new movement, objective and fail
  kinds compose into new kinds of game without new templates),
  richer bespoke mechanics, and the iterate loop around them.
- More of the third dimension: 3D movement beyond the flat
  ground plane (jumping, ramps), and 3D forms of the other
  compositions.
- Store connectors that upload and publish (itch first), signing and
  notarisation. Publishing comes last, by the user's direction.

## Look and feel

Weaverbird has one visual identity across the terminal and the app:
light, flat and quiet, with a single violet accent and lots of
whitespace. The design system is written down in
`docs/VISUAL_IDENTITY.md`, and its tokens and wordmark ship as code
inside the executable — run `weaverbird identity` for the summary, or
`weaverbird identity --export <dir>` to take the tokens and the mark
elsewhere. The app's screens follow the same system; the design
reference lives in `docs/visual-prototype/`.

## Architecture

All logic lives in the library, `src/weaverbird/`. The CLI
(`src/weaverbird/cli.py`) is a thin front end: it parses arguments,
calls the library, and prints plain results. The app
(`src/weaverbird/app.py`) is a second front end over exactly the same
library — no logic of its own — and a background job runner will call
the same core again. The platform is the core, and front ends come
and go.

## Principles, in plain language

Tokens are spent only on judgement and new work. Repetitive work —
menus, save systems, standard content — comes from templates and
generators written in code. Models are used as a ladder: a cheap model
for sorting and summarising, a mid model for routine edits, the
strongest model only for design decisions, hard problems and playtest
judgement. Raw logs are never fed to a model; failures are digested
first. Budgets are hard: when a global cap is reached, the factory
stops and escalates to the user instead of spending on.

Connectors never widen their own permissions, and a missing connector
never blocks the factory — it falls back (no GitHub means local git;
no store connector means a prepared package and a checklist for manual
upload).

Games made with Weaverbird belong to their makers. There is no
royalty and no claim on the output.

## Running the tests

```
PYTHONPATH=src python3 -m unittest discover -s tests
```

## Licence

MIT. See `LICENSE`.
