"""Weaverbird — a game factory on top of Godot.

This package is the factory: global settings and budgets, the scope
interview file, the connector registry, the token ledger, the asset
provenance log, the pipeline state machine, local git helpers, Godot
detection, the skeleton builder (0.2.0), and since 0.3.0 the
bring-your-own-key brain, the local app, and the evidence-based
finish check with its report. Since 0.4.0: real game generation
(three archetypes), deterministic asset generation (vector sprites,
a generated icon, synthesised audio), and the headless test stage
with scripted playtesters, all feeding the finish check. Since
0.5.0: the feedback + patch loop (fetch, triage, approved patches
gated by the test stage and a save-compatibility check), bounded
self-repair of failing test runs, and a model-driven playtester
that plays through a bridge in the generated games. Since 0.6.0:
the job runner (every app action a persisted background job with
live progress and cooperative cancel; each record names its
worker for the coming orchestrator), the balance lab (per-unit
metrics from the scripted players, deterministic flags,
suggestions routed through the patch gates, checkpointed A/B
compare), and the asset studio (per-asset regeneration from the
generator registry, palette editor writing back to the scope).
Since 0.7.0: every step is an independent worker behind one
contract (workers.py — scope, skeleton, world, mechanics,
assets, assembly, testing, model-playtest, balance, repair,
feedback, patch, finish), coordinated by an orchestrator
(orchestrator.py) that plans the DAG, skips workers whose inputs
are unchanged, and routes gate failures back to their owner.
Generation is composition-based — movement × objective × fail ×
structure × interactions — with the three archetypes kept as
named presets, and a bespoke path where a brain-written module
behind a documented contract provides mechanics no composition
can express.
Since 0.8.0: the scope interview is a conversation
(interview.py — one session engine behind the app's chat page
and `weaverbird interview`, deterministic without a brain and
model-driven with one, with generated style-sample cards for
the style lock), the composition has a dimension (one 3D
preset, the flat-ground arena_3d, with materials instead of
sprites), and native installers (PyInstaller binaries per
platform, built by build-native.sh and the manual workflow;
`update` prefers the platform's native asset when frozen).
Since 0.11.0: generation is genre-agnostic. No scope is matched
into a named game any more: the world worker derives a game
plan from the scope's own words (plan.py — movement, objective,
fail rules, entities with behaviours, numbers, each with a
derivation note quoting the scope), and one generic runtime
executes any plan from a small primitive vocabulary (movements
top_down_2d / side_view / grid_step / top_down_3d; objectives
collect / reach / survive / deliver; fail rules timer /
hazard_contacts / fall). A scope whose core mechanic the
vocabulary cannot express is marked bespoke_required in the
plan: with a brain, the bespoke-module path takes it; without
one, generation stops at the plan and says so plainly. Projects
built by older versions keep working untouched — their files,
test stage, patch loop and exports are unchanged.
Since 0.12.0: the brain ladder also speaks OpenCode (the
maker's own local server holds the keys; Weaverbird sends
none), projects can be deleted behind a typed confirmation
and guardrails, the 3D test bots measure stuck over time
windows instead of frames (the intermittent win-proof flake's
root cause), and the style lock is obeyed in the assets —
outline, shading, glow and pattern traits drive the drawing,
collectibles vary per instance from the world data, 3D
materials glow and take form, and the audio is shaped
synthesis in the scope's mood.
"""

from importlib import resources
from pathlib import Path

__version__ = "0.13.0"

TEMPLATES_DIR = Path(__file__).parent / "templates"


def template_path(name: str) -> Path:
    """Path of a bundled template (valid when running from a source tree).

    Code that must also work inside the packaged single-file executable
    should use template_text()/template_bytes() instead — inside a
    zipapp there is no real filesystem path for package data.
    """
    return TEMPLATES_DIR / name


def _template_resource(name: str):
    return resources.files(__package__).joinpath("templates", name)


def template_text(name: str) -> str:
    """Text of a bundled template, from a source tree or inside the app."""
    return _template_resource(name).read_text(encoding="utf-8")


def template_bytes(name: str) -> bytes:
    """Bytes of a bundled template, from a source tree or inside the app."""
    return _template_resource(name).read_bytes()
