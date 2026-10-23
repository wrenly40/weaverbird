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
    "crate": ["crate"],
    "pad": ["pad"],
    "wall": ["wall"],
}
_MOVEMENT_AUDIO = {"platform": ["jump"], "grid": ["push"]}
_FAIL_AUDIO = {"hazard_contact": ["hurt"], "timer": ["hurt"],
               "lives": ["hurt"], "move_budget": ["push"]}


def asset_requests(project: Path, values: dict) -> dict:
    """The assets this game needs, derived from its composition
    and — when the world worker has run — the entity kinds
    actually present in game/world/world.json:
    {sprites, audio, based_on, bespoke}.

    Preset games additionally keep the factory's shared asset
    set (their templates opportunistically load any of it, as
    they always have); bespoke games get exactly their requests
    — there is no preset set to fall back on.
    """
    from . import world as world_module

    derived = generate_module.derive_composition(
        values, override=None)
    bespoke = bool(derived["bespoke"])
    world_data = world_module.load_world(project)
    kinds: list[str] = []
    based_on = "composition"
    if world_data and world_data.get("entity_kinds"):
        kinds = list(world_data["entity_kinds"])
        based_on = "world"
    elif derived["composition"]:
        preset = derived["preset"]
        kinds = list(world_module.ENTITY_KINDS.get(preset, []))
    sprites = {"background"}
    for kind in kinds:
        sprites.update(_ENTITY_SPRITES.get(kind, []))
    if bespoke:
        sprites.update(["player", "target"])
    audio = {"click", "collect", "win", "lose", "music"}
    composition = derived["composition"] or {}
    audio.update(_MOVEMENT_AUDIO.get(composition.get("movement"), []))
    for fail in composition.get("fail") or []:
        audio.update(_FAIL_AUDIO.get(fail, []))
    if bespoke:
        audio.update(["hurt"])
    return {
        "sprites": sorted(sprites),
        "audio": sorted(audio),
        "based_on": based_on,
        "bespoke": bespoke,
        "composition": derived["composition"],
        "preset": derived["preset"],
    }


def _preflight(project: Path,
               archetype: str | None = None) -> tuple[dict, str, dict]:
    """(palette, archetype, scope values) for a project whose
    skeleton game exists — the shared start of every asset
    operation. The 'archetype' returned is the composition's
    preset name (kept for the icon motif and the report's
    history); asset selection itself goes through
    asset_requests()."""
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
        derived = generate_module.derive_composition(values)
        chosen = derived["preset"] or "arena_collector"
    return generate_module._palette(values), chosen, values


def _producers(palette: dict, archetype: str,
               requests: dict | None = None) -> dict:
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
    producers: dict[str, tuple[str, object, str, str]] = {}
    for name, maker in _SPRITES.items():
        if bespoke and name not in requests["sprites"]:
            continue
        producers[name] = (
            f"{GAME_DIR_NAME}/assets/{name}.svg",
            (lambda maker=maker: maker(palette).encode("utf-8")),
            f"{name} sprite drawn from the style-lock palette by "
            "the deterministic SVG generator",
            "sprite")
    producers["icon"] = (
        f"{GAME_DIR_NAME}/icon.svg",
        (lambda: _icon(palette, archetype).encode("utf-8")),
        f"game icon generated from the palette with the "
        f"{archetype} motif",
        "icon")
    for filename, samples in _audio_files().items():
        stem = filename.rsplit(".", 1)[0]
        if bespoke and stem not in requests["audio"]:
            continue
        producers[stem] = (
            f"{GAME_DIR_NAME}/assets/audio/{filename}",
            (lambda samples=samples: _wav_bytes(samples)),
            "procedurally synthesised audio (stdlib wave, no samples)",
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
    producers = _producers(palette, chosen, requests)
    if reporter is not None:
        reporter(f"Generating assets for the {chosen} game — palette "
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
        "archetype": chosen,
        "composition": requests["composition"],
        "preset": requests["preset"],
        "requests": {"sprites": requests["sprites"],
                     "audio": requests["audio"],
                     "based_on": requests["based_on"]},
        "palette": palette,
        "files": written,
        "icon": f"{GAME_DIR_NAME}/icon.svg",
        "icon_generated": True,
    }
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
        "sky": "#FBEED3", "ground": "#7FA653", "sun": "#F6B352",
    },
]

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
                             asset_requests(project, values)))


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
                           asset_requests(project, values))
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
                           asset_requests(project, values))
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
