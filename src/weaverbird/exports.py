"""Exporting a finished game to local files (0.10.0).

Exports produce **local files only** — zips in the project's
``exports/`` folder, plus an ``EXPORT_REPORT.md`` at the project
root and a machine-readable ``exports/EXPORTS.json``. Nothing is
uploaded anywhere: publishing to stores is not part of this
release, and this module has no network code at all.

Targets: ``linux``, ``windows``, ``macos`` (the desktop trio, via
Godot's headless ``--export-release``) and ``source`` (a zip of
the game project source). Android and iOS are deliberately out:
they need SDK and keystore setup the factory does not perform,
and the report says so in one line.

Honesty rules, enforced by the worker gate:

- Every requested target ends as either a real artifact
  (verified to exist, with size + sha256 recorded) or a concrete
  named blocker (the missing piece, or Godot's own error line).
  Never a silent skip, never a fake success.
- If the Godot binary or the matching export templates are
  missing, ``source`` still succeeds and each desktop target
  reports blocked, naming the exact missing piece and where the
  templates belong.
- Desktop builds are UNSIGNED. Signing / notarisation is the
  maker's own step; the report says so per target.

Presets: the worker ensures the game has a Godot 4.7
``export_presets.cfg`` covering the desktop targets. A missing
file is written whole; an existing file gains only the presets
it lacks — a maker's own presets (matched by platform) are used
as they are and never edited. The macOS preset needs a bundle
identifier and the universal architecture (the shipped template
is universal-only), and macOS export requires the project's
ETC2/ASTC import setting — when that setting is missing the
worker adds it (additive, and recorded in the report), because
Godot refuses the export without it.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from . import godot as godot_module
from . import patch as patch_module
from . import projects as projects_module
from . import skeleton as skeleton_module

GAME_DIR_NAME = skeleton_module.GAME_DIR_NAME
EXPORTS_DIR_NAME = "exports"
REPORT_NAME = "EXPORT_REPORT.md"
RECORD_NAME = "EXPORTS.json"
BUILD_DIR_NAME = "build"

TARGETS = ("linux", "windows", "macos", "source")
DESKTOP_TARGETS = ("linux", "windows", "macos")
# The Godot template file each desktop target needs inside the
# versioned export-templates folder.
TEMPLATE_FILES = {
    "linux": "linux_release.x86_64",
    "windows": "windows_release_x86_64.exe",
    "macos": "macos.zip",
}
# Canonical Godot preset name + platform string per target.
PRESET_NAMES = {
    "linux": "Linux",
    "windows": "Windows Desktop",
    "macos": "macOS",
}
PLATFORMS = {
    "linux": "Linux",
    "windows": "Windows Desktop",
    "macos": "macOS",
}
EXPORT_TIMEOUT = 600
# A desktop artifact smaller than this is not plausible — the
# Godot runtime alone is tens of megabytes.
MIN_DESKTOP_BYTES = 1_000_000
# The macOS .app's main binary is the universal Godot template
# itself (~170 MB for Godot 4.7) — never smaller than this in a
# complete export. (Godot can exit 0 with a truncated binary
# when its temp staging runs out of space; this check is what
# catches that.)
MIN_MACOS_BINARY_BYTES = 50_000_000


class ExportsError(Exception):
    """Raised when an export run is refused before it starts."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def project_slug(project_dir) -> str:
    slug = projects_module.slugify(Path(project_dir).name)
    return slug or "game"


def artifact_name(slug: str, version: str, target: str) -> str:
    return f"{slug}-{version}-{target}.zip"


# ---------------------------------------------------------------------------
# Godot + templates discovery

def templates_dir_for(godot_version: str | None, home=None) -> Path | None:
    """The export-templates folder matching a Godot version
    string like ``4.7.2.stable.official.<hash>`` — the folder is
    named after the first three components (``4.7.2.stable``)
    under ``~/.local/share/godot/export_templates/``."""
    if not godot_version:
        return None
    # Godot's own scheme: <major>.<minor>.<patch>.<status>
    match = re.match(r"^(\d+)\.(\d+)\.(\d+)\.([A-Za-z]+)",
                     str(godot_version))
    if not match:
        return None
    folder = ".".join(match.groups())
    base = Path(home) if home is not None else Path.home()
    return base / ".local" / "share" / "godot" / "export_templates" / folder


def missing_prerequisite(target: str, godot_bin, godot_version,
                         home=None) -> str | None:
    """None when the target can run, else the concrete blocker:
    the exact missing piece and where it belongs."""
    if godot_bin is None:
        return ("the Godot binary was not found (looked on PATH, "
                "in WEAVERBIRD_GODOT and under "
                "~/workspace/tools/godot/) — install Godot 4 or "
                "set WEAVERBIRD_GODOT to its path")
    templates = templates_dir_for(godot_version, home=home)
    if templates is None:
        return (f"could not parse the Godot version "
                f"({godot_version!r}) to locate its export "
                "templates folder")
    needed = templates / TEMPLATE_FILES[target]
    if not needed.is_file():
        return (f"the export template {TEMPLATE_FILES[target]} is "
                f"missing — expected at {needed}; install the "
                f"Godot {templates.name} export templates there "
                "(Godot editor: Editor → Manage Export Templates)")
    return None


# ---------------------------------------------------------------------------
# export_presets.cfg — read what exists, add only what is missing

_PRESET_HEADER_RE = re.compile(r"^\[preset\.(\d+)\]\s*$")
_PRESET_OPTIONS_RE = re.compile(r"^\[preset\.(\d+)\.options\]\s*$")


def parse_presets(text: str) -> list[dict]:
    """The presets in an export_presets.cfg: [{index, name,
    platform, export_path}]. Tolerant line parser in the spirit
    of godot.parse_project_godot."""
    presets: list[dict] = []
    current: dict | None = None
    in_options = False
    for raw in text.splitlines():
        line = raw.strip()
        header = _PRESET_HEADER_RE.match(line)
        if header:
            current = {"index": int(header.group(1)), "name": "",
                       "platform": "", "export_path": ""}
            presets.append(current)
            in_options = False
            continue
        if _PRESET_OPTIONS_RE.match(line):
            in_options = True
            continue
        if line.startswith("["):
            current = None
            in_options = False
            continue
        if current is None or in_options or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"')
        if key in ("name", "platform", "export_path"):
            current[key] = value
    return presets


def _preset_block(index: int, target: str, slug: str,
                  bundle_id: str) -> str:
    name = PRESET_NAMES[target]
    if target == "linux":
        export_path = f"{BUILD_DIR_NAME}/linux/{slug}.x86_64"
        options = (
            'custom_template/debug=""\n'
            'custom_template/release=""\n'
            "debug/export_console_wrapper=1\n"
            "binary_format/embed_pck=true\n"
            'binary_format/architecture="x86_64"\n'
            "ssh_remote_deploy/enabled=false\n"
        )
    elif target == "windows":
        export_path = f"{BUILD_DIR_NAME}/windows/{slug}.exe"
        options = (
            'custom_template/debug=""\n'
            'custom_template/release=""\n'
            "debug/export_console_wrapper=1\n"
            "binary_format/embed_pck=true\n"
            'binary_format/architecture="x86_64"\n'
            'application/icon=""\n'
            "application/console_wrapper_icon=16777216\n"
            "application/modify_resources=true\n"
            "display/window/subsystem=0\n"
            "texture_format/s3tc_bptc=true\n"
            "texture_format/etc2_astc=true\n"
        )
    else:  # macos — Godot zips the .app itself when the path ends .zip
        export_path = f"{BUILD_DIR_NAME}/macos/{slug}.zip"
        options = (
            'custom_template/debug=""\n'
            'custom_template/release=""\n'
            'application/icon=""\n'
            f'application/bundle_identifier="{bundle_id}"\n'
            "export/distribution_type=1\n"
            "xcode/sdkv_build=false\n"
            "debug/export_console_wrapper=1\n"
            'binary_format/architecture="universal"\n'
            "codesign/enable=false\n"
            "notarization/enable=false\n"
            'privacy/usage_description=""\n'
            "texture_format/s3tc_bptc=true\n"
            "texture_format/etc2_astc=true\n"
        )
    return (
        f"[preset.{index}]\n\n"
        f'name="{name}"\n'
        f'platform="{PLATFORMS[target]}"\n'
        "runnable=true\n"
        "dedicated_server=false\n"
        'custom_features=""\n'
        'export_filter="all_resources"\n'
        'include_filter=""\n'
        'exclude_filter=""\n'
        f'export_path="{export_path}"\n'
        'encryption_include_filters=""\n'
        'encryption_exclude_filters=""\n'
        "encrypt_pck=false\n"
        "encrypt_directory=false\n\n"
        f"[preset.{index}.options]\n\n"
        f"{options}"
    )


def bundle_identifier(slug: str) -> str:
    compact = re.sub(r"[^a-z0-9]+", "", slug.lower()) or "game"
    return f"com.weaverbird.{compact}"


def ensure_presets(game_dir, targets, slug: str) -> dict:
    """Ensure export_presets.cfg covers the requested desktop
    targets. Returns {path, added: [target...], existing:
    [target...], presets: {target: preset dict}} — a maker's
    preset for a platform (any name) counts as existing and is
    used untouched; only missing platforms are appended."""
    game_dir = Path(game_dir)
    path = game_dir / "export_presets.cfg"
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    presets = parse_presets(text)
    by_platform = {p["platform"]: p for p in presets if p["platform"]}
    added: list[str] = []
    existing: list[str] = []
    used: dict[str, dict] = {}
    next_index = (max((p["index"] for p in presets), default=-1) + 1)
    blocks: list[str] = []
    for target in targets:
        if target not in DESKTOP_TARGETS:
            continue
        preset = by_platform.get(PLATFORMS[target])
        if preset is not None:
            existing.append(target)
            used[target] = preset
            continue
        block = _preset_block(next_index, target, slug,
                              bundle_identifier(slug))
        blocks.append(block)
        used[target] = {"index": next_index, "name": PRESET_NAMES[target],
                        "platform": PLATFORMS[target],
                        "export_path": _preset_block_export_path(block)}
        next_index += 1
        added.append(target)
    if blocks:
        joined = "\n".join(b.rstrip("\n") for b in blocks) + "\n"
        if text.strip():
            path.write_text(text.rstrip("\n") + "\n\n" + joined,
                            encoding="utf-8")
        else:
            path.write_text(joined, encoding="utf-8")
    return {"path": path, "added": added, "existing": existing,
            "presets": used}


def _preset_block_export_path(block: str) -> str:
    for line in block.splitlines():
        if line.startswith("export_path="):
            return line.partition("=")[2].strip().strip('"')
    return ""


# ---------------------------------------------------------------------------
# The ETC2/ASTC project setting macOS export requires

def ensure_etc2_astc(game_dir) -> bool:
    """Add ``textures/vram_compression/import_etc2_astc=true``
    under [rendering] in project.godot when missing. Returns True
    when the setting was added by this call. Godot refuses a
    universal/arm64 macOS export without it."""
    path = Path(game_dir) / "project.godot"
    text = path.read_text(encoding="utf-8")
    if "import_etc2_astc" in text:
        return False
    if "[rendering]" in text:
        text = text.replace(
            "[rendering]",
            "[rendering]\n\ntextures/vram_compression/import_etc2_astc=true",
            1)
    else:
        text = (text.rstrip("\n")
                + "\n\n[rendering]\n\n"
                "textures/vram_compression/import_etc2_astc=true\n")
    path.write_text(text, encoding="utf-8")
    return True


# ---------------------------------------------------------------------------
# Running one export

def _error_line(output: str) -> str:
    """Godot's own error line from its output, for the report.

    Godot ends a failed export with a generic "Project export
    for preset ... failed" line — the informative line comes
    BEFORE it (a configuration-errors header plus its detail, or
    the specific ERROR), so prefer those over the generic tail.
    """
    lines = [ln.strip() for ln in output.splitlines() if ln.strip()]
    for i, line in enumerate(lines):
        if "due to configuration errors" in line:
            detail = lines[i + 1] if i + 1 < len(lines) else ""
            return f"{line} {detail}".strip()
    for line in lines:
        if line.startswith("ERROR:") and \
                "Project export for preset" not in line:
            return line
    errors = [ln for ln in lines if ln.startswith("ERROR:")]
    if errors:
        return errors[-1]
    return lines[-1] if lines else "Godot produced no output."


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _macos_binary_size(zip_path: Path) -> int | None:
    """The size of the .app main binary inside Godot's macOS
    zip (the entry under Contents/MacOS/), or None when the zip
    is unreadable or has none."""
    try:
        with zipfile.ZipFile(zip_path) as zf:
            for info in zf.infolist():
                if "/Contents/MacOS/" in info.filename \
                        and not info.is_dir():
                    return info.file_size
    except (zipfile.BadZipFile, OSError):
        return None
    return None


def _zip_files(zip_path: Path, files: list[tuple[Path, str]]) -> None:
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for source, arcname in files:
            zf.write(source, arcname)


def _export_desktop(game_dir: Path, target: str, preset: dict,
                    godot_bin: str, exports_dir: Path, slug: str,
                    version: str) -> dict:
    """Run Godot for one desktop target and package the result.
    Returns the per-target record (exported or blocked)."""
    export_rel = preset.get("export_path") or ""
    if not export_rel:
        return {"target": target, "status": "blocked",
                "reason": f"the {preset.get('name', target)} preset "
                          "has no export_path — set one in "
                          "export_presets.cfg and re-run"}
    out_path = (game_dir / export_rel).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    command = [godot_bin, "--headless", "--path", str(game_dir),
               "--export-release", preset["name"], str(out_path)]
    try:
        proc = subprocess.run(command, capture_output=True, text=True,
                              timeout=EXPORT_TIMEOUT)
        output = (proc.stdout or "") + (proc.stderr or "")
        returncode = proc.returncode
    except subprocess.TimeoutExpired:
        return {"target": target, "status": "blocked",
                "reason": f"Godot export timed out after "
                          f"{EXPORT_TIMEOUT}s"}
    except OSError as exc:
        return {"target": target, "status": "blocked",
                "reason": f"could not run Godot: {exc}"}
    if returncode != 0 or not out_path.is_file():
        return {"target": target, "status": "blocked",
                "reason": "Godot export failed: "
                          + _error_line(output)}
    if target == "macos":
        # Godot already zipped the .app (export path ends .zip).
        # Verify the .app's main binary is really inside and
        # non-trivial: a truncated export can exit 0 with an
        # empty binary, and a zip-size check alone would wave a
        # partial one through.
        binary_size = _macos_binary_size(out_path)
        if binary_size is None:
            return {"target": target, "status": "blocked",
                    "reason": "the exported macOS zip contains no "
                              ".app main binary — the export is "
                              "incomplete"}
        if binary_size < MIN_MACOS_BINARY_BYTES:
            return {"target": target, "status": "blocked",
                    "reason": f"the exported .app's main binary "
                              f"is only {binary_size} bytes — the "
                              "export is incomplete (a real Godot "
                              "binary is tens of megabytes)"}
        artifact = exports_dir / artifact_name(slug, version, target)
        shutil.copyfile(out_path, artifact)
        members = [out_path]
    else:
        # The binary, plus a separate .pck when the preset did
        # not embed it.
        members = [out_path]
        pck = out_path.with_suffix(".pck")
        if pck.is_file():
            members.append(pck)
        total = sum(m.stat().st_size for m in members)
        if total < MIN_DESKTOP_BYTES:
            return {"target": target, "status": "blocked",
                    "reason": f"the exported files total only "
                              f"{total} bytes — not plausible for "
                              "a real build"}
        artifact = exports_dir / artifact_name(slug, version, target)
        _zip_files(artifact, [(m, m.name) for m in members])
    record = {"target": target, "status": "exported",
              "artifact": f"{EXPORTS_DIR_NAME}/{artifact.name}",
              "size": artifact.stat().st_size,
              "sha256": _sha256(artifact),
              "note": _unsigned_note(target)}
    if target == "macos":
        record["bundle_identifier"] = bundle_identifier(slug)
    return record


def _unsigned_note(target: str) -> str:
    if target == "macos":
        return ("unsigned — ad-hoc signature only; Gatekeeper "
                "will block it until the maker signs and "
                "notarises it with their own Apple developer "
                "identity")
    if target == "windows":
        return ("unsigned — SmartScreen will warn until the "
                "maker signs it with their own code-signing "
                "certificate")
    return "unsigned — no signing step applies on Linux"


_SOURCE_EXCLUDE_DIRS = {".godot", ".git", BUILD_DIR_NAME}


def _export_source(game_dir: Path, exports_dir: Path, slug: str,
                   version: str) -> dict:
    artifact = exports_dir / artifact_name(slug, version, "source")
    files: list[tuple[Path, str]] = []
    for child in sorted(game_dir.rglob("*")):
        if not child.is_file():
            continue
        rel = child.relative_to(game_dir)
        if any(part in _SOURCE_EXCLUDE_DIRS for part in rel.parts):
            continue
        files.append((child, rel.as_posix()))
    if not files:
        return {"target": "source", "status": "blocked",
                "reason": "the game folder is empty — nothing to "
                          "package"}
    _zip_files(artifact, files)
    return {"target": "source", "status": "exported",
            "artifact": f"{EXPORTS_DIR_NAME}/{artifact.name}",
            "size": artifact.stat().st_size,
            "sha256": _sha256(artifact),
            "note": f"the game project source ({len(files)} files; "
                    ".godot/, build/ and .git excluded)"}


# ---------------------------------------------------------------------------
# The run

def run_exports(project_dir, targets=None, godot_path=None,
                home=None, reporter=None) -> dict:
    """Export the requested targets for a finished project.

    Returns the report dict (also written to EXPORT_REPORT.md +
    exports/EXPORTS.json). Raises ExportsError only when the run
    cannot start at all (no game project). Per-target failures
    are records, never exceptions — the gate is that every target
    ends exported or blocked with a named reason.
    """
    project = Path(project_dir)
    game_dir = project / GAME_DIR_NAME
    if not (game_dir / "project.godot").exists():
        raise ExportsError(
            "No game found — there is nothing to export yet.")
    requested = [t for t in (targets or TARGETS) if t in TARGETS]
    if not requested:
        raise ExportsError(
            f"No known targets requested. Known: {', '.join(TARGETS)}.")
    slug = project_slug(project)
    version = patch_module.game_version(project)
    exports_dir = project / EXPORTS_DIR_NAME
    exports_dir.mkdir(parents=True, exist_ok=True)

    godot_bin, godot_version = godot_module.find_godot(godot_path)
    presets_info = ensure_presets(game_dir, requested, slug)
    notes: list[str] = []
    if presets_info["added"]:
        notes.append(
            "Presets written by the factory for: "
            + ", ".join(presets_info["added"])
            + " (export_presets.cfg — edit freely, re-runs never "
              "overwrite a maker's presets).")
    etc2_added = False
    if "macos" in requested:
        etc2_added = ensure_etc2_astc(game_dir)
        if etc2_added:
            notes.append(
                "Added textures/vram_compression/import_etc2_astc"
                "=true to project.godot — Godot refuses a "
                "universal macOS export without it.")

    results: list[dict] = []
    total = len(requested)
    for step, target in enumerate(requested, start=1):
        if reporter is not None:
            reporter(f"Exporting {target}", done=step - 1, total=total)
        if target == "source":
            record = _export_source(game_dir, exports_dir, slug, version)
        else:
            blocker = missing_prerequisite(target, godot_bin,
                                           godot_version, home=home)
            if blocker is not None:
                record = {"target": target, "status": "blocked",
                          "reason": blocker}
            else:
                record = _export_desktop(
                    game_dir, target, presets_info["presets"][target],
                    godot_bin, exports_dir, slug, version)
        results.append(record)
        if reporter is not None:
            reporter(f"{target}: {record['status']}",
                     done=step, total=total)

    # The worker-managed staging folder (game/build) is removed
    # when every desktop preset in play was written by the
    # factory — a maker's own build output is left alone.
    if presets_info["added"] and not presets_info["existing"]:
        shutil.rmtree(game_dir / BUILD_DIR_NAME, ignore_errors=True)

    report = {
        "project": project.name,
        "slug": slug,
        "version": version,
        "ran_at": _now(),
        "godot_version": godot_version,
        "targets": results,
        "notes": notes,
        "out_of_scope": ("Android and iOS are not exported: they "
                         "need SDK and keystore setup the factory "
                         "does not perform."),
    }
    (exports_dir / RECORD_NAME).write_text(
        json.dumps(report, indent=2) + "\n", encoding="utf-8")
    (project / REPORT_NAME).write_text(render_report(report),
                                       encoding="utf-8")
    return report


def load_record(project_dir) -> dict | None:
    """The last export run's machine record, or None."""
    path = Path(project_dir) / EXPORTS_DIR_NAME / RECORD_NAME
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return data if isinstance(data, dict) else None


def render_report(report: dict) -> str:
    lines = [
        f"# Export report — {report['project']}",
        "",
        f"Exported at {report['ran_at']} with Godot "
        f"{report.get('godot_version') or 'not found'}. "
        f"Game version {report['version']}.",
        "",
        "Exports are local files in the project's `exports/` "
        "folder. Nothing was uploaded anywhere — publishing to "
        "stores is not part of this release.",
        "",
        "| Target | Status | Artifact | Size | sha256 |",
        "|---|---|---|---|---|",
    ]
    for record in report["targets"]:
        if record["status"] == "exported":
            lines.append(
                f"| {record['target']} | exported | "
                f"{record['artifact']} | {record['size']} bytes | "
                f"{record['sha256'][:16]}… |")
        else:
            lines.append(
                f"| {record['target']} | blocked | — | — | — |")
    lines.append("")
    for record in report["targets"]:
        if record["status"] == "exported":
            lines.append(f"- **{record['target']}**: "
                         f"{record.get('note', '')}.")
            if record["target"] == "macos" and \
                    record.get("bundle_identifier"):
                lines.append(
                    f"  Bundle identifier: "
                    f"{record['bundle_identifier']} (set in "
                    "export_presets.cfg; change it there to your "
                    "own).")
        else:
            lines.append(f"- **{record['target']}** blocked: "
                         f"{record['reason']}.")
    for note in report.get("notes", []):
        lines.append(f"- {note}")
    lines += ["", report["out_of_scope"], ""]
    return "\n".join(lines)
