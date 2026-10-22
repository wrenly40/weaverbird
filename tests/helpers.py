"""Shared helpers for the Weaverbird test suite (not a test module)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from weaverbird import template_path  # noqa: E402

# Real answers for the hint-valued template fields.
_HINT_ANSWERS = {
    "Dimension": "2D",
    "Scope class": "Micro",
    "Allowed sources": "code-drawn first, AI-generated allowed",
}


def filled_scope_text() -> str:
    """The packaged SCOPE.md template with every REQUIRED field filled."""
    lines = []
    for line in template_path("SCOPE.md").read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("- REQUIRED"):
            rest = stripped[len("- REQUIRED"):].strip()
            name = rest.partition(":")[0].strip()
            lines.append(f"- REQUIRED {name}: {_HINT_ANSWERS.get(name, 'Filled in for the test')}")
        elif stripped.startswith("- Telemetry:"):
            lines.append("- Telemetry: no")
        else:
            lines.append(line)
    return "\n".join(lines) + "\n"


def template_scope_text() -> str:
    return template_path("SCOPE.md").read_text(encoding="utf-8")
