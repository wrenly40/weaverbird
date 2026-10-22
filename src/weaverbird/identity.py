"""The Weaverbird visual identity, as code.

The design tokens and the wordmark live in the packaged assets, so the
CLI today and the future app/UI mode — and any store or page material
the factory generates — all draw from the same source. See
docs/VISUAL_IDENTITY.md for the design system in words.
"""

from __future__ import annotations

import json
from importlib import resources
from pathlib import Path

ASSET_TOKENS = "design_tokens.json"
ASSET_MARK = "weaverbird.svg"


def _asset(name: str):
    return resources.files(__package__).joinpath("assets", name)


def load_tokens() -> dict:
    """The design tokens, loaded from the packaged assets."""
    return json.loads(_asset(ASSET_TOKENS).read_text(encoding="utf-8"))


def mark_svg() -> str:
    """The weaverbird wordmark/icon as SVG text."""
    return _asset(ASSET_MARK).read_text(encoding="utf-8")


def summary_text() -> str:
    """A plain summary of the identity, for the terminal."""
    tokens = load_tokens()
    colors = tokens.get("colors", {})
    lines = [
        "Weaverbird — visual identity",
        "",
        "Light, flat and quiet. One accent colour; everything else is a",
        "neutral surface or text. No gradients, no gloss.",
        "",
        "Colours:",
    ]
    for name, value in colors.items():
        lines.append(f"- {name}: {value}")
    lines += [
        "",
        f"Spacing scale: {', '.join(str(s) for s in tokens.get('spacing', []))}",
        "Radii: " + ", ".join(
            f"{k} {v}" for k, v in tokens.get("radii", {}).items()
        ),
        f"Type: {tokens.get('typography', {}).get('font_family', '')}",
        f"Shadow (soft): {tokens.get('shadows', {}).get('soft', '')}",
        "",
        "Mark: a minimal woven bird, flat, in the accent colour, with the",
        "wordmark set in the same clean sans as everything else.",
        "",
        "Full design system: docs/VISUAL_IDENTITY.md",
    ]
    return "\n".join(lines) + "\n"


def export_assets(target_dir) -> list[Path]:
    """Copy the tokens and the mark into a directory for use elsewhere."""
    target = Path(target_dir)
    target.mkdir(parents=True, exist_ok=True)
    written = []
    for name in (ASSET_TOKENS, ASSET_MARK):
        dest = target / name
        dest.write_bytes(_asset(name).read_bytes())
        written.append(dest)
    return written
