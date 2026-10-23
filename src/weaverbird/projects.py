"""Project creation — one code path for the CLI and the app.

``weaverbird init`` and the app's New project flow both land here,
so a project scaffolded from a terminal and one created in the
browser are identical: the SCOPE.md template, the connector
registry, the pipeline state, an empty provenance log and an
empty ledger. The app additionally records the maker's display
name for the project (``.weaverbird/project.json``) and can seed
the scope's Title from it and store a starting prompt — the
prompt itself is consumed later, by the interview's prompt-first
entry (interview.start), exactly as a CLI-stored prompt is.
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

from . import template_text
from . import ledger as ledger_module
from . import provenance as provenance_module
from . import scope as scope_module
from . import state as state_module

PROJECT_META_REL = ".weaverbird/project.json"
TITLE_FIELD = "Title (working)"

# Folder names a project may not take: the workspace's own files.
RESERVED_FOLDERS = ("global-settings.yaml", ".weaverbird-app")

_FOLDER_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
_NAME_OK_RE = re.compile(r"^[A-Za-z0-9 _'’.-]+$")


class ProjectError(Exception):
    """Raised when a project cannot be created as asked."""


def slugify(name: str) -> str:
    """A folder name from a display name: lowercase, runs of
    anything that is not a letter or digit become one dash."""
    slug = re.sub(r"[^a-z0-9]+", "-", str(name).lower()).strip("-")
    return slug


def validate_folder_name(workspace, folder: str) -> str | None:
    """Why this folder name cannot be used in the workspace, or
    None when it can. Plain-language reasons — the app shows them
    verbatim in the creation form."""
    folder = (folder or "").strip()
    if not folder:
        return "The folder name is empty."
    if folder in RESERVED_FOLDERS:
        return (f"{folder!r} is reserved for the workspace itself — "
                "pick another folder name.")
    if not _FOLDER_RE.match(folder):
        return ("The folder name must be lowercase letters, digits "
                "and dashes (no spaces, capitals or punctuation) — "
                "for example 'star-shepherd'.")
    if (Path(workspace) / folder).exists():
        return (f"A folder named {folder!r} already exists in this "
                "workspace — pick another folder name.")
    return None


def create_project(target, with_settings: bool = False,
                   force: bool = False) -> list[str]:
    """Scaffold a project at *target*: the exact creation the CLI
    has always done. Returns the list of what was created (for
    the CLI to print). Raises ProjectError when the directory is
    not empty and *force* is not set."""
    target = Path(target)
    if target.exists() and any(target.iterdir()) and not force:
        raise ProjectError(
            f"Refusing to initialise in {target}: the directory is "
            "not empty. Use --force to scaffold anyway.")
    target.mkdir(parents=True, exist_ok=True)
    created = []

    scope_dest = target / "SCOPE.md"
    scope_dest.write_text(template_text("SCOPE.md"), encoding="utf-8")
    created.append("SCOPE.md (scope interview file — fill every "
                   "REQUIRED field)")

    connectors_dest = target / "connectors.yaml"
    connectors_dest.write_text(template_text("connectors.yaml"),
                               encoding="utf-8")
    created.append("connectors.yaml (connector registry for this "
                   "project)")

    if with_settings:
        settings_dest = target / "global-settings.yaml"
        settings_dest.write_text(template_text("global-settings.yaml"),
                                 encoding="utf-8")
        created.append("global-settings.yaml (global budgets — fill "
                       "in the REQUIRED budget fields)")

    state_dir = target / ".weaverbird"
    state_dir.mkdir(parents=True, exist_ok=True)
    state_module.init_state(target)
    created.append(".weaverbird/state.json (pipeline state, at "
                   "stage: scope)")
    provenance_file = provenance_module.provenance_path_for(target)
    if not provenance_file.exists():
        provenance_file.write_text(
            json.dumps({"records": []}, indent=2) + "\n",
            encoding="utf-8")
    created.append(".weaverbird/provenance.json (asset provenance "
                   "log, empty)")
    ledger_file = ledger_module.ledger_path_for(target)
    ledger_file.touch(exist_ok=True)
    created.append(".weaverbird/ledger.jsonl (token/cost ledger, "
                   "empty)")
    return created


def display_name_for(project_dir) -> str:
    """The maker's display name for a project, recorded at
    creation in .weaverbird/project.json — '' when none was
    recorded (CLI-created projects, older projects, a damaged
    file). Failure-tolerant by design: this feeds page titles."""
    try:
        data = json.loads(
            (Path(project_dir) / PROJECT_META_REL).read_text(
                encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    if isinstance(data, dict):
        name = data.get("display_name")
        if isinstance(name, str):
            return name.strip()
    return ""


def new_project(workspace, display_name: str, folder: str = "",
                prompt: str = "") -> dict:
    """Create a project from the app's New project form.

    Validates everything before touching the disk, so a refused
    creation leaves nothing behind; a failure mid-scaffold removes
    the half-made folder again. The display name seeds the scope's
    Title (working) — the maker just stated it — and a starting
    prompt is stored verbatim for the interview to consume.
    Returns {"name": folder, "path": Path, "display_name": str}.
    """
    workspace = Path(workspace)
    display_name = " ".join(str(display_name or "").split())
    if not display_name:
        raise ProjectError("Give the project a name — it is the "
                           "working title of your game.")
    if len(display_name) > 120 or not _NAME_OK_RE.match(display_name):
        raise ProjectError(
            "The project name has characters a title cannot use — "
            "letters, digits, spaces and . ' - are fine.")
    folder = (folder or "").strip() or slugify(display_name)
    if not folder:
        raise ProjectError(
            "The name does not reduce to a folder name — type a "
            "folder name yourself (lowercase letters, digits, "
            "dashes).")
    problem = validate_folder_name(workspace, folder)
    if problem:
        raise ProjectError(problem)
    target = workspace / folder
    try:
        create_project(target)
        meta = {"display_name": display_name}
        (target / PROJECT_META_REL).write_text(
            json.dumps(meta, indent=2) + "\n", encoding="utf-8")
        scope_file = scope_module.scope_path_for(target)
        parsed = scope_module.load_scope(scope_file)
        title = next((f for f in parsed.fields
                      if f.name == TITLE_FIELD), None)
        if title is not None and not title.filled:
            scope_module.set_field_value(scope_file, TITLE_FIELD,
                                         display_name)
        if prompt and prompt.strip():
            scope_module.set_original_prompt(scope_file,
                                             prompt.strip())
    except ProjectError:
        raise
    except Exception:
        # Nothing half-created survives a failed creation.
        shutil.rmtree(target, ignore_errors=True)
        raise
    return {"name": folder, "path": target,
            "display_name": display_name}
