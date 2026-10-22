# Game Factory — Full Pipeline Design (v0.3, 2026-10-22 — user revisions applied)

User's outline (2026-10-22): on top of Godot to start with. Prompt is the first input → factory creates the game from scratch (assets 2D/3D, sounds, menus, etc.) → factory asks questions if unsure → build/test loop: headless testing, then real play testing by the AI → iterate until finished → publish to storefronts with the user's permission → listen to player feedback and keep iterating/polishing. Everything run optimally to avoid burning tokens.

This document keeps that outline intact and adds the pieces it was missing. Those pieces are marked [MISSING] — without them the loop either never finishes, burns tokens, or cannot legally publish.

## Pipeline

### 0. Intake & scope interview — the scope file is built live, and the build cannot start until it is complete (user revision, 2026-10-22)
- User prompt arrives in plain language.
- The factory immediately creates the scope file (`SCOPE.md` — template in `templates/SCOPE.md`) and starts filling it from the prompt itself. Everything already stated in the prompt is extracted, never re-asked.
- Then the interview loop runs: **ask → update the scope file → show what is still missing → ask again, until every field marked REQUIRED is filled.** Questions are grouped per round (not a drip), each round shows the current scope file so the user can see it filling in and correct anything in place. A field can be filled by an explicit answer, by a user-approved factory suggestion, or by a shown sample (for style) — but a REQUIRED field left blank blocks the build. No silent defaults on required fields.
- Scope fields include: dimension (2D first in v1, 3D later — they are different asset/animation pipelines), genre/archetype, core verb, win/lose condition, session length, scope class (Micro: jam-size / Small: 30–90 min game / Medium: later), feature list in/out, content counts, difficulty target, platforms + target storefronts, monetisation/price intent, **style lock (below) and asset provenance policy (below)**.
- Out-of-scope requests are caught in this phase, not mid-build: multiplayer/backend, licensed IP (e.g. "make a Mario game"), MMO-scale content, gambling mechanics. The factory offers the nearest in-scope version in the scope file instead of arguing.
- Output: the completed, frozen scope file. The user approves it once when the last REQUIRED field lands. Every later decision is checked against it; anything outside it needs a scope amendment (a small second interview, appended to the file — never a quiet drift). This is the single biggest token saver: no re-litigating direction mid-build.
- Note: the token/cost cap is NOT a scope-file field (user revision) — it lives in global settings (section below) and applies across games.

### 1. Design skeleton (cheap tokens, mostly deterministic)
- Archetype template selected from the scope file (code, menus, save, settings, pause, input are template code — zero LLM tokens to create, only to customise by diff).
- Grey-box playable first: placeholder shapes, full loop, win/lose, menus wired. No real assets yet.

### 2. Asset factory — style lock + provenance are decided in the scope phase (user revision, 2026-10-22)
- Style lock is a REQUIRED scope-phase decision: during the interview the factory shows 2–3 style samples, the user picks/edits one, and the resulting style bible (palette, proportions, outline/shading rules, audio mood) is written into the scope file. Every asset is then generated against it, so assets cohere instead of each looking like a different game. No asset work starts before this field is filled.
- Provenance policy is also a REQUIRED scope-phase decision: which asset sources are allowed for this game (code-drawn only / AI generation allowed / licensed library allowed), and the disclosure stance for stores. The user decides it once, up front.
- Provenance log (automatic, not asked): every asset then records how it was actually made (code / model + prompt / library / licence), following the scope-phase policy. Stores require this: Steam requires a content survey including AI-generated content disclosure, and unclear disclosure can delay approval. The log also protects the maker if an asset is challenged.
- 2D: sprite/tile/UI generation, procedural/code-drawn where possible (cheapest, most coherent), AI image generation only where code cannot do it; animation via sprite sheets / skeletal where the archetype supports it.
- 3D (post-v1): model generation + auto-rig + animation clips is the hardest, least reliable station — expect a constrained low-poly, modular kit approach first, not free-form 3D.
- Sound: procedural synth SFX by default (code, free, coherent), generated music loops from a small licensed/local set; voice acting out of v1 scope.

### 3. Build loop
- Factory writes Godot project by composing templates + generating only the novel code as diffs against the template, never regenerating whole files.
- Rollback via the version-control connector (user revision, 2026-10-22): local git always; a GitHub connector (see Connectors) creates/mirrors the repo when connected. Commit per iteration, tag every state that passes headless tests as a known-good checkpoint, branch for risky experiments. Rollback = return to the last known-good tag — the factory never paints itself into a corner, and a failed iteration costs a checkout, not a rebuild.
- Static checks first (cheapest): project parses, scenes load, no missing resources, export presets valid.

### 4. Headless test loop
- Headless Godot run: main scene boots, simulated inputs play the loop, scripted scenario suite (start → play → pause → die → restart → win), crash/error capture, performance snapshot (fps, memory, load time).
- Content verifiers: generated levels/waves must be completed by a simulated competent player; economy/difficulty curves checked numerically.
- Failures return as a compact failure digest (error + scene + last actions), not raw logs — raw logs are a token fire.

### 5. Real play test (AI actually playing) [MISSING: what "playing" means concretely]
Three distinct players, because one agent doing everything conflates bugs with balance:
- Breaker: tries to soft-lock, clip out of bounds, spam inputs, idle forever, alt-tab/pause abuse.
- Competent player: plays to win at target skill; records time-to-complete, deaths, stuck points — the balance signal.
- First-time player: no spec knowledge, follows only on-screen teaching; records where it got confused — the onboarding/tutorial signal. Uses screenshots/video frames + game state together (vision is expensive, so frames are sampled at decisions, not streamed).
- Each run produces a structured report against the scope file's criteria, not free-text impressions.

### 6. Iterate until "finished" [MISSING: definition of done + stopping rule]
Finished is a checklist, not a vibe:
- All scenario tests pass, zero crash in N consecutive full playthroughs, performance targets met on min-spec target.
- Menus/save/settings/pause/quit complete; game is winnable and losable; content counts in the scope file delivered.
- Buyer-polish gate: proper game icon, sensible filenames, clean first-run experience with no dev placeholders.
- Stopping rules: iteration ceiling and token/cost caps come from **global settings** (user revision — not from the scope file, not per-game negotiation). If the loop cannot converge inside those global budgets, it escalates to the user with the specific blocker — it does not burn tokens forever.

### 7. Human approval gate (one, concentrated)
User plays the release candidate (or watches a captured playthrough) and approves: game, price, platforms, store copy. This is separate from the publish permission — taste approval first, transaction second.

### 8. Publishing — handled by storefront connectors (user revision, 2026-10-22)
Each storefront is a connector (see Connectors): it declares how it authenticates (vault/OAuth, never credentials in chat), what it can do (draft page, upload build, submit for review, patch, read reviews), and which steps are user-only (account creation, fees, legal agreements, final publish permission per store). Factory prepares everything through the connector; the user supplies the identity/legal steps and gives per-store permission. Per store:
- itch.io (v1 first): free to publish, upload via butler CLI (delta uploads for patches). Simplest real storefront — the right first target.
- Steam: $100 Steam Direct fee per game (recouped after $1,000 gross), tax/bank identity interview, store page must be public as Coming Soon for at least 2 weeks before release, capsule art + screenshots + content survey with AI disclosure, build + store review (days). So "publish today" is impossible on Steam — the factory must start the clock at spec stage if Steam is a target.
- Google Play: $25 developer account, AAB format (not APK), signed release keystore (losing it means the app can never be updated under the same identity — factory must vault it and warn), IARC content-rating questionnaire is mandatory, closed-test requirements for new personal accounts.
- macOS anywhere: build must be signed + notarised with an Apple Developer ID ($99/yr) or Gatekeeper blocks downloaded copies; iOS needs a Mac + Xcode, period.
- Everywhere: capsule/store art in each store's exact sizes, age ratings, privacy policy if the game collects any data (keep v1 games telemetry-off by default — then no policy is needed), pricing/regional pricing choices are the user's call, presented with a recommendation.
Factory does: builds, signing (with user's credentials in a vault, never in chat/logs), store-page draft in plain copy, upload, submission checklist. Factory never does: creating accounts in the user's name, paying fees without explicit approval, agreeing legal terms for the user.

### 9. Live loop — feedback → polish [MISSING: sources, triage, save-safety]
- Sources (via connectors): itch comments/reviews, Steam reviews, crash reports (opt-in only), and the game's own optional anonymous stats only if the user approved telemetry in the scope file.
- Triage, not auto-fix (agreed, user 2026-10-22): feedback is clustered (bug / balance / confusion / request), ranked by frequency × severity. Bugs and balance fixes inside scope-file scope can flow through stages 3–6 automatically and ship as a patch with the user's one-tap approval. Feature requests and anything changing scope, price or saves goes to the user as a decision.
- Save compatibility is a hard test in stage 4 for every patch — a polish update that wipes players' saves is the classic factory-own-goal.
- Replies to players are drafted by the factory under the game's/store's account (never a personal account), posted after user approval until the user grants a standing rule.
- Cadence + sunset rule: check feedback on a schedule, batch patches (not a release per comment), and define when a game is done being polished (e.g. 2 quiet cycles) so the factory's attention moves on.

## Token economy (the operating system of the whole factory)
Principle: tokens are spent only on judgement and novelty. Everything repetitive is code.
1. Templates & generators first — menus, save, standard enemies, level layouts come from deterministic code. LLM writes only the delta that makes this game different.
2. Model ladder — small/cheap model (or local) for classification, log digestion, test-report formatting; mid model for routine code edits; frontier model only for design decisions, hard debugging, and play-test judgement. No frontier model reading raw logs.
3. Compact state, not conversation — the factory carries a small structured project state (scope file + current failures + decisions log) into each call instead of replaying history. Diffs in, digests out.
4. Cache aggressively — asset prompts + outputs, template renders, test results keyed by file hash: unchanged systems are never re-tested or re-described.
5. Grouped questions & approvals — scope questions grouped per interview round (rounds repeat only until all REQUIRED fields are filled), then one approval gate and one publish permission per store — not a drip of interruptions during the build itself.
6. Hard budgets from global settings — token/cost caps live in global settings (below), not in the scope file. Every run shows estimated cost before starting (tokens + asset APIs + store fees) and stops at the global cap. A per-game cost ledger is still part of the run report — it reports against the global budget, it does not set its own.
7. Vision sparingly — screenshots at decision points and on failure, downscaled; never continuous video into a model.

## Global settings (user revision, 2026-10-22)
One settings file for the whole factory (`templates/global-settings.yaml` is the draft), set once by the user, applying to every game:
- Budgets: token cap per run, token cap per game, monthly token/cost ceiling, iteration ceiling per build loop, play-test runs per iteration.
- Model ladder defaults: which model/provider for cheap, mid and frontier work (via model connectors); allow local models on/off.
- Defaults that scope interviews start from: Godot version (pinned), default platforms, default stores, telemetry off, reply-approval required.
- Connector credentials are referenced here by name only — secrets live in a vault, never in this file.
Changing a global cap mid-game is a user action in settings, never something the factory does to itself to keep a loop alive.

## Connectors (user revision, 2026-10-22)
The factory's core stays small; everything external is a connector with a declared contract (draft registry in `templates/connectors.yaml`): what it does, how it authenticates, what it may do without asking, and what always needs user approval.
- Version control: GitHub (repo create/mirror, commits, tags, rollback, releases) — local git works with no connector; GitHub adds backup, sharing and release hosting.
- Storefronts: itch.io (butler), Steam (Steamworks/SteamPipe), Google Play, Epic, Apple (signing/notarisation) — draft, upload, submit, patch, read reviews; publish action itself always needs per-store user permission.
- Models & assets: LLM providers (frontier/cloud + local, e.g. Ollama), image/3D/audio generation providers — bring-your-own-key; the factory records provenance from whichever connector produced an asset.
- Feedback: itch comments, Steam reviews, crash reporting — read-only by default; posting a reply is a separate, approval-gated capability.
- Signing & services: code-signing/notarisation, keystore vault.
Connector rules: a connector never widens its own permissions; a missing connector degrades gracefully (no GitHub → local git only; no Steam connector → factory prepares the package + checklist for manual upload) instead of blocking the whole factory.

## Other missing pieces the outline didn't mention
- Discovery/marketing after publish: feedback cannot arrive from players who never find the game. Minimum v1 answer: store-page quality (capsules, tags, description) + a launch devlog — full marketing is out of factory v1 scope but "publish and listen" silently assumes it.
- Legal/IP screen at intake: prompt and generated assets checked for trademarked characters/franchises and near-copy risk before build, not after store rejection.
- Failure & refusal paths: what the factory says/does when a game cannot be made well inside scope (too big, needs multiplayer, needs licensed content) — offer the nearest in-scope version instead of a bad build.
- Factory's own updates: Godot version pinning per game; a game in production never silently inherits a new engine version.
- Who owns what, restated for users of the tool: their prompt, game, assets and revenue are theirs; factory code open source; no claim on output.

## v1 scope recommendation (revised)
2D only, itch.io as the first storefront, one archetype proven end-to-end prompt→published→patched, with Steam started in parallel only as a Coming Soon clock. 3D, Play and full autonomy on replies come after v1 proves the loop converges inside a sane token budget.
