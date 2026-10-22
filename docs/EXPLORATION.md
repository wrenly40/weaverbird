# Game Factory — External Tool Exploration
Date: 2026-10-22
Status: Exploration only. Not approved for build. Not a Wrenworks internal tool — user reframed it (2026-10-22) as a standalone external tool for other developers, open source, with a paid option later only if it works as expected.

## The idea in one line
A factory layer on top of an existing open engine (Godot 4), not a new engine and not a prompt-to-game toy:
spec in → scaffolded Godot project from proven archetype templates → generated + verified content → headless playtest / smoke checks → packaged exports + store draft out.
The output is a normal Godot project the user owns outright. No lock-in, no royalty.

## Why this shape (the gap)
Two crowded camps leave a middle gap:

1. Closed prompt-to-game SaaS — Rosebud AI (Indie Dev from $15/mo), Ludo.ai ($15/mo Indie billed annually, $35 Pro, $300 Studio), Astrocade, playmix.ai ($9.99/mo), plus a fast-growing list of clones. Strength: speed for non-coders. Weakness: web-hosted output, platform lock-in, code export often paid/gated, generic templates, no real desktop/Steam/Play release pipeline, no ownership of a maintainable engine project.
2. Open engines — Godot (MIT, $0, no royalties) and GDevelop (MIT engine). Strength: ownership, real projects, community. Weakness: no factory line at all — every new game repeats scaffolding, menus/save/settings, export setup, testing and packaging by hand.

Recent direct neighbours to watch:
- OpenGame (arXiv / GitHub, 2026): open-source agentic framework for end-to-end web game creation, with reusable template + debug skills. Research-grade, web games, not a Godot production/release line.
- Hobby autonomous game factories (Claude Code + VPS loops, ~15 min per small web/Godot game): prove the loop works for small games, authors still note output is "not perfectly verifiable" and a human stays in the loop. Not productised for other devs.
- Dark software factories (StrongDM et al., early 2026): spec → agents write/test/ship, humans neither write nor review code. The transferable lesson for games is verification design (scenarios, simulated players, holdout checks), not lights-out autonomy — fun and feel are not unit-testable.

Dead ends this teardown rules out:
- Building another engine (fights Godot/Unity/Unreal, years, no distribution).
- A generic any-genre prompt generator (direct fight with funded SaaS + OpenGame; quality unverifiable; support burden is infinite).
- Gating code export or commercial use in the free tier (destroys trust in open-source communities; the Unity pricing backlash is the cautionary tale).

## Target user (v1, narrow)
Primary: solo and 2–3 person indie devs already using (or switching to) Godot 4, and game-jam teams.
Their concrete costs today:
- 1–3 days of boilerplate before a new idea is playable (project setup, input, menus, save, audio, export presets).
- Export/release friction every ship: three desktop platforms + web/Android, icons, builds, store pages — error-prone and boring, so projects die at 90% done.
- Content volume done by hand (levels, waves, items) with no automated check that it's beatable/fair.
- Prototypes abandoned because scaffolding cost exceeded the idea's value.

Not v1 users: non-coders wanting one-click games (Rosebud serves them), AAA/3D studios, Unity shops.

## Product definition — stations
1. Spec: a small declarative file (factory.yaml / GAME.md) — archetype, core loop, content counts, platforms, style tokens.
2. Scaffold: archetype templates (start with ONE: see MVP) + shared systems as Godot addons — main menu, pause, settings, save, synth SFX hooks, input map.
3. Generate: content generators paired with verifiers (levels/waves generated to be solvable by construction, then a simulated player must complete them; failures loop back, not to the user).
4. Verify: headless Godot run — project opens, main scene runs clean, simulated input playthrough, screenshot capture per scene, performance sanity. Report is a plain HTML run sheet.
5. Package: export presets for Windows / macOS / Linux / Web (+ Android when SDK present), correct icon, buyer-sensible filenames, itch.io (butler) / store-page draft in plain copy.
AI is a pluggable worker at stations 2–3 (bring-your-own-key or local model), never the product itself and never mandatory. Deterministic templates/generators carry the load — that is what makes output maintainable by its owner.

## Open source + paid later (open-core, in that order)
Licence recommendation: MIT (or Apache-2.0) for core CLI, templates, verifiers and exporters — matches Godot's licence culture and maximises adoption. Trademark the name/logo separately. Games made with it: 100% the maker's, no royalty, ever — say so on page one.

Paid options, ONLY after adoption gates are met (conversion for dev tools is typically ~2–5%, so paid before volume is fantasy):
1. Hosted cloud factory — the GDevelop precedent: free local use forever; pay for cloud runs (no local setup, parallel builds, one-click iOS/Android builds, version history). Indicative $8–15/mo.
   GDevelop today: free MIT engine, paid Silver from $7.99/mo for cloud/AI/publishing conveniences.
2. Ready-made desktop app — the Aseprite precedent: source free to compile, polished signed binary $19.99 one-time. Low pressure, indie-trusted.
3. Pro archetype packs — extra genre templates + commercial-ready systems, one-time $29–49 per pack or a marketplace with revenue share for community template authors.
4. Team tier (much later) — private/shared template libraries, cloud run history, priority support.
Never paid-gate: exporting, commercial use, removing a splash/watermark, or access to the user's own code.

## Route to users (£0, community-led)
- GitHub (repo, good README demo GIF, sample games shipped with the factory as proof) → Godot Asset Library listing.
- Godot community: forum, Discord, r/godot; itch.io devlogs of the sample games, not just the tool.
- Game jams: a jam-ready template + "ship in a weekend" story; offer a factory track / sponsor a small jam.
- YouTube/Twitch Godot creators: give them a timed challenge ("spec to shipped itch page in an hour").
No paid ads at this stage. If the Godot community does not pull it, no channel will save it — that itself is the test.

## MVP scope (if validated)
- CLI + minimal desktop console (same engine, two front ends).
- 1 archetype done properly. Recommendation: top-down arena survivor — jam-popular, template-friendly, content (waves/enemies/items) is generatable + simulatable, scope is bounded. Alternative: 2D platformer (bigger audience, harder feel/verification).
- Spec → project → verify → export (Win/Linux/Web first; Mac/Android packaging best-effort in v1).
- 3 sample games produced by the factory itself, published free on itch as evidence.
Explicitly out: multiplayer, 3D, visual scripting, asset generation, store submission automation, any-genre generation.

## Validation gates before build (competition rule)
1. Fake door (1 week): landing page + a worked spec→game example + GitHub repo README. Gate: 200 waitlist / repo stars or 30 substantive comments from Godot devs. Below that: stop.
2. Concierge (2 weeks): manually run the factory process by hand for 5 external devs' specs. Gate: at least 3 say they would use it for their next real project, and median time-to-playable under 4 hours.
3. Prototype (only if 1+2 pass): build MVP above. Gate for continuing: 20 external games shipped with it in 90 days.
4. Paid tier: only discussed after gate 3, starting with the hosted cloud or Aseprite-style binary — whichever users ask for unprompted.

## Main risks
- Godot version churn breaking templates/exports — mitigations: pin to one Godot LTS-style version per factory release.
- Open-source maintenance load (issues, PRs, platform quirks) becoming the whole job.
- Squeezed middle: Rosebud adds Godot export, or Godot community builds official templates — speed and jam/community niche are the defence.
- Reputation: if early output looks like AI slop, the tool is branded as a slop machine. The sample games must be genuinely decent — the verification + human-feel gate is a brand feature, not just QA.

## Recommendation
Worth exploring, in the narrow form above: Godot factory layer, one archetype, open source, paid only as hosted convenience / binary / packs after adoption. Not worth doing as a generic prompt-to-game platform.
Next decision for the user: approve the fake-door gate, pick the MVP archetype, or park it.
