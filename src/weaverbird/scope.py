"""The scope file (SCOPE.md) and its interview loop.

The scope file is created the moment a prompt arrives and is filled in
live while the factory asks questions. A field marked REQUIRED blocks the
build until it is filled — there are no silent defaults on required
fields. This module parses the file, reports what is still missing, turns
the missing fields into grouped interview questions, and freezes
(approves) a complete scope.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

# Values in the template that are option hints, not real answers.
_HINT_VALUES = {
    "2D / 3D",
    "Micro / Small / Medium",
}

# Friendlier interview questions for fields we know about.
_QUESTIONS = {
    "Title (working)": "What is the working title of the game?",
    "One-line pitch": "Describe the game in one line.",
    "Dimension": "Is the game 2D or 3D?",
    "Genre / archetype": "What genre or archetype is it (for example arena survivor, platformer, puzzle)?",
    "Core verb (what the player does most)": "What does the player do most of the time — the core verb?",
    "Win condition": "How does the player win?",
    "Lose / fail condition": "How does the player lose or fail?",
    "Session length (one sitting)": "How long is one play session, in minutes?",
    "Scope class": "What scope class is it: Micro (jam-size), Small, or Medium?",
    "Difficulty target (who it is for, how hard)": "Who is it for, and how hard should it be?",
    "Must-have features (max 3)": "What are the must-have features (up to three)?",
    "Explicitly out of scope": "What is explicitly out of scope for this game?",
    "Style sample chosen (id / link to approved sample)": "Pick a style from the samples shown — which one is the style lock?",
    "Palette": "What is the colour palette in the chosen style?",
    "Proportions / outline / shading rules": "What are the proportions, outline and shading rules in the chosen style?",
    "Audio mood (music + SFX character)": "What should the music and sound effects feel like?",
    "UI look (follows the style sample)": "What should the menus and UI look like, following the style sample?",
    "AI disclosure stance for stores (follows the allowed sources)": "How should AI use be disclosed to stores, following the allowed sources?",
    "Forbidden sources / content": "Which asset sources or content are forbidden for this game?",
    "Price intent (free / paid + rough price / decide at approval gate)": "Is the game free or paid, and roughly what price?",
    "Telemetry": "Is telemetry allowed in this game? Answer yes or no explicitly.",
}


class ScopeError(Exception):
    """Raised for unreadable scope files and refused approvals."""


class ScopeIncompleteError(ScopeError):
    """Raised when approving a scope that still has empty REQUIRED fields."""

    def __init__(self, missing):
        self.missing = list(missing)
        names = ", ".join(f.name for f in self.missing)
        super().__init__(
            f"Scope is incomplete — {len(self.missing)} required field(s) "
            f"still empty: {names}"
        )


@dataclass
class ScopeField:
    name: str
    section: str
    value: str = ""
    line_no: int | None = None

    @property
    def filled(self) -> bool:
        return bool(self.value)


@dataclass
class Scope:
    path: Path | None
    fields: list[ScopeField] = field(default_factory=list)
    approval: str = ""
    original_prompt: str = ""

    @property
    def required_fields(self) -> list[ScopeField]:
        return list(self.fields)

    @property
    def filled_fields(self) -> list[ScopeField]:
        return [f for f in self.fields if f.filled]

    @property
    def missing_fields(self) -> list[ScopeField]:
        return [f for f in self.fields if not f.filled]

    @property
    def is_complete(self) -> bool:
        return not self.missing_fields

    @property
    def is_approved(self) -> bool:
        return self.approval.startswith("Approved by")


def _clean_value(raw: str) -> str:
    """Return the real value of a field, or '' for template hints."""
    value = raw.strip().rstrip(":").strip()
    if not value:
        return ""
    if value in _HINT_VALUES:
        return ""
    if "(pick any" in value:
        return ""
    return value


def _parse_required_line(text: str, section: str,
                         line_no: int | None = None) -> ScopeField | None:
    """Parse one bullet line into a required ScopeField, if it is one."""
    if "REQUIRED" not in text:
        return None
    if text.startswith("REQUIRED"):
        rest = text[len("REQUIRED"):].strip()
        name, _, raw_value = rest.partition(":")
        return ScopeField(name=name.strip(), section=section,
                          value=_clean_value(raw_value), line_no=line_no)
    # Shape like: "Telemetry: off by default — REQUIRED explicit yes/no:"
    name = text.split(":", 1)[0].strip()
    return ScopeField(name=name, section=section, value="", line_no=line_no)


def parse_scope_text(text: str, path=None) -> Scope:
    """Parse SCOPE.md text into a Scope."""
    scope = Scope(path=Path(path) if path else None)
    section = "General"
    for line_no, line in enumerate(text.splitlines()):
        stripped = line.strip()
        if stripped.startswith("## "):
            section = stripped[3:].strip()
            continue
        if not stripped.startswith("- "):
            continue
        content = stripped[2:].strip()
        if content.startswith("Approval:"):
            scope.approval = _clean_value(content.partition(":")[2])
            continue
        if content.startswith("Original prompt"):
            scope.original_prompt = content.partition(":")[2].strip()
            continue
        parsed = _parse_required_line(content, section, line_no=line_no)
        if parsed is not None:
            scope.fields.append(parsed)
    return scope


def load_scope(path) -> Scope:
    """Load and parse a SCOPE.md file."""
    path = Path(path)
    if not path.exists():
        raise ScopeError(f"Scope file not found: {path}")
    return parse_scope_text(path.read_text(encoding="utf-8"), path=path)


def question_for(field_: ScopeField) -> str:
    return _QUESTIONS.get(field_.name, f"What is the {field_.name.lower()}?")


def interview_questions(scope: Scope) -> list[tuple[str, list[str]]]:
    """Grouped questions for exactly the missing required fields.

    Returns a list of (section, [question, ...]) in file order.
    """
    groups: list[tuple[str, list[str]]] = []
    by_section: dict[str, list[str]] = {}
    for missing in scope.missing_fields:
        if missing.section not in by_section:
            by_section[missing.section] = []
            groups.append((missing.section, by_section[missing.section]))
        by_section[missing.section].append(
            f"{question_for(missing)}  [{missing.name}]"
        )
    return groups


_APPROVAL_RE = re.compile(r"^- Approval:.*$", re.MULTILINE)


def approve_scope(path, by: str, on: date | None = None) -> Scope:
    """Freeze a complete scope by writing its approval line.

    Refuses (ScopeIncompleteError) when any REQUIRED field is empty.
    """
    path = Path(path)
    scope = load_scope(path)
    if not scope.is_complete:
        raise ScopeIncompleteError(scope.missing_fields)
    if not by or not by.strip():
        raise ScopeError("An approver name is required (--by).")
    day = (on or date.today()).isoformat()
    line = f"- Approval: Approved by {by.strip()} on {day}"
    text = path.read_text(encoding="utf-8")
    if _APPROVAL_RE.search(text):
        text = _APPROVAL_RE.sub(line, text, count=1)
    else:
        text = text.rstrip("\n") + "\n" + line + "\n"
    path.write_text(text, encoding="utf-8")
    return load_scope(path)


def scope_path_for(project_dir) -> Path:
    """The scope file for a project directory (or the file itself)."""
    path = Path(project_dir)
    if path.is_file():
        return path
    return path / "SCOPE.md"


# ---------------------------------------------------------------------------
# Writing values back into the scope file.
#
# Both writers keep the file's structure intact: only the one line that
# holds the value is rewritten, everything else stays as it was. They
# are used by the scope interview (storing the prompt, applying answers
# a model extracted) and by the app's scope form.

_ORIGINAL_PROMPT_RE = re.compile(r"^- Original prompt.*$", re.MULTILINE)


def _one_line(value) -> str:
    return " ".join(str(value).split())


def set_original_prompt(path, prompt: str) -> Scope:
    """Store the user's prompt in the scope file's Original prompt line."""
    path = Path(path)
    if not path.exists():
        raise ScopeError(f"Scope file not found: {path}")
    line = f"- Original prompt (verbatim): {_one_line(prompt)}"
    text = path.read_text(encoding="utf-8")
    if _ORIGINAL_PROMPT_RE.search(text):
        text = _ORIGINAL_PROMPT_RE.sub(line, text, count=1)
    elif "## Identity" in text:
        text = text.replace("## Identity", "## Identity\n" + line, 1)
    else:
        text = text.rstrip("\n") + "\n" + line + "\n"
    path.write_text(text, encoding="utf-8")
    return load_scope(path)


def set_field_value(path, field_name: str, value) -> Scope:
    """Write one field's value into the scope file, in place.

    The line is rewritten in the plain "- REQUIRED Name: value" shape
    the parser reads, so the file stays a valid scope file. Raises
    ScopeError when the field is not part of the scope file.
    """
    path = Path(path)
    if not path.exists():
        raise ScopeError(f"Scope file not found: {path}")
    text = path.read_text(encoding="utf-8")
    parsed = parse_scope_text(text, path=path)
    target = None
    for fld in parsed.fields:
        if fld.name == field_name:
            target = fld
            break
    if target is None or target.line_no is None:
        raise ScopeError(f"Scope file {path} has no field named {field_name!r}.")
    lines = text.splitlines()
    lines[target.line_no] = f"- REQUIRED {field_name}: {_one_line(value)}"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return load_scope(path)
