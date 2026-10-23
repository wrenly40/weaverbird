"""The skeleton builder — pipeline stage 2 (design and skeleton).

Turns a complete, approved scope file into a real, playable grey-box
Godot project under ``<project>/game/``. Everything here is
deterministic: the project is rendered from the templates packaged in
``weaverbird/skeleton_templates`` with a handful of values taken from
the scope (title, pitch, a style colour when the style lock names
one, the target count, the round length). No model is called and no
tokens are spent — the stage adds a zero-cost entry to the ledger to
prove it.

After writing the files, the builder runs Godot itself against the
generated project — an import pass, a run of the main (menu) scene, a
run of the game scene, and the shipped flow smoke run
(menu → game → collect every target → win screen). Any Godot error
fails the build: the failure digest is reported, the pipeline does
not advance, and no known-good tag is made. On success the project is
committed to local git and tagged ``known-good/skeleton``.
"""

from __future__ import annotations

import json
import math
import re
import subprocess
import time
from datetime import datetime, timezone
from importlib import resources
from pathlib import Path

from . import godot as godot_module
from . import ledger as ledger_module
from . import provenance as provenance_module
from . import scope as scope_module
from . import state as state_module
from . import vcs as vcs_module

GAME_DIR_NAME = "game"
REPORT_NAME = "skeleton_report.json"
KNOWN_GOOD_TAG = "known-good/skeleton"
PROVENANCE_NOTE = "weaverbird skeleton template"

_DEFAULT_ACCENT = (0x6C / 255, 0x4C / 255, 0xF1 / 255)  # #6C4CF1
_DEFAULT_ACCENT_HEX = "#6C4CF1"
_DEFAULT_TARGET = (0.25, 0.80, 0.45)

_HEX_RE = re.compile(r"#([0-9a-fA-F]{6}|[0-9a-fA-F]{3})\b")
_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")
_INT_RE = re.compile(r"\d+")
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
_ERROR_MARKERS = ("SCRIPT ERROR", "ERROR")


class SkeletonError(Exception):
    """Raised when a skeleton build is refused or validation fails."""


# ---------------------------------------------------------------------------
# Scope values

def _field_values(parsed_scope) -> dict:
    return {f.name: f.value for f in parsed_scope.fields}


def _session_minutes(raw: str):
    match = _NUMBER_RE.search(raw or "")
    if not match:
        return None
    try:
        return float(match.group(0))
    except ValueError:
        return None


def _round_seconds(minutes) -> int:
    """Grey-box round length: scope minutes scaled down, clamped."""
    if minutes is None:
        return 60
    return int(min(max(math.ceil(minutes) * 15, 30), 180))


def _target_count(raw: str) -> int:
    match = _INT_RE.search(raw or "")
    if not match:
        return 5
    return int(min(max(int(match.group(0)), 1), 12))


def _hex_to_rgb(digits: str) -> tuple:
    if len(digits) == 3:
        digits = "".join(ch * 2 for ch in digits)
    return tuple(int(digits[i:i + 2], 16) / 255 for i in (0, 2, 4))


def _scope_colours(values: dict) -> tuple:
    """(accent_rgb, target_rgb, accent_hex) from the style lock.

    Only the Palette and Style sample fields are read. Anything
    unparseable falls back quietly to the neutral defaults.
    """
    hexes: list[str] = []
    for field_name in (
        "Palette",
        "Style sample chosen (id / link to approved sample)",
    ):
        hexes.extend(_HEX_RE.findall(values.get(field_name, "")))
    rgbs = [_hex_to_rgb(h) for h in hexes]
    accent = rgbs[0] if rgbs else _DEFAULT_ACCENT
    if hexes:
        first = hexes[0].upper()
        if len(first) == 3:
            first = "".join(ch * 2 for ch in first)
        accent_hex = "#" + first
    else:
        accent_hex = _DEFAULT_ACCENT_HEX
    target = _DEFAULT_TARGET
    if len(rgbs) > 1 and rgbs[1] != accent:
        target = rgbs[1]
    return accent, target, accent_hex


def _colour_expr(rgb) -> str:
    def part(value: float) -> str:
        return ("%0.3f" % value).rstrip("0").rstrip(".") or "0"

    return "Color(%s, %s, %s)" % tuple(part(c) for c in rgb)


def _substitutions(values: dict) -> dict:
    title = values.get("Title (working)", "") or "Untitled Game"
    pitch = values.get("One-line pitch", "") or ""
    minutes = _session_minutes(values.get("Session length (one sitting)", ""))
    seconds = _round_seconds(minutes)
    count = _target_count(
        values.get("Content counts (levels / waves / rooms / items, as applicable)", "")
    )
    accent, target, accent_hex = _scope_colours(values)
    return {
        "{{APP_NAME_JSON}}": json.dumps(title),
        "{{TITLE_JSON}}": json.dumps(title),
        "{{PITCH_JSON}}": json.dumps(pitch),
        "{{TITLE}}": title,
        "{{PITCH}}": pitch,
        "{{ACCENT_COLOR}}": _colour_expr(accent),
        "{{TARGET_COLOR}}": _colour_expr(target),
        "{{ACCENT_HEX}}": accent_hex,
        "{{ROUND_SECONDS}}": str(seconds),
        "{{TARGET_COUNT}}": str(count),
        "{{SESSION_TEXT}}": (
            f"{minutes:g} minutes" if minutes is not None else "not stated"
        ),
    }


# ---------------------------------------------------------------------------
# Template rendering

def _template_files() -> list:
    """(relative path, resource) for every skeleton template file."""
    root = resources.files(__package__).joinpath("skeleton_templates")
    found: list = []

    def walk(node, prefix: str) -> None:
        for child in sorted(node.iterdir(), key=lambda c: c.name):
            rel = prefix + child.name
            if child.is_dir():
                walk(child, rel + "/")
            else:
                found.append((rel, child))

    walk(root, "")
    return found


def _render(text: str, substitutions: dict) -> str:
    for token, value in substitutions.items():
        text = text.replace(token, value)
    return text


# ---------------------------------------------------------------------------
# Godot validation

def _error_digest(output: str) -> list:
    """The error lines of a Godot run, summarised — never a raw dump."""
    lines: list[str] = []
    for raw in output.splitlines():
        line = _ANSI_RE.sub("", raw).strip()
        if any(marker in line for marker in _ERROR_MARKERS):
            if line not in lines:
                lines.append(line[:200])
    return lines[:20]


def _run_godot(godot_bin: str, game_dir: Path, args: list, timeout: int = 120) -> dict:
    command = [godot_bin, "--headless", "--path", str(game_dir), *args]
    started = time.monotonic()
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, timeout=timeout
        )
        output = (result.stdout or "") + "\n" + (result.stderr or "")
        returncode = result.returncode
    except subprocess.TimeoutExpired:
        output = ""
        returncode = None
    duration = round(time.monotonic() - started, 2)
    errors = _error_digest(output)
    ok = returncode == 0 and not errors
    if returncode is None:
        errors = [f"Timed out after {timeout} seconds."] + errors
        ok = False
    return {
        "command": " ".join(command),
        "returncode": returncode,
        "duration_seconds": duration,
        "errors": errors,
        "ok": ok,
        "smoke_pass": "SMOKE PASS" in output,
    }


def validate_project(project_dir, godot_path=None, reporter=None) -> dict:
    """Run Godot against the generated project. Returns the report dict.

    Four runs, in order: import, main (menu) scene, game scene, and
    the flow smoke run shipped in the project. A run passes when
    Godot exits 0 and prints no errors; the smoke run must also print
    its SMOKE PASS line (it exits 1 itself on failure).

    ``reporter`` (optional) is called after each run with a short
    step note — the job runner's live-progress hook. It may raise
    to abort (job cancellation); nothing catches that here.
    """
    project = Path(project_dir)
    game_dir = project / GAME_DIR_NAME
    godot_bin, version = godot_module.find_godot(godot_path)
    validation = {
        "ran": True,
        "passed": False,
        "godot_path": godot_bin,
        "godot_version": version,
        "runs": [],
    }
    if not godot_bin:
        validation["error"] = (
            "Godot was not found, so the skeleton could not be validated. "
            "Install Godot 4 or set WEAVERBIRD_GODOT to its path."
        )
        return validation
    if not (game_dir / "project.godot").exists():
        validation["error"] = f"No Godot project found at {game_dir}."
        return validation

    steps = [
        ("import", ["--import"]),
        ("main scene", ["--quit-after", "45"]),
        ("game scene", ["res://scenes/game.tscn", "--quit-after", "45"]),
        ("flow smoke", ["--script", "smoke/smoke.gd"]),
    ]
    passed = True
    for index, (name, args) in enumerate(steps):
        run = _run_godot(godot_bin, game_dir, args)
        run["name"] = name
        if name == "flow smoke" and not run["smoke_pass"]:
            # The smoke script exits 0 only on success, so a failed
            # smoke also shows up in the return code — but say it
            # plainly either way.
            run["ok"] = False
            if not run["errors"]:
                run["errors"] = ["The smoke run did not print SMOKE PASS."]
        validation["runs"].append(run)
        if reporter is not None:
            reporter(
                f"Validation run {index + 1} of {len(steps)}: {name} — "
                f"{'ok' if run['ok'] else 'FAILED'}",
                done=index + 1, total=len(steps))
        if not run["ok"]:
            passed = False
            break
    validation["passed"] = passed
    return validation


# ---------------------------------------------------------------------------
# The build

def report_path_for(project_dir) -> Path:
    return Path(project_dir) / ".weaverbird" / REPORT_NAME


def load_report(project_dir):
    path = report_path_for(project_dir)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def _write_report(project_dir, report: dict) -> None:
    path = report_path_for(project_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")


def build_skeleton(project_dir, godot_path=None, validate: bool = True,
                   reporter=None) -> dict:
    """Build the grey-box skeleton for a project. Returns the report.

    Requires the project's scope to be complete AND approved. Raises
    SkeletonError with a plain reason otherwise, and when Godot
    validation fails (the report is still written, so what happened
    is on record). ``reporter`` (optional) narrates the step
    boundaries for the job runner; with none, the build is silent
    exactly as before.
    """
    project = Path(project_dir)
    scope_file = scope_module.scope_path_for(project)
    if not scope_file.exists():
        raise SkeletonError(f"No scope file found at {scope_file}.")
    parsed = scope_module.load_scope(scope_file)
    if not parsed.is_complete:
        missing = ", ".join(f.name for f in parsed.missing_fields)
        raise SkeletonError(
            "Scope is incomplete, so the skeleton cannot be built yet — "
            f"still missing: {missing}. "
            f"See the questions with 'weaverbird scope questions {project}'."
        )
    if not parsed.is_approved:
        raise SkeletonError(
            "The scope is complete but not approved yet, so the skeleton "
            f"cannot be built. Approve it with 'weaverbird scope approve "
            f"{project} --by <name>'."
        )

    substitutions = _substitutions(_field_values(parsed))
    game_dir = project / GAME_DIR_NAME
    written: list[str] = []
    for rel, resource in _template_files():
        dest_rel = ".gitignore" if rel == "gitignore.txt" else rel
        raw = resource.read_bytes()
        try:
            data = _render(raw.decode("utf-8"), substitutions).encode("utf-8")
        except UnicodeDecodeError:
            data = raw
        dest = game_dir / dest_rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        written.append(f"{GAME_DIR_NAME}/{dest_rel}")
    written.sort()
    if reporter is not None:
        reporter(f"Wrote the grey-box project ({len(written)} files)")

    report = {
        "stage": "skeleton",
        "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "game_dir": str(game_dir),
        "files": written,
        "validation": {"ran": False, "passed": False},
        "known_good_tag": None,
        "commit": None,
    }

    if validate:
        report["validation"] = validate_project(
            project, godot_path=godot_path, reporter=reporter)

    # The factory's records: provenance for every generated file, and
    # a zero-cost ledger entry — this stage is deterministic.
    for rel in written:
        provenance_module.add_record(project, rel, "code", note=PROVENANCE_NOTE)
    ledger_module.add_entry(project, "skeleton", "cheap", 0, 0.0)
    _write_report(project, report)

    if validate and not report["validation"]["passed"]:
        digest = _failure_digest(report["validation"])
        raise SkeletonError(
            "Godot validation failed, so the skeleton is not known-good "
            f"and was not tagged. {digest} "
            f"Full report: {report_path_for(project)}"
        )

    # Version the result locally; tag it known-good once validated.
    vcs_module.init_repo(project)
    vcs_module.ensure_local_identity(project)
    try:
        report["commit"] = vcs_module.commit_all(
            project, "Weaverbird skeleton: grey-box Godot project"
        )
    except vcs_module.VcsError:
        report["commit"] = None  # nothing new to commit (a rebuild)
    if validate and KNOWN_GOOD_TAG not in vcs_module.list_tags(project):
        vcs_module.tag_known_good(project, KNOWN_GOOD_TAG)
        report["known_good_tag"] = KNOWN_GOOD_TAG
    elif validate:
        report["known_good_tag"] = KNOWN_GOOD_TAG
    if validate and reporter is not None:
        reporter("Committed and tagged known-good/skeleton")
    _write_report(project, report)
    return report


def _failure_digest(validation: dict) -> str:
    if validation.get("error"):
        return validation["error"]
    for run in validation.get("runs", []):
        if not run.get("ok"):
            errors = run.get("errors") or ["Godot exited with an error."]
            return f"In the {run.get('name')} run: " + " ".join(errors[:5])
    return "Validation did not pass."


# ---------------------------------------------------------------------------
# Pipeline wiring

def skeleton_stage_handler(project_dir, state=None) -> bool:
    """Pipeline handler for the skeleton stage.

    A validated skeleton must exist for the pipeline to move through
    this stage: if the build report already shows a passed
    validation, the handler passes at once; otherwise it runs the
    full build (which raises SkeletonError, blocking the advance,
    when anything fails).
    """
    project = Path(project_dir)
    report = load_report(project)
    if (
        report
        and report.get("validation", {}).get("passed")
        and (project / GAME_DIR_NAME / "project.godot").exists()
    ):
        return True
    build_skeleton(project)
    return True


def register_stage_handlers() -> None:
    """Register the skeleton builder with the pipeline.

    Registered for "scope" — leaving the scope stage is the
    scope → skeleton transition, which is what produces the skeleton
    — and for "skeleton", so leaving the skeleton stage also requires
    a built, validated skeleton to exist.
    """
    state_module.register_handler("skeleton", skeleton_stage_handler)
    state_module.register_handler("scope", skeleton_stage_handler)
