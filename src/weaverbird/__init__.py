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
"""

from importlib import resources
from pathlib import Path

__version__ = "0.10.0"

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
