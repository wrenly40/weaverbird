"""Weaverbird — a game factory on top of Godot.

This package is the factory: global settings and budgets, the scope
interview file, the connector registry, the token ledger, the asset
provenance log, the pipeline state machine, local git helpers, Godot
detection, and (since 0.2.0) the skeleton builder, which turns an
approved scope into a playable grey-box Godot project and validates
it by running Godot. Asset generation and the later stages are not
built yet.
"""

from importlib import resources
from pathlib import Path

__version__ = "0.2.0"

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
