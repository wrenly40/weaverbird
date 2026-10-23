"""The bundled demo project (0.10.0).

The package ships one small finished game — ``examples/
demo-arena``: an approved scope, the generated game, its test
and finish reports, pipeline state at `finished`. It was built
by the factory itself (scope → orchestrator → 7/7 finish), so
what a new maker sees is the real end state, not a mock-up.

"Opening" the demo copies it into the maker's workspace, where
it behaves like any project: look around, re-run the tests, run
exports on it. The copy never overwrites an existing folder of
the same name. The tree is copied by walking the package
resources file by file, so it works identically from a source
checkout, the zipapp and the frozen binary.
"""

from __future__ import annotations

from importlib import resources
from pathlib import Path

from . import projects as projects_module

DEMO_NAME = "demo-arena"
_EXCLUDE_DIRS = {".git", ".godot", "__pycache__", "exports"}


class DemoError(Exception):
    """Raised when the demo cannot be installed as asked."""


def demo_source():
    """The packaged demo tree (an importlib.resources handle)."""
    return resources.files(__package__).joinpath("examples", DEMO_NAME)


def demo_available() -> bool:
    try:
        source = demo_source()
        return source.is_dir() and (source / "SCOPE.md").is_file()
    except Exception:
        return False


def _walk(node, prefix: str = ""):
    """Yield (relative path, file handle) for every file in a
    resource tree, skipping build/cache directories."""
    for child in node.iterdir():
        if child.is_dir():
            if child.name in _EXCLUDE_DIRS:
                continue
            yield from _walk(child, f"{prefix}{child.name}/")
        elif child.is_file():
            yield f"{prefix}{child.name}", child


def demo_size() -> tuple[int, int]:
    """(files, bytes) of the packaged demo tree."""
    files = 0
    total = 0
    for _rel, node in _walk(demo_source()):
        total += len(node.read_bytes())
        files += 1
    return files, total


def install_demo(workspace, name: str = DEMO_NAME) -> dict:
    """Copy the bundled demo into *workspace* as *name*.

    Refuses — DemoError, nothing written — when the name is not
    a usable folder name or a folder of that name already
    exists: an existing project is never overwritten.
    Returns {"name", "path", "files"}."""
    name = (name or "").strip() or DEMO_NAME
    problem = projects_module.validate_folder_name(workspace, name)
    if problem:
        raise DemoError(problem)
    if not demo_available():
        raise DemoError(
            "The bundled demo project is missing from this "
            "Weaverbird build — nothing was copied.")
    target = Path(workspace) / name
    if target.exists():
        raise DemoError(
            f"A folder named {name!r} already exists in this "
            "workspace — the demo was not copied over it.")
    written = 0
    try:
        for rel, node in _walk(demo_source()):
            dest = target / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(node.read_bytes())
            written += 1
    except Exception:
        # A half-copied demo is worse than none.
        import shutil
        shutil.rmtree(target, ignore_errors=True)
        raise
    return {"name": name, "path": target, "files": written}
