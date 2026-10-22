"""Finding Godot, and reading a Godot project's project.godot file."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

ENV_VAR = "WEAVERBIRD_GODOT"
_FALLBACK_DIR = Path.home() / "workspace" / "tools" / "godot"
_FALLBACK_GLOB = "Godot_v*-stable_linux.x86_64"


def _version_of(path: Path) -> str | None:
    try:
        result = subprocess.run(
            [str(path), "--version"], capture_output=True, text=True, timeout=15
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    text = (result.stdout or result.stderr or "").strip()
    if not text:
        return None
    return text.splitlines()[0].strip()


def find_godot(explicit=None) -> tuple[str | None, str | None]:
    """Locate a Godot binary and return (path, version).

    Order: explicit argument, the WEAVERBIRD_GODOT environment variable,
    `godot` on PATH, then a downloaded binary under
    ~/workspace/tools/godot/. Returns (None, None) when nothing is
    found.
    """
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit))
    env_value = os.environ.get(ENV_VAR)
    if env_value:
        candidates.append(Path(env_value))
    on_path = shutil.which("godot")
    if on_path:
        candidates.append(Path(on_path))
    if _FALLBACK_DIR.exists():
        candidates.extend(sorted(_FALLBACK_DIR.glob(_FALLBACK_GLOB)))

    seen: set[Path] = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate), _version_of(candidate)
    return None, None


_SECTION_RE = re.compile(r"^\[(.+)\]\s*$")
_KV_RE = re.compile(r"^([^=]+)=(.*)$")


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value.startswith('"') and value.endswith('"'):
        return value[1:-1]
    return value


def parse_project_godot(path) -> dict:
    """Parse the small part of project.godot the factory needs.

    Reads config_version and the application name (config/name).
    Accepts the project.godot file itself or its project directory.
    """
    path = Path(path)
    if path.is_dir():
        path = path / "project.godot"
    if not path.exists():
        raise FileNotFoundError(f"project.godot not found: {path}")
    result: dict = {"path": str(path), "config_version": None, "name": None}
    section = ""
    for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if not line or line.startswith(";"):
            continue
        section_match = _SECTION_RE.match(line)
        if section_match:
            section = section_match.group(1).strip()
            continue
        kv = _KV_RE.match(line)
        if not kv:
            continue
        key, value = kv.group(1).strip(), kv.group(2).strip()
        if not section and key == "config_version":
            try:
                result["config_version"] = int(value)
            except ValueError:
                result["config_version"] = value
        elif section == "application" and key == "config/name":
            result["name"] = _unquote(value)
    return result
