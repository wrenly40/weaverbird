# Weaverbird as a platform — the full end-to-end map

Weaverbird is a full end-to-end platform for making games, not a single
narrow tool. A user arrives with a prompt and leaves with a published
game that keeps getting polished from player feedback. The stages below
are the whole journey. Most are built; the ones that are not say so,
and what each future module plugs into, so nothing has to be redesigned
later.

The packaged form of the platform is one executable: `dist/weaverbird`,
built by `./build.sh`. That single file is the app. Future platform
stages — generation workers, playtest runners — ship inside this same
executable as new library modules. They are not separate services a
user has to assemble.

## Entry model (v0.3)

The app is the product surface. Bare `weaverbird` starts the local
server (workspace = the current folder, where projects are
discovered) and opens the app in the user's browser; `--workspace`,
`--port` and `--no-open` adjust the launch, and Ctrl+C stops it.
There is no separate app command. The CLI surface is minimalist —
bare launch, `weaverbird update`, `weaverbird version`,
`weaverbird help` — with the project commands (init, scope, brain,
skeleton, pipeline, finish, ledger, provenance, connectors,
settings, doctor, identity) kept underneath for scripting, listed in
help under "Project commands (scripting)". The server and every
command call the same library, so the surfaces cannot drift apart.

`weaverbird update [--check]` keeps the executable current from the
GitHub releases of wrenly40/weaverbird: the standalone executable
downloads the release's `weaverbird` asset, verifies it runs and
reports the new version before swapping itself (the old file is kept
as `weaverbird.bak`); a source checkout is pulled and rebuilt (and
refused while dirty); other installs are told the exact upgrade
command. Releases are therefore published with the built executable
attached.

## How the pieces fit

- The library (`src/weaverbird/`) is the core. Every stage works
  through it.
- The CLI is a thin front end over the library. A future desktop or
  web UI, and a background job runner, will call the same library
  functions the CLI calls today. Nothing important lives in the CLI.
- Four records travel with every game project and are the interfaces
  between stages:
  - the scope file (`SCOPE.md`) — what the game is,
  - the pipeline state (`.weaverbird/state.json`) — where it is,
  - the ledger (`.weaverbird/ledger.jsonl`) — what it has cost,
  - the provenance log (`.weaverbird/provenance.json`) — how its
    assets were made.
- Since 0.7.0, two more: the worker run records
  (`.weaverbird/workers/<name>.json` — what each independent worker
  last did, and the hash of the inputs it did it from) and the
  orchestrator's plan (`.weaverbird/plan.json` — the worker DAG
  for the project and what happened when it ran). The stages below
  are, since 0.7.0, executed by the workers in the "Workers and
  the orchestrator" section; the stage list remains the journey,
  the workers are how each step is owned and run.
- Global settings hold the budgets and model choices for the whole
  factory. Connectors are how anything external is reached.

## The stages

### 1. Prompt intake and scope interview
- Inputs: the user's prompt, in plain language.
- Outputs: a scope file, created immediately and filled in live while
  the factory asks grouped questions, until every REQUIRED field is
  filled and the user approves it. Style lock and provenance policy
  are decided in this phase and written into the scope file.
- Built in v0.1: the scope file format, the parser, the missing-field
  report, the grouped interview questions, and approval (which is
  refused while the scope is incomplete). The pipeline cannot leave
  this stage without a complete, approved scope.
- Built in v0.3: the bring-your-own-key brain (`brain.py`) and the
  interview's first working form. `weaverbird scope interview <dir>
  --prompt "..."` stores the prompt in the scope file, then the cheap
  tier extracts answers for the still-empty REQUIRED fields (strict
  JSON, unsure fields stay empty, filled fields are never
  overwritten) and the remaining grouped questions are printed. The
  call is logged to the ledger under the scope stage. Without a
  configured model it degrades plainly: the prompt is stored, nothing
  is extracted, and the questions carry the interview. The app's
  scope view is the same flow as a form over the same library, with
  the prompt box and the approve button.
- Future: the multi-turn interview conversation (follow-up rounds
  driven by the model, style samples shown and picked during the
  interview). It plugs into: scope parser, pipeline state, ledger
  (its model calls are logged).

### 2. Design and skeleton
- Inputs: the approved scope file, the archetype templates.
- Outputs: a grey-box Godot project — the full loop playable with
  placeholder shapes, menus, save, settings, pause and input wired.
- Built in v0.1: the pipeline stage and its place in the order,
  Godot detection (`godot.py`) and a `project.godot` reader, local
  git initialisation helpers.
- Built in v0.2: the skeleton builder itself (`skeleton.py`, with
  the templates packaged in `skeleton_templates/`). It refuses an
  incomplete or unapproved scope, renders one generic grey-box
  archetype (arena, collect-the-targets, hazards, a round timer)
  with the scope's title, pitch, target count, round length and
  style colours filled in, records every generated file in the
  provenance log, adds a zero-cost ledger entry, and validates the
  result by running Godot headless: import, menu scene, game scene,
  and the flow smoke run shipped in the project
  (`game/smoke/smoke.gd`, menu → game → win). Results land in
  `.weaverbird/skeleton_report.json`; a passing build is committed
  and tagged `known-good/skeleton`. It is registered as a pipeline
  stage handler (`register_stage_handlers()` in `skeleton.py`), so
  advancing out of the scope stage runs it through the gates, and
  `weaverbird skeleton build <dir>` runs it directly.
- Future: more archetypes (the builder takes the archetype from the
  scope), and design work beyond the grey-box loop. It plugs into:
  pipeline state (registered as a stage handler), the stage
  registry in `state.py`, git commits per iteration.

### 3. Asset generation
- Inputs: the scope file's style lock and provenance policy.
- Outputs: 2D assets first (3D later), sounds and music, all recorded
  in the provenance log at the moment they are made. An asset with no
  provenance record is not used.
- Built in v0.1: the provenance log, its add/list functions, the
  unrecorded-asset check, and the store disclosure draft built from
  the records.
- Built in v0.4: the deterministic generator (`assets_gen.py`,
  `weaverbird assets generate <dir>`, and the pipeline's assets
  stage handler). Flat vector sprites (SVGs) drawn from the style
  lock's palette — the first palette hex is the hero/accent, the
  second the targets, a hue-shifted relative the hazards — covering
  every archetype: player, target, hazard, platform tile, spikes,
  exit door, crate, pad, wall, background pattern; a real game icon
  built from the palette and the archetype's motif, replacing the
  skeleton placeholder; and audio synthesised with the standard
  library `wave` module (22050 Hz mono): click, collect, hurt, jump
  and push effects, a win jingle, a lose tone, and an 8-second
  looping music track. Every file is provenance-recorded as it is
  made (source "code", provider "weaverbird-assets") and summarised
  in `.weaverbird/assets_report.json`; the ledger entry is
  zero-token. Games load the assets when present and fall back to
  code-drawn shapes when they are not.
- Built in v0.7: asset generation is a worker, and what it
  generates is selected by the game's composition and world
  manifest, not by an archetype name — the entity kinds present
  in `game/world/world.json` decide the sprites, the movement
  and fail kinds decide the sounds. Preset games keep the
  factory's shared asset set (their templates load it
  opportunistically, as before); bespoke games get exactly
  their derived requests. The assets report records the
  requests and what they were based on (world or composition).
- Future: model-provided art through connectors with the user's own
  keys, richer palettes per archetype, 3D assets. It plugs into:
  provenance, connectors (asset providers), ledger.

### 4. Build loop
- Inputs: the skeleton, the assets, generated game code.
- Outputs: a complete Godot project, versioned in git with a commit
  per iteration and a tag on every state that passes the tests.
- Built in v0.1: local git wrap — init, commit everything, tag a
  known-good state, roll back to a tag, list tags.
- Built in v0.4: archetype game generation (`generate.py`,
  `weaverbird build game <dir>`, and the pipeline's build stage
  handler). The scope's genre, core verb and win/lose text choose
  between three archetypes — arena collector, platformer, crate
  pusher — with an explicit `archetype:` scope field or
  `--archetype` flag winning over detection; the choice and its
  reason are recorded in `.weaverbird/generation_report.json`. The
  scope's content counts become arena rounds, platformer levels
  (seeded layouts: ground with spike pits, star staircases within
  jump reach, exit doors) or crate puzzles (a built-in set verified
  loop-by-loop with the solver shipped in `generate.py`, cycled
  with mirroring beyond the set, each puzzle's budget derived from
  its verified solution length). All tuning is data: the games read
  `game/game_config.json` at runtime. With a brain configured, the
  mid tier may propose tuning numbers as strict JSON; every value
  is validated and clamped to the archetype's bounds, and any brain
  failure falls back to the deterministic config. The build ships
  the scripted test players of stages 5–6 inside the game
  (`game/tests/`, the competent win player doubling as the flow
  smoke), validates with the skeleton's discipline (import, menu,
  game, win flow), then commits and tags `known-good/build`.
- Built in v0.7: the build stage is three independent workers,
  and generation is composition-based (archetype independence).
  The decision layer is a **composition** — movement (top_down /
  platform / grid) × objectives (collect / reach_exit /
  push_to_targets) × fail conditions (timer / hazard_contact /
  lives / move_budget) × structure (single / levels / rounds) ×
  interactions — derived deterministically from the scope with
  the derivation recorded in `game_config.json` and the
  generation report. The three archetypes are **presets**: named
  compositions, chosen by the historical detection, an
  `archetype:`/`preset:` scope note, or a `--archetype` flag.
  The **world worker** (`world.py`) produces the world's data —
  per-unit layouts written as `game/world/world.json` — and
  gates it before anything downstream runs (platformer
  reachability invariants checked on the data, crate solvability
  through the solver, arena bounds). The **mechanics worker**
  (`mechanics.py`) writes the player/game-logic scripts, which
  interpret the world data at runtime (the generated games load
  `world/world.json`; layouts are not baked into code), and
  gates on every entity kind in the world having a handler in
  its rule set. The **assembly worker** wires shell + mechanics
  + world + assets into the runnable game, writes
  `game_config.json`, runs the Godot validation suite, and
  commits + tags `known-good/build`. A scope whose core loop no
  composition can express takes the **bespoke path**: with a
  brain configured, the frontier/mid tier writes one GDScript
  module implementing `docs/MODULE_CONTRACT.md`
  (`setup(game, config)`, `bridge_state()` exposing objective
  positions for the testers, win/lose signalled through the
  shell host), assembled and tested like any other mechanics;
  without a brain the orchestrator stops at the plan with a
  plain explanation. The bespoke matcher is conservative —
  bespoke needs positive evidence (a negotiation/dialogue/
  detective-style core loop) and no primitive signal at all — so
  ordinary scopes keep composing. `weaverbird build game`
  remains, running the three workers in order.
- Future: more composition primitives, richer bespoke modules,
  static checks beyond the gates. It plugs into: pipeline
  state, vcs, ledger, brain.

### 5. Headless testing
- Inputs: the built project.
- Outputs: a compact failure digest or a pass — scenarios (start,
  play, pause, die, restart, win), crashes and performance, produced
  by running Godot without a display.
- Built in v0.1: Godot location and version detection; the stage's
  place in the pipeline.
- Built in v0.4: the runner (`testing.py`, `weaverbird test run
  <dir>`, and the pipeline's headless_test handler). Base runs —
  import, menu scene, game scene — require zero Godot ERROR lines,
  then the game's own scripted players (stage 6) run; performance
  (average FPS and scene load time) is sampled during the competent
  win run against a target that defaults to 30 FPS and can be set
  under `testing.target_fps` in the global settings. Everything
  lands in `.weaverbird/test_report.json`, the report the finish
  check reads with precedence over the skeleton report. The stage
  fails when any base run or a win/lose proof fails.
- Built in v0.5: bounded self-repair (`repair.py`, `weaverbird
  repair run <dir>`, and automatically at the end of a failed test
  run when the global setting `repair.max_attempts` > 0, default
  3). Failures are classified (crash_or_error, win_not_proven,
  lose_not_proven, performance_low, breaker_stuck, missing_assets)
  and each attempt applies one matching strategy from a fixed menu
  — config nudges clamped to the generator's bounds, asset or game
  regeneration for the known crash classes — behind a git
  checkpoint. A change is kept only when the re-run's pass count
  strictly improves or its failure class is resolved; otherwise
  the checkpoint is restored and another strategy tried. Success
  tags `known-good/repair`; exhaustion restores the best
  checkpoint and writes REPAIR_REPORT.md escalating to the user.
  The project is never left worse than it started. A configured
  brain may choose among the offered strategies; it never invents
  new edit types.
- Future: longer scenario matrices, per-level coverage reports.
  It plugs into: godot module, pipeline state, ledger.

### 6. AI playtesting ("real" testing)
- Inputs: a build that passed headless testing.
- Outputs: structured reports from three different players — a
  breaker looking for soft-locks and exploits, a competent player
  measuring balance, and a first-time player following only what the
  game teaches — feeding the iterate loop back to stages 3–5.
- Built in v0.1: the stage's place in the pipeline and the iteration
  budgets in global settings (playtest runs per iteration).
- Built in v0.4: the three players as deterministic scripted bots,
  shipped inside every generated game under `game/tests/` and run
  by the test stage (`weaverbird test run`). The competent player
  plays to a win and separately to a loss through the game's real
  input and physics — steering in the arena and platformer,
  replaying the factory-verified solutions as real key presses in
  the crate game — recording time and deaths. The breaker fuzzes
  inputs from a seed, spams pause, and in the crate game drives a
  crate into a characterised corner so the game's deadlock
  detection fires (it reports the corner to the player instead of
  soft-locking); breaker findings are warnings unless a run
  crashes or logs engine errors. The first-time player presses the
  menu's real Play button and follows only the one-line objective
  hint in the game scene, recording time-to-first-objective and
  whether it finished — its results are observations, not gates.
  Honest limit: these are scripted bots, not model agents; they
  prove the game works end to end, they do not judge whether it is
  fun.
- Built in v0.5: the model-driven playtester (`modelplayer.py`,
  `weaverbird playtest model <dir>`). Every generated game ships
  a control bridge (`game/tests/model_bridge.gd`): launched with
  `--model-bridge`, the game writes its honestly exposed state
  (scene, counts, lives, timer, positions — for the arena game,
  target and hazard positions and hazard patrols) plus a viewport
  screenshot every 0.75s tick, pauses, and applies the driver's
  chosen action as real input. The driver sends the screenshot
  and state to a vision-capable tier of the user's own brain
  (BYOK, ledgered under stage "playtest") and validates the
  strict-JSON reply against the scene's action set. Runs stop at
  the win/lose screens, the decision cap (global setting
  `playtest.model_max_decisions`, default 40), or three
  consecutive invalid decisions, and land in
  `.weaverbird/model_playtest.json`, folded into the test report's
  playtesters as "model". Confusions the model flags — and stuck
  loops the driver detects — are filed as feedback items (stage
  10), by design. Without a usable vision tier the command says
  exactly what is missing and touches nothing. Honest split: the
  scripted players are the deterministic gate; the model player
  is judgement and exploration on top, and in this environment it
  is verified with a scripted policy driving the real bridge —
  no live model call has been made with a real key.
- Future: judgement reports (clarity, difficulty, feel) as a
  scored rubric feeding the iterate loop. It plugs into:
  pipeline state, ledger (every run is costed), global budgets,
  brain.

### 7. Finished gate
- Inputs: the definition-of-done checklist.
- Outputs: a game that may be called finished — all scenario tests
  pass, no crashes across repeated full playthroughs, performance
  targets met, menus/save/settings complete, the game winnable and
  losable, the scope's content counts delivered, and the polish gate
  (proper icon, sensible filenames, clean first run) passed.
- Built in v0.1: the checklist lives in pipeline state, and the
  pipeline refuses to enter or leave the finished stage while any
  item is untrue. Iteration and token caps come from global settings.
- Built in v0.3: the evidence-based finish check (`finish.py`).
  `weaverbird finish check <dir>` inspects the real evidence and
  marks the items it can back — scenario tests and crashes from the
  skeleton validation runs, menus/save/settings from the generated
  game's scripts, the win half of winnable-and-losable from the flow
  smoke — each mark stored with who marked it, when, and the
  evidence note. Items it cannot evidence stay open with a plain
  reason (the lose path, performance, content counts and the polish
  gate have no honest evidence yet). A test report at
  `.weaverbird/test_report.json`, when present, takes precedence over
  the skeleton report — that is the file the test and playtest stages
  will write.
- Future: richer evidence as the test and playtest stages land
  (lose-path runs, measured performance, content counted against the
  scope, a real icon from asset generation), so more of the checklist
  can be marked by evidence instead of staying open.

### 8. Approval
- Inputs: the finished game, its price and store copy drafts.
- Outputs: the user's approval of the game, the price, the platforms
  and the store copy — taste approval first, publishing second.
- Built in v0.1: the stage's place in the pipeline; scope approval is
  the working model for how approvals are recorded (a named person, a
  date, written into the record).
- Built in v0.3: the document the approval stage consumes.
  `weaverbird finish report <dir>` runs the finish check and writes
  `FINISH_REPORT.md`: the game's title and pitch, an opening summary
  (written by the frontier model from the structured facts when one
  is configured — clearly labelled as the only model-written part —
  otherwise a template paragraph from the same facts), stage history,
  what was built, the definition-of-done table with its evidence, the
  test summary, the full cost ledger by stage and tier, the
  provenance summary with the store disclosure draft, and the
  decisions left for the human (open items, price, platforms,
  storefronts, store copy, playing the release candidate). The app's
  project view shows the same checklist and evidence.
- Built in v0.4: the remaining evidence paths. With the test
  report present, the finish check marks winnable_and_losable from
  the competent player's win and lose runs, performance from the
  measured FPS against the target, content_counts_delivered by
  comparing the generation report's units against the scope's
  number, and the icon_and_polish gate from the assets report's
  generated icon plus the test report's clean runs — so a fully
  built and tested game can mark all seven items, each with its
  evidence. The finish report's "What was built" section now
  describes the generated game, its assets and its test verdict,
  and the app's project view shows the same in the browser: the
  archetype and delivery lines, Generate assets / Build game /
  Run tests buttons under the CLI's rules, and a test summary
  card.
- Future: the approval view itself (captured playthrough, drafts,
  one place to say yes or send it back), shipped in the app.

### 9. Publishing through connectors
- Inputs: the approved game, per-store permission from the user.
- Outputs: the game live on its stores — itch first, then Steam,
  Google Play and others — with store art, ratings and the AI-content
  disclosure built from the provenance log.
- Built in v0.1: the connector registry and its rules. Every connector
  declares what it can do alone, what needs approval, and what only
  the user can do (accounts, fees, legal agreements). Unknown actions
  fail closed to needing approval. The GitHub connector check reads
  the logged-in account name without ever exposing a token.
- Future: the store connectors themselves (butler for itch,
  SteamPipe for Steam, Play uploads, Apple signing/notarisation),
  each used through the registry's capability checks. They plug into:
  connectors, provenance (disclosure), pipeline state, ledger.

### 10. Live: feedback triage and patching
- Inputs: player comments and reviews (through the feedback
  connectors), crash reports where the user allowed them.
- Outputs: feedback clustered and ranked (bug, balance, confusion,
  request), patches that flow back through build, headless test and
  playtest before shipping, and drafted replies that are posted only
  with approval. Every patch must pass a save-compatibility check.
- Built in v0.1: the stage's place in the pipeline, and the feedback
  connector's declared capabilities in the registry.
- Built in v0.5: the feedback store and patch loop (`feedback.py`,
  `patch.py`; `weaverbird feedback fetch|add|list|triage|approve|
  dismiss`, `weaverbird patch start`, and the app's Feedback
  view). Fetch sources: GitHub Issues of the project's linked
  repo through the `gh` CLI; itch.io comments when ITCH_API_KEY
  is set and the project records an itch page — best-effort by
  design, a failed source is recorded in the fetch report and
  never crashes the run; and a manual inbox
  (`feedback-inbox/*.txt|*.md`). Fetches dedupe by source and
  content hash. Triage is a deterministic keyword classifier and
  clusterer, ranked by mentions × severity; a configured cheap
  brain tier may refine categories and clusters under strict-JSON
  validation, and any failure keeps the deterministic result.
  Only approved items patch, and only in-scope categories:
  balance (bounded config tuning, clamped to the generator's
  bounds), confusion (the objective hint, length-capped), and
  bugs that match a known remedy class; anything else produces
  PATCH_PLAN.md and stops, and requests/scope/price items always
  need the user's decision. Every applied patch must pass the
  full test stage AND a save-compatibility check (a save captured
  from the pre-patch build through the game's own save system
  must load in the patched build with its stats intact, proven
  by a savecheck scene shipped in the game). Passing bumps the
  game version (game/VERSION, semver patch), appends CHANGELOG.md,
  commits and tags `known-good/patch-<version>`; failing either
  gate restores the pre-patch checkpoint exactly.
- Future: reply drafting and posting through the feedback
  connectors. They plug into: connectors, pipeline state, ledger,
  vcs (each patch is a tagged version).

## Jobs and the runner model

Built in 0.6.0 (`src/weaverbird/jobs.py`). Stages like asset
generation, builds and playtests are long-running, so every app
action is a job:

- A job is one action run for one project: {id, workspace,
  project, action, worker, label, status, created/started/
  finished, events, result, error}, persisted as JSON under
  `<workspace>/.weaverbird-app/jobs/<id>.json` the moment it
  exists and at every step. History survives restarts; a job a
  dead server left mid-flight is rewritten `interrupted` on the
  next start — marked, never silently resumed. `worker` names
  the step that ran the action (skeleton, assets, build,
  testing, finish, repair, patch, feedback, balance,
  model-playtest); since 0.7.0 the orchestrator's plan view
  keys off that field, exactly as planned.
- Execution is a small thread pool (max 2 concurrent). Jobs for
  the same project serialise behind a per-project lock — two
  Godot stages on one project would corrupt each other — while
  jobs for different projects run side by side.
- Progress is steps, not log spam: the stage entry points take
  an optional `reporter` callable and report at step boundaries
  (per validation run, per playtester, per repair attempt, per
  patch gate). The app polls `GET /api/jobs/<id>` about every
  1.5 seconds and updates the live job card in place; the CLI
  reads the same records (`weaverbird jobs <workspace>`,
  `weaverbird jobs show <workspace> <id>`). Library and CLI
  behaviour without a reporter is unchanged.
- Cancellation is cooperative: a cancel flag is checked at
  every step boundary, so a cancel lands between steps — never
  mid-Godot-run — and the record says so ("cancelled between
  steps"). A cancelled or failed job never marks its stage
  complete; the stage functions keep owning their state writes.
- Jobs are costed as they run. Every model call inside a job
  writes to the project ledger, and the budget guard stops and
  escalates when the global settings say so.
- Checkpoints come from the stages themselves: pipeline state
  on disk, a git commit per stage, tags on known-good states.
- Since 0.7.0 the runner also carries the orchestrator: the
  `orchestrate` action runs a whole plan as one job (its events
  are the plan's events), and `run-worker` runs a single worker
  — the job record's `worker` field then names that worker
  itself.

## Workers and the orchestrator

Built in 0.7.0 (`src/weaverbird/workers.py`,
`src/weaverbird/orchestrator.py`, `src/weaverbird/world.py`,
`src/weaverbird/mechanics.py`). The user's directive for this
pass: every different step of making a game is an independent
worker — asset creation independent from world creation
independent from player/mechanics management — coordinated by
an orchestrator, with no step left as a monolith or a special
case.

### The worker contract

A worker is a registry entry (code, like the asset generator
registry) with:

- `name`, `title`;
- `inputs` — the artifact paths it reads (relative to the
  project), plus its relevant config slice (per-worker scope
  fields, hashed with the inputs);
- `outputs` — the artifact paths it writes;
- `depends_on` — the workers that must have run before it;
- `run(project_dir, ctx) -> WorkerResult` — an adapter that
  calls the same library entry point the CLI and the app call
  (adapters never fork logic) and translates the module's
  report into {ok, artifacts, metrics, issues, summary}.
  Issues are {kind, detail, owner}: the owner is the worker a
  failure routes back to.

`ctx` carries the reporter, the settings path, the inputs
hash, the attempt number, and — on a retry — the issue digest
of the previous attempt.

Every run writes `.weaverbird/workers/<name>.json`:
{worker, ran_at, inputs_hash, ok, artifacts, metrics, issues,
summary, job_id?}. The inputs hash is a hash of the declared
input files' contents plus the config slice; it is what makes
skipping sound.

| Worker | Owns | Inputs (declared) | Outputs |
| --- | --- | --- | --- |
| scope | the scope gate: complete + approved | the scope file (completeness slice) | SCOPE.md |
| skeleton | the grey-box shell | scope title/pitch/session/counts slice | game/project.godot, skeleton report |
| world | the world's data + its gate | project.godot, scope design slice | game/world/world.json |
| mechanics | player + game logic, rule coverage gate | world.json, scope verb slice | game/scripts/game.gd (+ the game's scripts/tests) |
| assets | sprites, icon, audio from composition + world requests | project.godot, world.json, palette slice | game/assets/, assets report, icon |
| assembly | wiring + Godot validation + known-good tag | world.json, game scripts, assets report, palette slice | game_config.json, generation report |
| testing | the headless suite + scripted players | game config, world, scripts, assets report | test report |
| model-playtest | the model-driven playtester | test report | model playtest record |
| balance | per-unit analysis (applying stays with patch) | test report, game config | balance report |
| repair | bounded self-repair | test report | REPAIR_REPORT.md |
| feedback | fetch + triage | feedback inbox | feedback store |
| patch | the gated patch loop | feedback store | game/VERSION, CHANGELOG.md |
| finish | finish check + report | test/generation/assets reports | FINISH_REPORT.md |

`weaverbird workers list` prints the registry.

### The orchestrator

The orchestrator owns no creative logic and writes no game
files. `plan(project_dir)` builds `.weaverbird/plan.json`: the
ordered DAG from the project's current state to a target
(default `finished` — scope → skeleton → world → mechanics →
assets → assembly → testing → finish; model-playtest optional;
feedback/patch/balance/repair appear as reactive plans when
invoked), each step with its reason, expected inputs/outputs,
status, and whether it may skip.

`run(...)` executes the plan — through the job runner from the
app (`weaverbird orchestrate <dir> [--to STAGE] [--force]`
synchronously from the CLI):

- **Skip semantics.** A step whose run record matches the
  current inputs hash and says ok is recorded as *skipped
  (inputs unchanged)* and does not run. Touching an input
  un-skips exactly the workers that declare it; dependents
  follow because their own inputs changed as a consequence.
  Non-deterministic or externally-fed workers (feedback,
  patch, repair, model-playtest) declare `can_skip=False` and
  always run when planned.
- **Failure routing.** A gate failure's issues go to their
  owning worker for ONE bounded retry, with the issue digest
  in its context. The world worker genuinely self-corrects —
  a failing platformer level is regenerated with a different
  seed; other workers re-run once with the digest recorded,
  and no worker retries more than once per plan run. Still
  failing → the plan stops and the plan record escalates with
  the owner and the issues.
- **Known-blocked steps never start.** Bespoke mechanics
  without a usable brain is the standing case: the plan stops
  at the plan, with the plain explanation, instead of burning
  a run.
- **State stays truthful.** As steps complete the orchestrator
  advances the pipeline state (never backwards, and `finished`
  only when the definition-of-done checklist is actually
  true), so the app's stepper reflects worker reality.
  Pipeline advance delegates to the same worker adapters, so
  CLI `pipeline advance`, the app's actions and the
  orchestrator can never diverge.
- **Budgets** are the brain's own caps (global settings);
  brain-using workers (bespoke mechanics, model playtest,
  triage refinement) spend through them exactly as before.
- The app's project view shows the plan as a Plan card: the
  DAG as a vertical worker list with per-worker status (done /
  skipped / failed / pending, the last-run summary, and the
  inputs-unchanged note), a Run plan button (a job), and a
  per-worker Run button (a job).

## The balance lab

Built in 0.6.0 (`src/weaverbird/balance.py`), on top of stage 5
/ 6 evidence. The competent and first-time scripted players
record per-unit stats as they play (per level, per round, per
puzzle: completed, deaths, time, and moves against budget
where the game counts moves); the test stage aggregates them
into `test_report.json` under `units`. The lab reads only that
data — a unit table, never the game's kind — and shows a
metric a game does not record as unavailable rather than
guessing it from the archetype.

- Deterministic flags, each carrying its numbers: difficulty
  spike (deaths at least twice the median of completed units,
  minimum 2), wall (a unit the competent player could not
  complete), first-time stall (over twice the median time, or
  3+ deaths, on one unit for the first-time player), breeze
  (every unit completed with no deaths — the game may be too
  easy).
- Suggestions reuse only the patch loop's bounded knobs and
  bounds: spike/wall → ease, breeze → harden, stall → an
  objective-hint clarification through the confusion path.
  Applying one files a synthetic feedback record (source
  `balance-lab`, the rationale as its text) and routes it
  through the normal patch gates — test stage plus
  save-compatibility, version bump, changelog entry citing the
  lab numbers. No side doors.
- Compare is a checkpointed A/B: baseline from the existing
  test report when fresh (else a fresh test run), candidate
  config applied, test stage again, then the original config
  and checkpoint restored exactly; `.weaverbird/balance_report.json`
  holds the side-by-side metrics and a plain verdict. It costs
  up to two full test-stage runs — that is stated wherever it
  is offered.

## The asset studio

Built in 0.6.0, on top of stage 3. Asset generation is a
registry of named generator functions in `assets_gen.py` — one
per sprite, the icon, one synthesiser per audio file — and the
studio is a view over that registry plus the assets manifest
and the provenance log:

- `weaverbird assets list <dir>` (and the app's Assets view)
  shows every generated asset with its provenance and size,
  and flags files that are missing. Assets the manifest or the
  provenance log names but no current generator produces are
  still listed, marked not regenerable — nothing a game ships
  is hidden.
- Regenerating one asset redraws exactly that file from the
  current palette (audio is re-synthesised), records the
  regeneration in the provenance log, and costs zero tokens.
- The palette editor writes the scope Palette field's hexes
  (accent, target, hazard, and the derived roles) back into
  SCOPE.md through the scope writer, shows a before/after
  swatch strip, and is honest about the follow-up: regenerate
  the assets to redraw them, and rebuild the game to pick up
  palette-driven template colours.
- The gallery serves asset files through a read-only route
  confined to the project's `game/assets` directory plus the
  game icon; anything else is a 404.

## Events and logs

- Pipeline history: every stage advance is recorded in the
  pipeline state with a timestamp. Job events (started, step
  progress, gate results, finished or failed) live on the job
  records described above, joined with the ledger for cost.
- Rule for all logs: digests, not dumps. Raw tool output is
  summarised before it reaches a model or a user, because raw
  logs burn tokens.

## Multi-project workspace model

- One global settings file serves the whole factory: budgets, the
  model ladder, the pinned Godot version, connector switches.
- Each game is its own project directory with its own scope file,
  pipeline state, ledger and provenance log, and its own git history.
  Projects never share state, so many games can be at different stages
  at once — one being scoped, one in playtest, one live and patching.
- A workspace is simply a folder of such projects plus the global
  settings. A future platform view lists the projects and their
  stages by reading each project's state file — no central database
  is needed for the local platform.

## Open-source core and the paid layer

- Open source, for everyone: the executable and everything in it —
  the core library, the pipeline, the scope system, budgets and
  ledger, provenance, the connector registry and local connectors,
  and the future generation, test and playtest modules built for the
  local platform. Games made with Weaverbird belong to their makers:
  no royalty, no claim on the output, no gating of exports.
- The paid layer, later and only if the platform earns it, is hosted
  convenience: running jobs on hosted machines instead of your own,
  ready-made downloads, extra archetype packs, team features. It is
  sold as a service around the same core, never by locking parts of
  the core away.
- What stays external by nature, in both layers: the user's Godot
  install, git and store accounts, their credentials (kept in a vault,
  never in files the factory writes), and model providers reached
  with the user's own keys.
