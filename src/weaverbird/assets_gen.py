"""Asset generation — pipeline stage "assets".

Deterministic, no model: given the scope's style-lock palette and
the game's archetype, this stage draws the game's art as flat
vector sprites (SVG, in the platform's own design language —
rounded, minimal, one accent), generates a real icon for the game,
and synthesises its audio with the standard library's ``wave``
module: short SFX (click, collect, hurt, jump, push, a win jingle,
a lose tone) and a looping music track built from a generated
chord/arpeggio pattern.

Everything lands in ``game/assets/`` (audio in ``game/assets/
audio/``) plus the icon at ``game/icon.svg``. Every file is
recorded in the provenance log at the moment it is made (source
"code", provider "weaverbird-assets"); the games use the assets
when they are present and fall back to code-drawn shapes when they
are not. The stage adds a zero-token ledger entry.
"""

from __future__ import annotations

import json
import math
import struct
import wave
from datetime import datetime, timezone
from pathlib import Path

from . import generate as generate_module
from . import ledger as ledger_module
from . import provenance as provenance_module
from . import scope as scope_module
from . import skeleton as skeleton_module
from . import state as state_module

GAME_DIR_NAME = skeleton_module.GAME_DIR_NAME
REPORT_NAME = "assets_report.json"
PROVENANCE_PROVIDER = "weaverbird-assets"
SAMPLE_RATE = 22050


class AssetsError(Exception):
    """Raised when asset generation is refused."""


# ---------------------------------------------------------------------------
# SVG art (flat, rounded, minimal — the platform identity as game art)

def _svg(body: str, size: int = 64, view: str | None = None) -> str:
    view = view or f"0 0 {size} {size}"
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{size}" '
            f'height="{size}" viewBox="{view}">{body}</svg>\n')


def _sprite_player(p: dict) -> str:
    accent, dark = p["accent_hex"], p["platform_hex"]
    return _svg(
        f'<circle cx="32" cy="34" r="22" fill="{accent}"/>'
        f'<circle cx="32" cy="40" r="13" fill="#FFFFFF" opacity="0.28"/>'
        f'<circle cx="25" cy="30" r="4" fill="#FFFFFF"/>'
        f'<circle cx="39" cy="30" r="4" fill="#FFFFFF"/>'
        f'<circle cx="25" cy="31" r="2" fill="{dark}"/>'
        f'<circle cx="39" cy="31" r="2" fill="{dark}"/>'
    )


def _sprite_target(p: dict) -> str:
    target = p["target_hex"]
    points = []
    for i in range(10):
        angle = -math.pi / 2 + i * math.pi / 5
        radius = 24 if i % 2 == 0 else 10.5
        points.append("%.1f,%.1f" % (32 + radius * math.cos(angle),
                                     32 + radius * math.sin(angle)))
    return _svg(
        f'<polygon points="{" ".join(points)}" fill="{target}"/>'
        f'<circle cx="32" cy="32" r="5" fill="#FFFFFF" opacity="0.85"/>'
    )


def _sprite_hazard(p: dict) -> str:
    hazard = p["hazard_hex"]
    return _svg(
        f'<rect x="10" y="10" width="44" height="44" rx="9" fill="{hazard}"/>'
        f'<path d="M22 22 L42 42 M42 22 L22 42" stroke="#FFFFFF" '
        f'stroke-width="5" stroke-linecap="round"/>'
    )


def _sprite_platform_tile(p: dict) -> str:
    platform, accent = p["platform_hex"], p["accent_hex"]
    return _svg(
        f'<rect x="0" y="0" width="64" height="64" fill="{platform}"/>'
        f'<rect x="0" y="0" width="64" height="10" fill="{accent}"/>'
        f'<rect x="0" y="54" width="64" height="10" fill="#000000" opacity="0.18"/>'
    )


def _sprite_spikes(p: dict) -> str:
    hazard = p["hazard_hex"]
    teeth = "".join(
        f'<polygon points="{i * 16},20 {i * 16 + 8},2 {i * 16 + 16},20" '
        f'fill="{hazard}"/>'
        for i in range(4)
    )
    return _svg(teeth, view="0 0 64 20")


def _sprite_exit_door(p: dict) -> str:
    accent, target = p["accent_hex"], p["target_hex"]
    return _svg(
        f'<rect x="14" y="6" width="36" height="52" rx="6" fill="{accent}"/>'
        f'<rect x="19" y="11" width="26" height="42" rx="4" fill="#FFFFFF" opacity="0.22"/>'
        f'<circle cx="43" cy="33" r="3.2" fill="{target}"/>'
    )


def _sprite_crate(p: dict) -> str:
    crate = p["crate_hex"]
    return _svg(
        f'<rect x="6" y="6" width="52" height="52" rx="6" fill="{crate}"/>'
        f'<rect x="6" y="6" width="52" height="52" rx="6" fill="none" '
        f'stroke="#000000" stroke-opacity="0.28" stroke-width="3"/>'
        f'<path d="M8 8 L56 56 M56 8 L8 56" stroke="#000000" '
        f'stroke-opacity="0.20" stroke-width="4"/>'
        f'<rect x="24" y="24" width="16" height="16" rx="3" fill="#FFFFFF" opacity="0.20"/>'
    )


def _sprite_pad(p: dict) -> str:
    target = p["target_hex"]
    return _svg(
        f'<rect x="9" y="9" width="46" height="46" rx="10" fill="none" '
        f'stroke="{target}" stroke-width="4"/>'
        f'<circle cx="32" cy="32" r="6" fill="{target}"/>'
    )


def _sprite_wall_tile(p: dict) -> str:
    platform = p["platform_hex"]
    return _svg(
        f'<rect x="0" y="0" width="64" height="64" fill="{platform}"/>'
        f'<rect x="3" y="3" width="58" height="58" rx="6" fill="none" '
        f'stroke="#FFFFFF" stroke-opacity="0.20" stroke-width="3"/>'
        f'<line x1="0" y1="32" x2="64" y2="32" stroke="#000000" stroke-opacity="0.20" stroke-width="3"/>'
    )


def _sprite_background(p: dict) -> str:
    accent = p["accent_hex"]
    dots = []
    for row in range(6):
        for col in range(10):
            cx = 60 + col * 116 + (28 if row % 2 else 0)
            cy = 58 + row * 108
            dots.append(f'<circle cx="{cx}" cy="{cy}" r="26" '
                        f'fill="{accent}" opacity="0.07"/>')
    body = ('<rect x="0" y="0" width="1152" height="648" fill="#101219"/>'
            + "".join(dots))
    return _svg(body, size=1152, view="0 0 1152 648")


def _icon(p: dict, archetype: str) -> str:
    accent, target, hazard = p["accent_hex"], p["target_hex"], p["hazard_hex"]
    crate = p["crate_hex"]
    if archetype == "platformer":
        motif = (
            f'<rect x="18" y="86" width="52" height="12" rx="5" fill="{accent}"/>'
            f'<rect x="52" y="60" width="52" height="12" rx="5" fill="{accent}" opacity="0.75"/>'
            f'<circle cx="82" cy="34" r="13" fill="{target}"/>'
        )
    elif archetype == "crate_pusher":
        motif = (
            f'<rect x="24" y="46" width="44" height="44" rx="7" fill="{crate}"/>'
            f'<path d="M26 48 L66 88 M66 48 L26 88" stroke="#000000" '
            f'stroke-opacity="0.22" stroke-width="4"/>'
            f'<rect x="70" y="70" width="34" height="34" rx="8" fill="none" '
            f'stroke="{target}" stroke-width="4"/>'
        )
    else:  # arena collector: the player dot chasing its target ring
        motif = (
            f'<circle cx="46" cy="70" r="17" fill="{accent}"/>'
            f'<circle cx="46" cy="70" r="6" fill="#FFFFFF" opacity="0.85"/>'
            f'<circle cx="84" cy="42" r="14" fill="none" stroke="{target}" stroke-width="6"/>'
            f'<rect x="96" y="82" width="15" height="15" rx="3" fill="{hazard}"/>'
        )
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" width="128" height="128" '
        'viewBox="0 0 128 128">'
        f'<rect x="4" y="4" width="120" height="120" rx="26" fill="#171A24"/>'
        f"{motif}</svg>\n"
    )


_SPRITES = {
    "player": _sprite_player,
    "target": _sprite_target,
    "hazard": _sprite_hazard,
    "platform_tile": _sprite_platform_tile,
    "spikes": _sprite_spikes,
    "exit_door": _sprite_exit_door,
    "crate": _sprite_crate,
    "pad": _sprite_pad,
    "wall": _sprite_wall_tile,
    "background": _sprite_background,
}


# ---------------------------------------------------------------------------
# Audio synthesis (stdlib wave): tones, sweeps, and a music loop

def _tone(freq: float, seconds: float, volume: float = 0.5,
          shape: str = "sine", sweep_to: float | None = None,
          harmonics: float = 0.0) -> list[float]:
    count = int(SAMPLE_RATE * seconds)
    samples = []
    for i in range(count):
        t = i / SAMPLE_RATE
        frac = i / max(count - 1, 1)
        f = freq if sweep_to is None else freq + (sweep_to - freq) * frac
        phase = 2 * math.pi * f * t if sweep_to is None else \
            2 * math.pi * (freq * t + (sweep_to - freq) * t * t / (2 * seconds))
        if shape == "square":
            value = 0.6 if math.sin(phase) >= 0 else -0.6
        else:
            value = math.sin(phase) + harmonics * math.sin(2 * phase)
        envelope = min(1.0, i / (0.008 * SAMPLE_RATE)) * \
            (1.0 - 0.65 * frac)
        samples.append(value * volume * envelope)
    return samples


def _mix(*tracks: list[float]) -> list[float]:
    length = max((len(t) for t in tracks), default=0)
    out = [0.0] * length
    for track in tracks:
        for i, value in enumerate(track):
            out[i] += value
    return out


def _sequence(notes: list[tuple[float, float, float]], volume=0.5,
              shape="sine") -> list[float]:
    """notes: (frequency, start_s, duration_s) — a tiny melody."""
    length = int(SAMPLE_RATE * max(s + d for _, s, d in notes)) if notes else 0
    out = [0.0] * length
    for freq, start, duration in notes:
        tone = _tone(freq, duration, volume, shape, harmonics=0.25)
        offset = int(start * SAMPLE_RATE)
        for i, value in enumerate(tone):
            if offset + i < length:
                out[offset + i] += value
    return out


def _write_wav(path: Path, samples: list[float]) -> None:
    frames = b"".join(
        struct.pack("<h", int(max(-1.0, min(1.0, s)) * 32767 * 0.9))
        for s in samples
    )
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(SAMPLE_RATE)
        wav.writeframes(frames)


def _audio_files() -> dict:
    """Generated audio: file name -> sample list."""
    return {
        "click.wav": _tone(1150.0, 0.06, 0.35),
        "collect.wav": _sequence([(880.0, 0.0, 0.09), (1318.5, 0.07, 0.14)], 0.45),
        "hurt.wav": _tone(220.0, 0.28, 0.5, "square", sweep_to=105.0),
        "jump.wav": _tone(300.0, 0.16, 0.4, sweep_to=720.0),
        "push.wav": _tone(82.0, 0.13, 0.7, harmonics=0.4),
        "win.wav": _sequence(
            [(523.25, 0.0, 0.13), (659.25, 0.11, 0.13),
             (783.99, 0.22, 0.13), (1046.5, 0.33, 0.3)], 0.45),
        "lose.wav": _sequence(
            [(392.0, 0.0, 0.2), (329.63, 0.17, 0.2), (261.63, 0.34, 0.38)], 0.45),
        "music.wav": _music_loop(),
    }


def _music_loop() -> list[float]:
    """A 8-second loop: a soft arpeggio over Am–F–C–G, built so the
    pattern ends where it begins and loops without a click."""
    roots = [110.0, 87.31, 130.81, 98.0]  # A2 F2 C3 G2
    notes: list[tuple[float, float, float]] = []
    step = 0.5
    for bar, root in enumerate(roots):
        chord = [root * 2, root * 2.5, root * 3, root * 4]
        for beat in range(4):
            freq = chord[beat % len(chord)]
            notes.append((freq, (bar * 4 + beat) * step, step * 0.96))
            notes.append((root, (bar * 4 + beat) * step, step * 0.96))
    samples = _sequence(notes, 0.16)
    target = int(SAMPLE_RATE * 8.0)
    if len(samples) < target:
        samples += [0.0] * (target - len(samples))
    return samples[:target]


# ---------------------------------------------------------------------------
# The stage itself

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


def generate_assets(project_dir, archetype: str | None = None) -> dict:
    """Generate the game's assets. Returns the assets report.

    Requires the skeleton game to exist (assets live inside it). The
    palette comes from the scope's style lock; the archetype from
    the scope, exactly as the build stage detects it (an explicit
    archetype, e.g. the one a build already chose, may be passed).
    """
    project = Path(project_dir)
    game_dir = project / GAME_DIR_NAME
    if not (game_dir / "project.godot").exists():
        raise AssetsError(
            "No skeleton game found — build the skeleton first "
            f"('weaverbird skeleton build {project}'); assets live "
            "inside the game it generates."
        )
    scope_file = scope_module.scope_path_for(project)
    if not scope_file.exists():
        raise AssetsError(f"No scope file found at {scope_file}.")
    parsed = scope_module.load_scope(scope_file)
    values = {f.name: f.value for f in parsed.fields}
    chosen, _why = generate_module.detect_archetype(
        values, override=archetype)
    palette = generate_module._palette(values)

    assets_dir = game_dir / "assets"
    audio_dir = assets_dir / "audio"
    assets_dir.mkdir(parents=True, exist_ok=True)
    audio_dir.mkdir(parents=True, exist_ok=True)

    written: list[str] = []
    notes: dict[str, str] = {}
    for name, maker in _SPRITES.items():
        rel = f"{GAME_DIR_NAME}/assets/{name}.svg"
        (assets_dir / f"{name}.svg").write_text(maker(palette), encoding="utf-8")
        written.append(rel)
        notes[rel] = f"{name} sprite drawn from the style-lock palette by the deterministic SVG generator"
    icon_rel = f"{GAME_DIR_NAME}/icon.svg"
    (game_dir / "icon.svg").write_text(_icon(palette, chosen), encoding="utf-8")
    written.append(icon_rel)
    notes[icon_rel] = f"game icon generated from the palette with the {chosen} motif"
    for name, samples in _audio_files().items():
        rel = f"{GAME_DIR_NAME}/assets/audio/{name}"
        _write_wav(audio_dir / name, samples)
        written.append(rel)
        notes[rel] = "procedurally synthesised audio (stdlib wave, no samples)"
    written.sort()

    for rel in written:
        provenance_module.add_record(
            project, rel, "code", provider=PROVENANCE_PROVIDER,
            note=notes[rel])
    ledger_module.add_entry(project, "assets", "cheap", 0, 0.0)

    report = {
        "stage": "assets",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "archetype": chosen,
        "palette": palette,
        "files": written,
        "icon": icon_rel,
        "icon_generated": True,
    }
    path = report_path_for(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report


# ---------------------------------------------------------------------------
# Pipeline wiring

def assets_stage_handler(project_dir, state=None) -> bool:
    """Handler for leaving the assets stage: assets must exist
    (generate them if they do not)."""
    project = Path(project_dir)
    report = load_report(project)
    if report and (project / GAME_DIR_NAME / "assets").is_dir():
        return True
    generate_assets(project)
    return True


def register_stage_handlers() -> None:
    state_module.register_handler("assets", assets_stage_handler)
