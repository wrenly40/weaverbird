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
queue. Publishing stays last, on purpose: the store connectors are
still to come.

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
is kept as `weaverbird.bak`. From a source checkout it pulls and
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
with it, and git for versioning. Store accounts, signing keys and
model-provider keys stay yours, in your own vault — those are external
by nature, not missing pieces of the app.

A secondary route, if you prefer it: `python3 -m pip install .` gives
you a `weaverbird` command the same way.

Then start a project:

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

The commands above are the scripting surface — everything they do,
the app does too, over the same core. To see the project in the app
instead, put it in a workspace folder and run Weaverbird there:

```
mkdir games && mv my-game games/
cd games
../dist/weaverbird        # the app opens; my-game is on the dashboard
```

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

See `docs/PLATFORM.md` for the full platform map — what each stage
takes in, what it produces, and what is built now versus later — and
`docs/DESIGN.md` for the full design.

## What works today

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

- The multi-turn scope interview conversation (today the prompt is
  extracted once and the remaining questions are printed; a
  conversational interview with style samples comes next).
- More archetypes beyond the first three, generated game code
  beyond the archetype cores (model-written diffs for what makes a
  game different), and the iterate loop around them.
- The background job runner, so long stages run, checkpoint and
  resume instead of blocking a terminal or a page.
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
