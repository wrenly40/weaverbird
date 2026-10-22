# Weaverbird

Weaverbird is an open-source game factory that sits on top of Godot.
You start with a prompt. The factory turns it into a scope, asks
questions until the scope is complete, then builds the game, tests it,
has AI players play it, publishes it through store connectors with
your permission, and keeps polishing it from player feedback.

It is a full end-to-end platform, being built in stages. This release
(v0.1) is the infrastructure — the backbone the rest of the platform
plugs into.

## Quickstart

```
git clone <this repository>
cd weaverbird
./build.sh
./dist/weaverbird doctor
```

`./build.sh` produces one self-contained executable, `dist/weaverbird`.
Inside it lives everything the app needs: the core library, the
command-line front end, the scope and settings templates, and the
connector registry. Its one library dependency is bundled in too. The
executable works from any working directory, on any copy of the app.

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
```

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

## What works today (v0.1 infrastructure)

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
  `scope`, `settings`, `connectors`, `ledger`, `provenance`,
  `pipeline`).

## Platform stages still to build

These are stages of the platform on the roadmap, in the order the
pipeline runs — not things left out of scope:

- The scope interview conversation itself (today the questions are
  printed; answering them in conversation is the next module).
- Archetype templates and skeleton building, asset generation,
  generated game code, and the build loop around them.
- The headless test runner and the three AI playtesters (breaker,
  competent player, first-time player).
- The background job runner, so long stages run, checkpoint and
  resume instead of blocking a terminal.
- Store connectors that upload and publish (itch first), signing and
  notarisation, feedback reading and triage, and the patch loop.
- An app mode with a proper interface over the same core.

## Look and feel

Weaverbird has one visual identity across the terminal today and the
app mode later: light, flat and quiet, with a single violet accent and
lots of whitespace. The design system is written down in
`docs/VISUAL_IDENTITY.md`, and its tokens and wordmark ship as code
inside the executable — run `weaverbird identity` for the summary, or
`weaverbird identity --export <dir>` to take the tokens and the mark
elsewhere.

## Architecture

All logic lives in the library, `src/weaverbird/`. The CLI
(`src/weaverbird/cli.py`) is a thin front end: it parses arguments,
calls the library, and prints plain results. A future desktop or web
platform, and a background job runner, will call the same library —
the platform is the core, and front ends come and go.

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
