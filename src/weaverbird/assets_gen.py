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

import io
import json
import math
import re
import struct
import wave
from datetime import datetime, timezone
from pathlib import Path

from . import generate as generate_module
from . import ledger as ledger_module
from . import plan as plan_module
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


# Sprites are drawn on a 256-unit canvas and declared at 64px:
# the geometry is authored at 4x and the renderer downscales it
# to the display size, anti-aliased — the vector pipeline's
# supersampling. Every maker obeys the style lock's traits
# (palette["style"], see style_traits): an outline in the
# style's width and colour when the style has one (none when it
# doesn't — neon never outlines), two-tone shading per the
# style's model, and a glow rim where the style glows.

_CANVAS = "0 0 256 256"


def _mix_hex(hex_value: str, other: str, frac: float) -> str:
    """Blend two #RRGGBB colours, frac of the way toward other."""
    def rgb(value: str) -> tuple[int, int, int]:
        value = value.lstrip("#")
        return (int(value[0:2], 16), int(value[2:4], 16),
                int(value[4:6], 16))
    a, b = rgb(hex_value), rgb(other)
    return "#%02X%02X%02X" % tuple(
        round(a[i] + (b[i] - a[i]) * frac) for i in range(3))


def _tint(hex_value: str, frac: float = 0.35) -> str:
    return _mix_hex(hex_value, "#FFFFFF", frac)


def _shade(hex_value: str, frac: float = 0.30) -> str:
    return _mix_hex(hex_value, "#000000", frac)


def _traits(p: dict) -> dict:
    traits = p.get("style")
    return traits if isinstance(traits, dict) else _DEFAULT_TRAITS


def _ol(p: dict, scale: float = 1.0) -> str:
    """The outline attributes for a shape, per the style lock:
    a stroke in the style's colour at its width (authored at
    display scale, so x4 on this canvas) — or nothing."""
    traits = _traits(p)
    if not traits.get("outline_width") or not traits.get("outline_color"):
        return ""
    width = float(traits["outline_width"]) * 4.0 * scale
    return (f' stroke="{traits["outline_color"]}" '
            f'stroke-width="{width:.0f}"')


def _halo(colour: str, cx: float, cy: float, r: float,
          opacity: float = 0.16) -> str:
    return (f'<circle cx="{cx:.0f}" cy="{cy:.0f}" r="{r:.0f}" '
            f'fill="{colour}" opacity="{opacity}"/>'
            f'<circle cx="{cx:.0f}" cy="{cy:.0f}" r="{r * 0.78:.0f}" '
            f'fill="{colour}" opacity="{opacity + 0.06}"/>')


def _sparkle(cx: float, cy: float, r: float, colour: str,
             opacity: float = 0.95) -> str:
    """A four-point sparkle (a diamond star), the collectible's
    glint."""
    inner = r * 0.22
    points = (f"{cx:.0f},{cy - r:.0f} {cx + inner:.0f},{cy - inner:.0f} "
              f"{cx + r:.0f},{cy:.0f} {cx + inner:.0f},{cy + inner:.0f} "
              f"{cx:.0f},{cy + r:.0f} {cx - inner:.0f},{cy + inner:.0f} "
              f"{cx - r:.0f},{cy:.0f} {cx - inner:.0f},{cy - inner:.0f}")
    return (f'<polygon points="{points}" fill="{colour}" '
            f'opacity="{opacity}"/>')


def _round_body(p: dict, base: str, cx: float, cy: float,
                r: float, clip_id: str = "body") -> str:
    """A round body with the style's two-tone shading: the base
    fill (with the style's outline, when it has one), then —
    clipped to the body — a darker shade crescent low on the
    shape and a lighter tint patch high on it. The shading
    model sets the strengths: cel is hard and full, tint is a
    whisper of each, soft (chunky) is broad and gentle, flat
    (neon) is none."""
    mode = _traits(p).get("shading", "tint")
    layers = ""
    if mode != "flat":
        shade_op = {"cel": 0.9, "tint": 0.55, "soft": 0.5}.get(mode, 0.55)
        tint_op = {"cel": 0.9, "tint": 0.5, "soft": 0.35}.get(mode, 0.5)
        layers = (
            f'<g clip-path="url(#{clip_id})">'
            f'<circle cx="{cx:.0f}" cy="{cy + r * 0.52:.0f}" '
            f'r="{r:.0f}" fill="{_shade(base, 0.32)}" '
            f'opacity="{shade_op}"/>'
            f'<ellipse cx="{cx - r * 0.34:.0f}" '
            f'cy="{cy - r * 0.40:.0f}" rx="{r * 0.54:.0f}" '
            f'ry="{r * 0.36:.0f}" fill="{_tint(base, 0.45)}" '
            f'opacity="{tint_op}"/></g>')
    return (
        f'<defs><clipPath id="{clip_id}">'
        f'<circle cx="{cx:.0f}" cy="{cy:.0f}" r="{r:.0f}"/>'
        f'</clipPath></defs>'
        f'<circle cx="{cx:.0f}" cy="{cy:.0f}" r="{r:.0f}" '
        f'fill="{base}"{_ol(p)}/>' + layers)


def _sprite_player(p: dict) -> str:
    accent = p["accent_hex"]
    body = ""
    if _traits(p).get("glow"):
        body += _halo(accent, 128, 128, 116)
    body += (f'<ellipse cx="128" cy="222" rx="72" ry="13" '
             f'fill="#000000" opacity="0.16"/>')
    body += _round_body(p, accent, 128, 128, 88, "clip-player")
    # The directional cue: a visor band across the face with its
    # bright slit pushed to the facing side — the player reads
    # as looking (and so moving) that way.
    visor = _shade(accent, 0.62)
    body += (f'<rect x="62" y="98" width="132" height="44" rx="22" '
             f'fill="{visor}"/>'
             f'<rect x="98" y="110" width="76" height="19" rx="9" '
             f'fill="#FFFFFF" opacity="0.92"/>'
             f'<circle cx="188" cy="119" r="6" fill="#FFFFFF" '
             f'opacity="0.75"/>')
    return _svg(body, view=_CANVAS)


def _star_points(cx: float, cy: float, outer: float, inner: float,
                 dy: float = 0.0) -> str:
    points = []
    for i in range(10):
        angle = -math.pi / 2 + i * math.pi / 5
        radius = outer if i % 2 == 0 else inner
        points.append("%.1f,%.1f" % (cx + radius * math.cos(angle),
                                     cy + dy + radius * math.sin(angle)))
    return " ".join(points)


def _sprite_target(p: dict) -> str:
    target = p["target_hex"]
    body = _halo(target, 128, 128, 120)  # collectibles always glow
    body += (f'<polygon points="{_star_points(128, 128, 96, 42, 9)}" '
             f'fill="{_shade(target, 0.30)}"/>')
    body += (f'<polygon points="{_star_points(128, 128, 96, 42)}" '
             f'fill="{target}"{_ol(p)}/>')
    if _traits(p).get("shading") != "flat":
        body += (f'<polygon points="{_star_points(128, 122, 52, 23)}" '
                 f'fill="{_tint(target, 0.40)}" opacity="0.75"/>')
    body += (f'<circle cx="128" cy="128" r="19" fill="#FFFFFF" '
             f'opacity="0.88"/>')
    body += _sparkle(198, 52, 17, "#FFFFFF")
    body += _sparkle(50, 192, 11, "#FFFFFF", 0.7)
    return _svg(body, view=_CANVAS)


def _sprite_hazard(p: dict) -> str:
    hazard = p["hazard_hex"]
    body = ""
    if _traits(p).get("glow"):
        body += _halo(hazard, 128, 128, 122, 0.13)
    # Menace, first read: spikes radiating in the hazard colour.
    for i in range(8):
        angle = i * math.pi / 4 + math.pi / 8
        tip = (128 + 116 * math.cos(angle), 128 + 116 * math.sin(angle))
        left = (128 + 66 * math.cos(angle - 0.30),
                128 + 66 * math.sin(angle - 0.30))
        right = (128 + 66 * math.cos(angle + 0.30),
                 128 + 66 * math.sin(angle + 0.30))
        body += (f'<polygon points="{left[0]:.0f},{left[1]:.0f} '
                 f'{tip[0]:.0f},{tip[1]:.0f} '
                 f'{right[0]:.0f},{right[1]:.0f}" fill="{hazard}"/>')
    body += (f'<circle cx="128" cy="128" r="76" '
             f'fill="{_shade(hazard, 0.28)}"/>')
    body += (f'<circle cx="128" cy="122" r="70" fill="{hazard}"'
             f'{_ol(p)}/>')
    # Menace, second read: angry eyes — white eyes, pupils
    # glaring inward and down, brows slanted over them.
    dark = _shade(hazard, 0.62)
    body += (f'<circle cx="102" cy="118" r="14" fill="#FFFFFF"/>'
             f'<circle cx="154" cy="118" r="14" fill="#FFFFFF"/>'
             f'<circle cx="106" cy="122" r="7" fill="{dark}"/>'
             f'<circle cx="150" cy="122" r="7" fill="{dark}"/>'
             f'<path d="M84 96 L120 106 M172 96 L136 106" '
             f'stroke="{dark}" stroke-width="10" '
             f'stroke-linecap="round"/>')
    return _svg(body, view=_CANVAS)


def _sprite_platform_tile(p: dict) -> str:
    platform, accent = p["platform_hex"], p["accent_hex"]
    return _svg(
        f'<rect x="0" y="0" width="256" height="256" fill="{platform}"/>'
        f'<rect x="0" y="0" width="256" height="40" fill="{accent}"/>'
        f'<rect x="0" y="40" width="256" height="26" fill="{_tint(platform, 0.30)}"/>'
        f'<rect x="0" y="216" width="256" height="40" fill="{_shade(platform, 0.30)}"/>'
        f'<rect x="0" y="0" width="256" height="256" fill="none"{_ol(p)}/>',
        view=_CANVAS)


def _sprite_spikes(p: dict) -> str:
    hazard = p["hazard_hex"]
    teeth = ""
    for i in range(4):
        x = i * 64
        teeth += (f'<polygon points="{x},80 {x + 32},8 {x + 64},80" '
                  f'fill="{_shade(hazard, 0.30)}"/>'
                  f'<polygon points="{x},72 {x + 32},8 {x + 60},72" '
                  f'fill="{hazard}"{_ol(p, 0.7)}/>')
    return _svg(teeth + f'<rect x="0" y="72" width="256" height="8" '
                        f'fill="{_shade(hazard, 0.45)}"/>',
                view="0 0 256 80")


def _sprite_exit_door(p: dict) -> str:
    accent, target = p["accent_hex"], p["target_hex"]
    return _svg(
        f'<rect x="56" y="24" width="144" height="208" rx="24" '
        f'fill="{accent}"/>'
        f'<rect x="56" y="188" width="144" height="44" rx="22" '
        f'fill="{_shade(accent, 0.30)}"/>'
        f'<rect x="76" y="44" width="104" height="168" rx="16" '
        f'fill="{_tint(accent, 0.28)}" opacity="0.8"/>'
        f'<circle cx="172" cy="132" r="13" fill="{target}"/>'
        f'<rect x="56" y="24" width="144" height="208" rx="24" '
        f'fill="none"{_ol(p)}/>',
        view=_CANVAS)


def _sprite_crate(p: dict) -> str:
    crate = p["crate_hex"]
    return _svg(
        f'<rect x="24" y="24" width="208" height="208" rx="24" '
        f'fill="{crate}"/>'
        f'<rect x="24" y="24" width="208" height="52" rx="24" '
        f'fill="{_tint(crate, 0.30)}"/>'
        f'<rect x="24" y="180" width="208" height="52" rx="24" '
        f'fill="{_shade(crate, 0.30)}"/>'
        f'<path d="M32 32 L224 224 M224 32 L32 224" '
        f'stroke="{_shade(crate, 0.45)}" stroke-opacity="0.55" '
        f'stroke-width="14"/>'
        f'<rect x="96" y="96" width="64" height="64" rx="12" '
        f'fill="{_tint(crate, 0.38)}"/>'
        f'<rect x="24" y="24" width="208" height="208" rx="24" '
        f'fill="none"{_ol(p)}/>',
        view=_CANVAS)


def _sprite_pad(p: dict) -> str:
    target = p["target_hex"]
    body = ""
    if _traits(p).get("glow"):
        body += _halo(target, 128, 128, 120, 0.13)
    body += (
        f'<rect x="36" y="36" width="184" height="184" rx="40" '
        f'fill="none" stroke="{target}" stroke-width="17"/>'
        f'<rect x="36" y="36" width="184" height="184" rx="40" '
        f'fill="none"{_ol(p, 0.6)}/>'
        f'<circle cx="128" cy="128" r="25" fill="{_tint(target, 0.25)}"/>')
    return _svg(body, view=_CANVAS)


def _sprite_wall_tile(p: dict) -> str:
    platform = p["platform_hex"]
    return _svg(
        f'<rect x="0" y="0" width="256" height="256" fill="{platform}"/>'
        f'<rect x="0" y="0" width="256" height="30" fill="{_tint(platform, 0.28)}"/>'
        f'<rect x="0" y="226" width="256" height="30" fill="{_shade(platform, 0.30)}"/>'
        f'<rect x="12" y="12" width="232" height="232" rx="24" '
        f'fill="none" stroke="#FFFFFF" stroke-opacity="0.20" '
        f'stroke-width="10"/>'
        f'<line x1="0" y1="128" x2="256" y2="128" '
        f'stroke="{_shade(platform, 0.42)}" stroke-width="10"/>',
        view=_CANVAS)


def _sprite_background(p: dict) -> str:
    """The menu/world backdrop: a palette ground (the platform
    colour, deepened) with a subtle pattern in the accent, the
    pattern family set by the style lock — dots, stars or a
    grid. Never the flat black it used to be: the ground belongs
    to the game's own palette."""
    accent, platform = p["accent_hex"], p["platform_hex"]
    ground = _shade(platform, 0.45)
    pattern = _traits(p).get("pattern", "dots")
    marks = []
    if pattern == "grid":
        for x in range(96, 1152, 96):
            marks.append(f'<line x1="{x}" y1="0" x2="{x}" y2="648" '
                         f'stroke="{accent}" stroke-width="2" '
                         f'opacity="0.055"/>')
        for y in range(96, 648, 96):
            marks.append(f'<line x1="0" y1="{y}" x2="1152" y2="{y}" '
                         f'stroke="{accent}" stroke-width="2" '
                         f'opacity="0.055"/>')
    elif pattern == "stars":
        for i in range(46):
            cx = (i * 173 + 61) % 1152
            cy = (i * 269 + 47) % 648
            marks.append(_sparkle(cx, cy, 9 + (i % 3) * 3, accent,
                                  0.10))
    else:  # dots
        for row in range(6):
            for col in range(10):
                cx = 60 + col * 116 + (28 if row % 2 else 0)
                cy = 58 + row * 108
                marks.append(f'<circle cx="{cx}" cy="{cy}" r="26" '
                             f'fill="{accent}" opacity="0.07"/>')
    body = (f'<rect x="0" y="0" width="1152" height="648" '
            f'fill="{ground}"/>' + "".join(marks))
    return _svg(body, size=1152, view="0 0 1152 648")


def _icon(p: dict, archetype: str) -> str:
    """The game's icon composes the game's own motifs — the
    player and the main collectible, drawn by the same sprite
    code that draws them in the game — on a ground from the
    palette. Not a generic shape."""
    ground = _shade(p["platform_hex"], 0.18)
    player = _embed_svg(_sprite_player(p), 8, 52, 66)
    target = _embed_svg(_sprite_target(p), 58, 10, 62)
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" width="128" height="128" '
        'viewBox="0 0 128 128">'
        f'<rect x="4" y="4" width="120" height="120" rx="26" '
        f'fill="{ground}"/>'
        f"{player}{target}</svg>\n"
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

def _adsr(i: int, count: int, attack: float = 0.008,
          decay: float = 0.06, sustain: float = 0.55,
          release: float = 0.09) -> float:
    """An ADSR envelope's value at sample i of count: a fast
    attack, a decay to the sustain level, and a release tail
    that reaches zero exactly at the end — every generated
    sound is shaped with one, so nothing starts or stops with
    a click."""
    t = i / SAMPLE_RATE
    total = count / SAMPLE_RATE
    if t < attack:
        return t / attack if attack > 0 else 1.0
    if t < attack + decay:
        return 1.0 - (1.0 - sustain) * (t - attack) / decay \
            if decay > 0 else sustain
    if t > total - release:
        return sustain * max(0.0, (total - t) / release) \
            if release > 0 else 0.0
    return sustain


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
        samples.append(value * volume * _adsr(i, count))
    return samples


def _noise_burst(seconds: float, volume: float = 0.5,
                 seed: int = 20240, smooth: int = 6) -> list[float]:
    """A deterministic noise burst: a seeded PRNG (the same
    burst in every build), lightly smoothed so it reads as a
    thud of static rather than digital harshness, under a
    fast-decay envelope."""
    import random
    rng = random.Random(seed)
    count = int(SAMPLE_RATE * seconds)
    raw = [rng.uniform(-1.0, 1.0) for _ in range(count)]
    samples = []
    for i in range(count):
        window = raw[max(0, i - smooth):i + 1]
        value = sum(window) / len(window)
        frac = i / max(count - 1, 1)
        samples.append(value * volume * (1.0 - frac) ** 1.6
                       * min(1.0, i / (0.004 * SAMPLE_RATE)))
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


def _wav_bytes(samples: list[float]) -> bytes:
    frames = b"".join(
        struct.pack("<h", int(max(-1.0, min(1.0, s)) * 32767 * 0.9))
        for s in samples
    )
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(SAMPLE_RATE)
        wav.writeframes(frames)
    return buffer.getvalue()


def _write_wav(path: Path, samples: list[float]) -> None:
    path.write_bytes(_wav_bytes(samples))


def _audio_files(mood: dict | None = None) -> dict:
    """Generated audio: file name -> sample list. Every sound
    is shaped synthesis in the scope's audio mood: the win
    jingle and the ambient pad are written in the mood's scale
    and key, at the mood's pace (see audio_mood)."""
    mood = mood or audio_mood({})
    root = mood["root"]
    scale = mood["scale"]
    step = mood["note_s"]

    def note(degree: int, octave: int = 1) -> float:
        return _mood_note(root * octave, scale[degree % len(scale)])

    chime = _sequence([(note(0, 2), 0.0, 0.10),
                       (note(2, 2), 0.07, 0.18)], 0.45)
    hurt = _mix(_noise_burst(0.22, 0.5),
                _tone(96.0, 0.26, 0.55, sweep_to=52.0))
    win = _sequence(
        [(note(0, 2), 0.0, step * 1.15),
         (note(1, 2), step, step * 1.15),
         (note(2, 2), step * 2, step * 1.15),
         (note(4, 2), step * 3, step * 3.2)], 0.45,
        shape="sine" if mood["name"] != "energetic" else "square")
    lose = _sequence([(note(2, 1), 0.0, 0.20),
                      (note(0, 1), 0.18, 0.34)], 0.45)
    return {
        "click.wav": _tone(880.0, 0.045, 0.30),
        "collect.wav": chime,
        "hurt.wav": hurt,
        "jump.wav": _tone(300.0, 0.16, 0.4, sweep_to=720.0),
        "push.wav": _tone(82.0, 0.13, 0.7, harmonics=0.4),
        "win.wav": win,
        "lose.wav": lose,
        "music.wav": _music_loop(mood),
    }


def _music_loop(mood: dict | None = None) -> list[float]:
    """An 8-second ambient pad loop in the mood's key: two
    detuned sines on the root and one on the fifth, breathing
    through a slow tremolo, kept very quiet — a bed under the
    game, not a tune over it. The tremolo completes a whole
    number of cycles in the loop and the pad's phase is
    continuous, so the loop point is seamless."""
    mood = mood or audio_mood({})
    root = mood["pad_root"]
    seconds = 8.0
    count = int(SAMPLE_RATE * seconds)
    fifth = root * 1.5
    samples = []
    for i in range(count):
        t = i / SAMPLE_RATE
        tremolo = 0.72 + 0.28 * math.sin(2 * math.pi * 0.25 * t)
        value = (math.sin(2 * math.pi * root * t)
                 + 0.85 * math.sin(2 * math.pi * root * 1.004 * t)
                 + 0.6 * math.sin(2 * math.pi * fifth * t))
        samples.append(value / 2.45 * 0.16 * tremolo)
    return samples


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


# Which sprites each entity kind needs, and which sounds the
# movement/fail kinds imply — the asset requests are derived
# from the composition and the world manifest (0.7.0), not from
# an archetype name.
_ENTITY_SPRITES = {
    "player": ["player"],
    "collectible": ["target"],
    "hazard": ["hazard", "spikes"],
    "platform": ["platform_tile"],
    "exit": ["exit_door"],
    "goal": ["exit_door"],
    "crate": ["crate"],
    "pushable": ["crate"],
    "pad": ["pad"],
    "zone": ["pad"],
    "wall": ["wall"],
    "obstacle": [],
    "ambient": [],
}
_MOVEMENT_AUDIO = {"platform": ["jump"], "grid": ["push"],
                   "side_view": ["jump"], "grid_step": ["push"]}
_FAIL_AUDIO = {"hazard_contact": ["hurt"], "hazard_contacts": ["hurt"],
               "timer": ["hurt"], "fall": ["hurt"],
               "lives": ["hurt"], "move_budget": ["push"]}


def _project_plan(project: Path, values: dict) -> tuple[dict, bool]:
    """(plan, is_legacy) for a project: the plan recorded in its
    world (or derived from its scope), and whether the project
    is a legacy build (world/config names a preset and no plan).
    """
    from . import world as world_module

    world_data = world_module.load_world(project)
    if world_data and isinstance(world_data.get("plan"), dict):
        return world_data["plan"], False
    config_path = project / GAME_DIR_NAME / "game_config.json"
    if config_path.exists():
        try:
            config = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            config = {}
        if isinstance(config.get("plan"), dict):
            return config["plan"], False
        if config.get("archetype"):
            return {}, True
    if world_data and world_data.get("preset"):
        return {}, True
    return plan_module.derive_plan(values), False


def asset_requests(project: Path, values: dict) -> dict:
    """The assets this game needs, derived from its game plan
    and — when the world worker has run — the entity kinds
    actually present in game/world/world.json:
    {sprites, audio, based_on, bespoke}.

    Legacy projects (built by older versions) keep the
    composition-based derivation they always had; bespoke games
    get exactly their requests — there is no set to fall back on.
    """
    from . import world as world_module

    plan, legacy = _project_plan(project, values)
    world_data = world_module.load_world(project)
    composition = None
    preset = None
    if legacy:
        derived = generate_module.derive_composition(
            values, override=None)
        bespoke = bool(derived["bespoke"])
        composition = derived["composition"] or {}
        preset = derived["preset"]
        dimension = composition.get("dimension", "2d")
        movement = composition.get("movement")
        fails = composition.get("fail") or []
    else:
        bespoke = plan.get("bespoke_required") is not None
        dimension = plan.get("dimension", "2d")
        movement = plan.get("movement")
        fails = plan.get("fail") or []
    kinds: list[str] = []
    based_on = "plan"
    if world_data and world_data.get("entity_kinds"):
        kinds = list(world_data["entity_kinds"])
        based_on = "world"
    elif legacy and composition:
        kinds = list(world_module.ENTITY_KINDS.get(preset, []))
    elif not legacy:
        kinds = plan_module.entity_kinds(plan)
    sprites = {"background"}
    for kind in kinds:
        sprites.update(_ENTITY_SPRITES.get(kind, []))
    if bespoke:
        sprites.update(["player", "target"])
    audio = {"click", "collect", "win", "lose", "music"}
    audio.update(_MOVEMENT_AUDIO.get(movement, []))
    for fail in fails:
        audio.update(_FAIL_AUDIO.get(fail, []))
    if bespoke:
        audio.update(["hurt"])
    materials: list[dict] = []
    if dimension == "3d":
        # A 3D game has no sprites to draw: its look is materials
        # defined in code from the palette. The assets worker's
        # product is the material palette spec the mechanics
        # worker consumes (via game_config.json's palette), and
        # the report records it — honestly, as data, not files.
        sprites = set()
        palette = generate_module._palette(values)
        materials = [
            {"name": "player", "hex": palette["accent_hex"],
             "used_for": "the player's capsule",
             "finish": "capsule with a dark visor band and a "
                       "bright facing slit"},
            {"name": "collectible", "hex": palette["target_hex"],
             "used_for": "the collectible orbs",
             "finish": "emissive — the orbs glow in their own "
                       "colour, with a brighter inner core; "
                       "roughness varies per instance (0.25–0.45) "
                       "by the world data's variant seed"},
            {"name": "hazard", "hex": palette["hazard_hex"],
             "used_for": "the patrolling hazards",
             "finish": "a spiked form — cones radiating from a "
                       "core — so the menace reads in silhouette"},
            {"name": "ground", "hex": palette["platform_hex"],
             "used_for": "the ground plane"},
            {"name": "obstacle", "hex": palette["crate_hex"],
             "used_for": "the obstacle blocks"},
        ]
    return {
        "sprites": sorted(sprites),
        "audio": sorted(audio),
        "based_on": based_on,
        "bespoke": bespoke,
        "plan": ({"movement": plan.get("movement"),
                  "objective": plan.get("objective")}
                 if plan else None),
        "composition": composition,
        "preset": preset,
        "dimension": dimension,
        "materials": materials,
    }


def _preflight(project: Path,
               archetype: str | None = None) -> tuple[dict, str, dict]:
    """(palette, motif key, scope values) for a project whose
    skeleton game exists — the shared start of every asset
    operation. The motif key is the plan's movement for
    genre-agnostic builds, or the legacy build name for old
    projects (kept for the icon motif); asset selection itself
    goes through asset_requests()."""
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
    if archetype:
        chosen = generate_module._normalise_archetype(archetype)
    else:
        plan, legacy = _project_plan(project, values)
        if legacy:
            derived = generate_module.derive_composition(values)
            chosen = derived["preset"] or "arena_collector"
        else:
            chosen = plan.get("movement") or "top_down_2d"
    return generate_module._palette(values), chosen, values


def _producers(palette: dict, archetype: str,
               requests: dict | None = None,
               values: dict | None = None) -> dict:
    """Every generated asset, by name: {name: (relative path,
    produce() -> bytes, provenance note, kind)}.

    Each asset is produced by a named generator — the sprite
    makers above, the icon maker, or a synthesiser for one audio
    file — so the asset studio can redraw exactly one file
    without touching the others. The registry is the single
    source of asset names and kinds: the studio and the CLI read
    it as data and never hardcode a per-game asset list. (The
    current generators are the built-in set; a future mechanics
    layer registers its own producers the same way.)
    """
    bespoke = bool(requests and requests.get("bespoke"))
    is_3d = bool(requests and requests.get("dimension") == "3d")
    values = values or {}
    # The drawing palette carries the style lock's traits, so
    # every sprite obeys the same outline/shading/glow rules.
    draw = dict(palette)
    draw["style"] = traits_for_values(values)
    mood = audio_mood(values)
    producers: dict[str, tuple[str, object, str, str]] = {}
    for name, maker in _SPRITES.items():
        if bespoke and name not in requests["sprites"]:
            continue
        if is_3d:
            # A 3D game's look is materials defined in code from
            # the palette (see asset_requests' material spec) —
            # no sprites are produced for it.
            continue
        producers[name] = (
            f"{GAME_DIR_NAME}/assets/{name}.svg",
            (lambda maker=maker: maker(draw).encode("utf-8")),
            f"{name} sprite drawn from the style-lock palette by "
            "the deterministic SVG generator, obeying the style "
            "lock's outline, shading and glow rules",
            "sprite")
    producers["icon"] = (
        f"{GAME_DIR_NAME}/icon.svg",
        (lambda: _icon(draw, archetype).encode("utf-8")),
        "game icon composed from the game's own player and "
        "collectible sprites on a ground from the palette",
        "icon")
    for filename, samples in _audio_files(mood).items():
        stem = filename.rsplit(".", 1)[0]
        if bespoke and stem not in requests["audio"]:
            continue
        producers[stem] = (
            f"{GAME_DIR_NAME}/assets/audio/{filename}",
            (lambda samples=samples: _wav_bytes(samples)),
            "procedurally synthesised audio (stdlib wave, no "
            f"samples), {mood['name']} mood, "
            f"{mood['scale_name']} scale",
            "audio")
    return producers


def generate_assets(project_dir, archetype: str | None = None,
                    reporter=None) -> dict:
    """Generate the game's assets. Returns the assets report.

    Requires the skeleton game to exist (assets live inside it). The
    palette comes from the scope's style lock; the archetype from
    the scope, exactly as the build stage detects it (an explicit
    archetype, e.g. the one a build already chose, may be passed).
    ``reporter`` (optional) narrates the step boundaries for the
    job runner.
    """
    project = Path(project_dir)
    game_dir = project / GAME_DIR_NAME
    palette, chosen, values = _preflight(project, archetype)

    assets_dir = game_dir / "assets"
    audio_dir = assets_dir / "audio"
    assets_dir.mkdir(parents=True, exist_ok=True)
    audio_dir.mkdir(parents=True, exist_ok=True)

    requests = asset_requests(project, values)
    producers = _producers(palette, chosen, requests, values)
    if reporter is not None:
        reporter(f"Generating assets for the game — palette "
                 f"{palette['accent_hex']} / {palette['target_hex']} "
                 "from the style lock", done=0, total=len(producers))
    written: list[str] = []
    notes: dict[str, str] = {}
    for name, (rel, produce, note, _kind) in producers.items():
        dest = project / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(produce())
        written.append(rel)
        notes[rel] = note
        if reporter is not None:
            reporter(f"Generated {rel}", done=len(written),
                     total=len(producers))
    written.sort()

    for rel in written:
        provenance_module.add_record(
            project, rel, "code", provider=PROVENANCE_PROVIDER,
            note=notes[rel])
    ledger_module.add_entry(project, "assets", "cheap", 0, 0.0)

    report = {
        "stage": "assets",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "dimension": requests.get("dimension", "2d"),
        "requests": {"sprites": requests["sprites"],
                     "audio": requests["audio"],
                     "based_on": requests["based_on"],
                     "materials": requests.get("materials", [])},
        "palette": palette,
        "style_traits": traits_for_values(values),
        "audio_mood": audio_mood(values),
        "files": written,
        "icon": f"{GAME_DIR_NAME}/icon.svg",
        "icon_generated": True,
    }
    if requests.get("preset"):
        # Legacy build: keep the historical report keys.
        report["archetype"] = chosen
        report["composition"] = requests["composition"]
        report["preset"] = requests["preset"]
    else:
        report["plan"] = requests.get("plan")
    path = report_path_for(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report


# ---------------------------------------------------------------------------
# Style samples — the style-lock decision, shown not described
#
# When the scope interview reaches the style lock, the maker does
# not pick from adjectives: the factory GENERATES a small sample
# card per candidate style direction — a tiny scene (sky, sun,
# hills, a tree, the player, a target, a crate) drawn by this
# module's own sprite code from the candidate's palette. Picking a
# card writes its palette and rules into the scope (see
# interview.py); nothing here is a static image shipped in the
# package — the SVGs are produced from these definitions at the
# moment they are shown, and cached under the project.

STYLE_SAMPLES: list[dict] = [
    {
        "id": "bright-chunky",
        "name": "Bright & chunky",
        "blurb": "Saturated colour, big rounded shapes, soft "
                 "shadows — playful and loud.",
        "palette": {"accent_hex": "#FF7A1A", "target_hex": "#2FBF71",
                    "hazard_hex": "#E63B3B", "platform_hex": "#7A4E21",
                    "crate_hex": "#C98A3D"},
        "rules": "Chunky proportions (shapes about 1.4x natural "
                 "scale), no outlines, soft drop shadows, every "
                 "corner rounded.",
        "traits": {"outline_width": 0.0, "outline_color": None,
                   "shading": "soft", "glow": False,
                   "pattern": "dots"},
        "sky": "#FFE9B8", "ground": "#8FD14F", "sun": "#FFC93D",
    },
    {
        "id": "soft-pastel",
        "name": "Soft pastel",
        "blurb": "Gentle tints, thin warm-grey outlines, calm flat "
                 "shading — quiet and friendly.",
        "palette": {"accent_hex": "#9B8CF2", "target_hex": "#7FD6A4",
                    "hazard_hex": "#F28B8B", "platform_hex": "#B8A99A",
                    "crate_hex": "#D9B98A"},
        "rules": "Soft natural proportions, thin 2px outlines in "
                 "warm grey, flat shading with one lighter tint "
                 "per shape.",
        "traits": {"outline_width": 2.0, "outline_color": "#8A7F72",
                   "shading": "tint", "glow": False,
                   "pattern": "grid"},
        "sky": "#FDF3F7", "ground": "#CDE7C0", "sun": "#FFE29A",
    },
    {
        "id": "bold-neon-night",
        "name": "Bold neon night",
        "blurb": "Dark world, glowing edges, electric colour — "
                 "arcade after dark.",
        "palette": {"accent_hex": "#22D3EE", "target_hex": "#A3E635",
                    "hazard_hex": "#F43F5E", "platform_hex": "#334155",
                    "crate_hex": "#7C3AED"},
        "rules": "Slim proportions, dark backgrounds, flat fills "
                 "with a bright glowing rim on every shape (a "
                 "light halo, never an outline).",
        "traits": {"outline_width": 0.0, "outline_color": None,
                   "shading": "flat", "glow": True,
                   "pattern": "stars"},
        "sky": "#0B1026", "ground": "#16213B", "sun": "#E2E8F0",
    },
    {
        "id": "warm-storybook",
        "name": "Warm storybook",
        "blurb": "Hand-drawn warmth, dark-brown outlines, two-tone "
                 "cel shading — a picture book come loose.",
        "palette": {"accent_hex": "#D9622B", "target_hex": "#5B8C3E",
                    "hazard_hex": "#B3392E", "platform_hex": "#6B4A2F",
                    "crate_hex": "#A9713B"},
        "rules": "Slightly irregular hand-drawn proportions, 3px "
                 "dark-brown outlines, two-tone cel shading.",
        "traits": {"outline_width": 3.0, "outline_color": "#3E2A1E",
                   "shading": "cel", "glow": False,
                   "pattern": "dots"},
        "sky": "#FBEED3", "ground": "#7FA653", "sun": "#F6B352",
    },
]

# The style lock, as machine-readable drawing rules. The samples'
# "rules" prose is what the maker reads; these traits are what
# the drawing code obeys — outline (width in display px, colour)
# or none, the shading model, whether shapes carry a glow rim,
# and the background pattern family. A scope that names no known
# sample gets the quiet default: a thin outline, one tint,
# dots.
_DEFAULT_TRAITS = {"outline_width": 2.0, "outline_color": "#4A4238",
                   "shading": "tint", "glow": False,
                   "pattern": "dots"}


def style_traits(sample_id: str | None) -> dict:
    """The drawing traits of one style sample (or the default)."""
    for sample in STYLE_SAMPLES:
        if sample["id"] == sample_id:
            return dict(sample["traits"])
    return dict(_DEFAULT_TRAITS)


def traits_for_values(values: dict) -> dict:
    """The drawing traits for a project's scope: the traits of
    the sample the style lock names (the scope field holds the
    sample id, possibly among other words)."""
    text = (values.get(
        "Style sample chosen (id / link to approved sample)")
        or "").lower()
    for sample in STYLE_SAMPLES:
        if sample["id"] in text:
            return dict(sample["traits"])
    return dict(_DEFAULT_TRAITS)


def audio_mood(values: dict) -> dict:
    """The sound design for a project's scope, from its REQUIRED
    Audio mood field: a scale, a root and a pace. Calm scopes
    get a slow major pentatonic in a low key; energetic scopes a
    faster, brighter one; anything else a plain bright major.
    The choice is recorded in the assets report and the audio
    provenance notes — never silently defaulted in the files."""
    text = (values.get("Audio mood (music + SFX character)")
            or "").lower()
    if any(word in text for word in
           ("calm", "quiet", "gentle", "soft", "peaceful", "chill",
            "relax")):
        return {"name": "calm", "scale_name": "major pentatonic",
                "scale": [0, 2, 4, 7, 9], "root": 130.81,
                "note_s": 0.22, "pad_root": 65.41}
    if any(word in text for word in
           ("energetic", "upbeat", "intense", "driving", "fast",
            "exciting", "punchy", "arcade")):
        return {"name": "energetic", "scale_name": "major pentatonic",
                "scale": [0, 2, 4, 7, 9], "root": 196.0,
                "note_s": 0.09, "pad_root": 98.0}
    return {"name": "bright", "scale_name": "major pentatonic",
            "scale": [0, 2, 4, 7, 9], "root": 164.81,
            "note_s": 0.13, "pad_root": 82.41}


def _mood_note(freq: float, semitones: int) -> float:
    return freq * (2.0 ** (semitones / 12.0))

# Palette roles in scope order — the scope's Palette field pins
# roles by position (see generate._palette), so a style's palette
# text lists its hexes exactly in this order.
_SAMPLE_ROLE_ORDER = ("accent_hex", "target_hex", "hazard_hex",
                      "platform_hex", "crate_hex")
_SAMPLE_ROLE_NAMES = ("accent", "target", "hazard", "platform", "crate")


def style_samples() -> list[dict]:
    """The candidate style directions for the style lock."""
    return [dict(sample, palette=dict(sample["palette"]))
            for sample in STYLE_SAMPLES]


def style_sample_by_id(sample_id: str) -> dict | None:
    for sample in STYLE_SAMPLES:
        if sample["id"] == sample_id:
            return dict(sample, palette=dict(sample["palette"]))
    return None


def sample_palette_text(sample: dict) -> str:
    """The Palette field value a style pick writes: the five role
    hexes in role order, with their names."""
    palette = sample["palette"]
    return ", ".join(
        f"{name} {palette[key]}"
        for name, key in zip(_SAMPLE_ROLE_NAMES, _SAMPLE_ROLE_ORDER))


def _embed_svg(svg_text: str, x: int, y: int, size: int) -> str:
    """Nest one generated sprite SVG inside a scene at (x, y)."""
    match = re.match(r'<svg[^>]*viewBox="([^"]+)"[^>]*>(.*)</svg>\s*$',
                     svg_text.strip(), re.DOTALL)
    if not match:
        return ""
    return (f'<svg x="{x}" y="{y}" width="{size}" height="{size}" '
            f'viewBox="{match.group(1)}">{match.group(2)}</svg>')


def style_sample_svg(sample: dict) -> str:
    """One sample card: a tiny scene drawn from the candidate
    palette by the sprite generators above — sky, sun, hills, a
    tree, and the player / target / crate sprites themselves."""
    palette = dict(sample["palette"])
    palette["style"] = dict(sample["traits"])
    player = _embed_svg(_sprite_player(palette), 28, 116, 58)
    target = _embed_svg(_sprite_target(palette), 238, 52, 46)
    crate = _embed_svg(_sprite_crate(palette), 138, 128, 50)
    body = (
        f'<rect x="0" y="0" width="320" height="200" fill="{sample["sky"]}"/>'
        f'<circle cx="272" cy="38" r="20" fill="{sample["sun"]}"/>'
        f'<ellipse cx="70" cy="176" rx="130" ry="42" fill="{sample["ground"]}" opacity="0.65"/>'
        f'<ellipse cx="262" cy="184" rx="140" ry="46" fill="{sample["ground"]}" opacity="0.8"/>'
        f'<rect x="0" y="158" width="320" height="42" fill="{sample["ground"]}"/>'
        f'<rect x="102" y="104" width="9" height="34" fill="{palette["crate_hex"]}"/>'
        f'<circle cx="106" cy="92" r="24" fill="{palette["target_hex"]}"/>'
        f'<circle cx="88" cy="104" r="16" fill="{palette["target_hex"]}" opacity="0.8"/>'
        f"{player}{target}{crate}"
    )
    return ('<svg xmlns="http://www.w3.org/2000/svg" width="320" '
            f'height="200" viewBox="0 0 320 200">{body}</svg>\n')


# ---------------------------------------------------------------------------
# The asset studio: list, regenerate one, edit the palette

def asset_names(project_dir) -> list[str]:
    """The names `regenerate_asset` accepts, sorted."""
    project = Path(project_dir)
    palette, chosen, values = _preflight(project)
    return sorted(_producers(palette, chosen,
                             asset_requests(project, values),
                             values))


def list_assets(project_dir) -> list[dict]:
    """Every generated asset with its provenance and file state:
    [{name, path, kind, regenerable, size, missing, source,
    provider, note}].

    The listing is data-driven: the generator registry says what
    can be redrawn (kind + regenerable), the assets manifest
    (.weaverbird/assets_report.json) and the provenance records
    say what the project claims to have. An asset the manifest
    or the log names but the registry no longer produces is
    still listed — flagged, not regenerable — so nothing a game
    ships is hidden from the studio.
    """
    project = Path(project_dir)
    palette, chosen, values = _preflight(project)
    producers = _producers(palette, chosen,
                           asset_requests(project, values),
                           values)
    records: dict[str, dict] = {}
    try:
        for record in provenance_module.list_records(project):
            records[str(record.get("asset", ""))] = record
    except provenance_module.ProvenanceError:
        records = {}
    listing = []
    covered: set[str] = set()
    for name in sorted(producers):
        rel, _produce, _note, kind = producers[name]
        covered.add(rel)
        dest = project / rel
        record = records.get(rel, {})
        listing.append({
            "name": name,
            "path": rel,
            "kind": kind,
            "regenerable": True,
            "size": dest.stat().st_size if dest.exists() else None,
            "missing": not dest.exists(),
            "source": record.get("source", ""),
            "provider": record.get("provider", ""),
            "note": record.get("note", ""),
            "recorded_at": record.get("recorded_at", ""),
        })
    extra_paths: list[str] = []
    manifest = load_report(project)
    if manifest:
        for rel in manifest.get("files", []):
            if rel not in covered and rel not in extra_paths:
                extra_paths.append(rel)
    for rel in sorted(records):
        # Provenance covers every file the factory writes
        # (scripts, scenes, project files) — only asset-shaped
        # paths belong in the studio listing.
        asset_shaped = rel.startswith(f"{GAME_DIR_NAME}/assets/") \
            or rel == f"{GAME_DIR_NAME}/icon.svg"
        if asset_shaped and rel not in covered \
                and rel not in extra_paths:
            extra_paths.append(rel)
    for rel in extra_paths:
        dest = project / rel
        record = records.get(rel, {})
        suffix = Path(rel).suffix.lower()
        kind = {".svg": "image", ".wav": "audio"}.get(suffix, "file")
        listing.append({
            "name": Path(rel).stem,
            "path": rel,
            "kind": kind,
            "regenerable": False,
            "size": dest.stat().st_size if dest.exists() else None,
            "missing": not dest.exists(),
            "source": record.get("source", ""),
            "provider": record.get("provider", ""),
            "note": record.get("note", "")
                    or "On record, but no current generator "
                       "produces this asset.",
            "recorded_at": record.get("recorded_at", ""),
        })
    return listing


def regenerate_asset(project_dir, name: str, reporter=None) -> dict:
    """Redraw exactly one generated asset from the current
    palette (audio is re-synthesised), leaving every other file
    untouched, and record the regeneration in the provenance log
    (a new record for that asset; the rest of the log is
    untouched). Zero tokens. Returns {name, path, size}."""
    project = Path(project_dir)
    palette, chosen, values = _preflight(project)
    producers = _producers(palette, chosen,
                           asset_requests(project, values),
                           values)
    if name not in producers:
        raise AssetsError(
            f"Unknown asset {name!r}. The generated assets are: "
            + ", ".join(sorted(producers)) + ".")
    rel, produce, note, _kind = producers[name]
    if reporter is not None:
        reporter(f"Regenerating {rel} from the current palette",
                 done=0, total=1)
    dest = project / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    data = produce()
    dest.write_bytes(data)
    provenance_module.add_record(
        project, rel, "code", provider=PROVENANCE_PROVIDER,
        note=note + " — regenerated in the asset studio")
    if reporter is not None:
        reporter(f"Regenerated {rel} and recorded it in the "
                 "provenance log", done=1, total=1)
    return {"name": name, "path": rel, "size": len(data)}


# Palette roles, in scope order: the scope's Palette field pins
# them by position (accent, target, hazard, platform, crate);
# generate._palette derives any role the field does not pin.
PALETTE_ROLES = (
    ("accent", "Accent", "accent_hex"),
    ("target", "Target", "target_hex"),
    ("hazard", "Hazard", "hazard_hex"),
    ("platform", "Platform", "platform_hex"),
    ("crate", "Crate", "crate_hex"),
)

_HEX_INPUT_RE = re.compile(r"#?[0-9a-fA-F]{6}\Z")


def normalise_hex(value: str) -> str:
    """'#rrggbb' in any case → '#RRGGBB'. Raises AssetsError."""
    text = str(value or "").strip()
    if not _HEX_INPUT_RE.match(text):
        raise AssetsError(f"{value!r} is not a six-digit hex colour.")
    return "#" + text.lstrip("#").upper()


def palette_state(project_dir) -> dict:
    """The palette editor's view: the effective palette, each
    role's current hex, and whether the scope pins that role."""
    project = Path(project_dir)
    scope_file = scope_module.scope_path_for(project)
    if not scope_file.exists():
        raise AssetsError(f"No scope file found at {scope_file}.")
    parsed = scope_module.load_scope(scope_file)
    values = {f.name: f.value for f in parsed.fields}
    palette = generate_module._palette(values)
    pinned = generate_module._palette_field_hexes(values)
    roles = []
    for index, (role, label, key) in enumerate(PALETTE_ROLES):
        roles.append({
            "role": role, "label": label, "hex": palette[key],
            "pinned": len(pinned) > index,
        })
    return {"palette": palette, "roles": roles,
            "palette_text": values.get("Palette", "")}


def _apply_palette_hexes(text: str, hexes: list[str]) -> str:
    """Replace the hex tokens of a Palette field value position
    by position, keeping the field's words; hexes beyond the
    tokens present are appended."""
    tokens = list(skeleton_module._HEX_RE.finditer(text))
    out = text
    # Replace from the end so earlier offsets stay valid.
    for index in range(min(len(tokens), len(hexes)) - 1, -1, -1):
        match = tokens[index]
        out = out[:match.start()] + hexes[index] + out[match.end():]
    extra = hexes[len(tokens):]
    if extra:
        suffix = " ".join(extra)
        out = (out.rstrip() + " " + suffix) if out.strip() else suffix
    return out


def save_palette(project_dir, hexes: list[str]) -> dict:
    """Write new role hexes into the scope's Palette field (the
    scope writer owns the file's shape), in role order. Returns
    the new palette_state. Roles beyond the list keep their
    current values: the caller passes all five."""
    project = Path(project_dir)
    if len(hexes) != len(PALETTE_ROLES):
        raise AssetsError(
            f"The palette editor saves {len(PALETTE_ROLES)} role "
            f"colours, got {len(hexes)}.")
    normalised = [normalise_hex(value) for value in hexes]
    scope_file = scope_module.scope_path_for(project)
    if not scope_file.exists():
        raise AssetsError(f"No scope file found at {scope_file}.")
    parsed = scope_module.load_scope(scope_file)
    values = {f.name: f.value for f in parsed.fields}
    new_text = _apply_palette_hexes(values.get("Palette", ""), normalised)
    scope_module.set_field_value(scope_file, "Palette", new_text)
    return palette_state(project)


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
