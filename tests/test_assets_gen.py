"""Tests for asset generation (assets_gen.py)."""

import io
import sys
import tempfile
import unittest
import wave
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import filled_scope_text, template_scope_text  # noqa: E402

from weaverbird import assets_gen, ledger, provenance, scope, skeleton, state  # noqa: E402

EXPECTED_SPRITES = [
    "player.svg", "target.svg", "hazard.svg", "platform_tile.svg",
    "spikes.svg", "exit_door.svg", "crate.svg", "pad.svg", "wall.svg",
    "background.svg",
]
EXPECTED_AUDIO = [
    "click.wav", "collect.wav", "hurt.wav", "jump.wav", "push.wav",
    "win.wav", "lose.wav", "music.wav",
]


def scoped_project(tmp, scope_text=None, approve=True):
    project = Path(tmp)
    (project / "SCOPE.md").write_text(scope_text or filled_scope_text(),
                                      encoding="utf-8")
    state.init_state(project)
    if approve:
        scope.approve_scope(project / "SCOPE.md", by="Tester")
    return project


class AssetsRefusalTests(unittest.TestCase):
    def test_refuses_without_a_skeleton(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = scoped_project(tmp)
            with self.assertRaises(assets_gen.AssetsError) as ctx:
                assets_gen.generate_assets(project)
            self.assertIn("skeleton", str(ctx.exception).lower())


class AssetsBuildTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        text = filled_scope_text().replace(
            "- REQUIRED Palette: Filled in for the test",
            "- REQUIRED Palette: deep #112233 with a bright #AABBCC accent")
        cls.project = scoped_project(cls._tmp.name, text)
        skeleton.build_skeleton(cls.project, validate=False)
        cls.icon_before = (cls.project / "game/icon.svg").read_text(
            encoding="utf-8")
        cls.report = assets_gen.generate_assets(cls.project)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_all_files_exist(self):
        game = self.project / "game"
        for name in EXPECTED_SPRITES:
            self.assertTrue((game / "assets" / name).exists(), name)
        for name in EXPECTED_AUDIO:
            self.assertTrue((game / "assets" / "audio" / name).exists(), name)
        self.assertEqual(len(self.report["files"]),
                         len(EXPECTED_SPRITES) + len(EXPECTED_AUDIO) + 1)

    def test_palette_comes_from_the_style_lock(self):
        self.assertEqual(self.report["palette"]["accent_hex"], "#112233")
        self.assertEqual(self.report["palette"]["target_hex"], "#AABBCC")
        player = (self.project / "game/assets/player.svg").read_text()
        self.assertIn("#112233", player)
        target = (self.project / "game/assets/target.svg").read_text()
        self.assertIn("#AABBCC", target)

    def test_icon_is_real_and_not_the_skeleton_placeholder(self):
        icon = (self.project / "game/icon.svg").read_text(encoding="utf-8")
        self.assertNotEqual(icon, self.icon_before)
        self.assertIn("#112233", icon)
        self.assertTrue(self.report["icon_generated"])
        self.assertEqual(self.report["icon"], "game/icon.svg")

    def test_audio_files_are_real_wavs(self):
        audio = self.project / "game/assets/audio"
        durations = {}
        for name in EXPECTED_AUDIO:
            with wave.open(str(audio / name), "rb") as wav:
                self.assertEqual(wav.getnchannels(), 1)
                self.assertEqual(wav.getframerate(), 22050)
                durations[name] = wav.getnframes() / wav.getframerate()
        self.assertGreater(durations["music.wav"], 7.0)   # the loop
        self.assertLess(durations["click.wav"], 0.2)      # a blip
        self.assertGreater(durations["win.wav"], durations["click.wav"])

    def test_provenance_records_every_file(self):
        records = {r["asset"]: r for r in provenance.list_records(self.project)}
        for rel in self.report["files"]:
            self.assertIn(rel, records)
            self.assertEqual(records[rel]["source"], "code")
            self.assertEqual(records[rel]["provider"], "weaverbird-assets")

    def test_ledger_entry_is_zero_tokens(self):
        summary = ledger.summarise(self.project)
        self.assertEqual(summary["by_stage"]["assets"]["tokens"], 0)
        self.assertEqual(summary["tokens"], 0)

    def test_handler_passes_once_assets_exist(self):
        self.assertTrue(assets_gen.assets_stage_handler(self.project))


class AssetsCliTests(unittest.TestCase):
    def test_cli_refuses_without_skeleton(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = scoped_project(tmp)
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                from weaverbird import cli
                code = cli.main(["assets", "generate", str(project)])
            self.assertEqual(code, 1)
            self.assertIn("skeleton", buffer.getvalue().lower())


def _palette_with_traits(sample_id):
    sample = assets_gen.style_sample_by_id(sample_id)
    palette = dict(sample["palette"])
    palette["style"] = dict(sample["traits"])
    return palette


def _tag_histogram(svg_text):
    import re
    hist = {}
    for tag in re.findall(r"<(circle|polygon|rect|ellipse|path|line)\b",
                          svg_text):
        hist[tag] = hist.get(tag, 0) + 1
    return hist


def _onsets(samples, rate=22050):
    """Note onsets in a sample list: short-window RMS jumps."""
    import math
    win = int(rate * 0.005)
    rms = []
    for i in range(0, len(samples) - win, win):
        chunk = samples[i:i + win]
        rms.append(math.sqrt(sum(v * v for v in chunk) / len(chunk)))
    found = []
    last = -10
    for i in range(1, len(rms)):
        if rms[i] > 0.04 and rms[i] > rms[i - 1] * 1.55 \
                and i - last >= 8:
            found.append(i)
            last = i
    return found


class StyleLockArtTests(unittest.TestCase):
    """0.12.0 (4/4): the style lock is real in the assets — the
    chosen sample's outline, shading, glow and pattern rules
    are obeyed by the drawing code, not just described."""

    def test_traits_resolve_from_the_scope_style_field(self):
        values = {"Style sample chosen (id / link to approved "
                  "sample)": "warm-storybook"}
        traits = assets_gen.traits_for_values(values)
        self.assertEqual(traits["outline_width"], 3.0)
        self.assertEqual(traits["outline_color"], "#3E2A1E")
        self.assertEqual(traits["shading"], "cel")

    def test_outlined_style_draws_outlines_neon_never_does(self):
        storybook = _palette_with_traits("warm-storybook")
        neon = _palette_with_traits("bold-neon-night")
        player_book = assets_gen._sprite_player(storybook)
        self.assertIn('stroke="#3E2A1E"', player_book)
        self.assertIn('stroke-width="12"', player_book)  # 3px at 4x
        target_book = assets_gen._sprite_target(storybook)
        self.assertIn('stroke="#3E2A1E"', target_book)
        player_neon = assets_gen._sprite_player(neon)
        self.assertNotIn("stroke", player_neon)
        target_neon = assets_gen._sprite_target(neon)
        self.assertNotIn('stroke="#', target_neon.replace(
            'stroke="#FFFFFF"', ""))  # no outline stroke in neon

    def test_collectible_glows_and_sparkles(self):
        svg = assets_gen._sprite_target(
            _palette_with_traits("soft-pastel"))
        self.assertIn('opacity="0.16"', svg)  # the halo
        hist = _tag_histogram(svg)
        self.assertGreaterEqual(hist.get("circle", 0), 3)
        # A sparkle is an 8-point polygon.
        import re
        sparkles = [pts for pts in re.findall(
            r'<polygon points="([^"]+)"', svg)
            if len(pts.split()) == 8]
        self.assertTrue(sparkles)

    def test_hazard_and_collectible_differ_in_shape_not_colour(self):
        palette = _palette_with_traits("warm-storybook")
        hazard = _tag_histogram(assets_gen._sprite_hazard(palette))
        target = _tag_histogram(assets_gen._sprite_target(palette))
        self.assertNotEqual(hazard, target)
        # The hazard's menace is geometry: radiating spike
        # polygons the collectible does not have.
        self.assertGreater(hazard.get("polygon", 0),
                           target.get("polygon", 0))

    def test_player_has_a_visor_with_a_facing_slit(self):
        svg = assets_gen._sprite_player(
            _palette_with_traits("bright-chunky"))
        self.assertIn('rx="22"', svg)          # the visor band
        self.assertIn('opacity="0.92"', svg)   # its bright slit

    def test_icon_composes_the_games_own_sprites(self):
        palette = _palette_with_traits("warm-storybook")
        icon = assets_gen._icon(palette, "top_down_2d")
        # Player + collectible, embedded from the sprite makers.
        self.assertEqual(icon.count('viewBox="0 0 256 256"'), 2)
        self.assertNotIn("#171A24", icon)  # no generic dark ground

    def test_background_is_a_palette_ground_with_a_pattern(self):
        neon = assets_gen._sprite_background(
            _palette_with_traits("bold-neon-night"))
        self.assertNotIn("#101219", neon)
        self.assertIn("<polygon", neon)  # star pattern sparkles
        pastel = assets_gen._sprite_background(
            _palette_with_traits("soft-pastel"))
        self.assertIn("<line", pastel)  # grid pattern

    def test_sprites_are_authored_at_4x(self):
        svg = assets_gen._sprite_player(
            _palette_with_traits("bright-chunky"))
        self.assertIn('width="64"', svg)
        self.assertIn('viewBox="0 0 256 256"', svg)


class MoodAudioTests(unittest.TestCase):
    """0.12.0 (4/4): shaped synthesis in the scope's audio
    mood — ADSR notes, a real jingle, a quiet pad."""

    def test_mood_parsing(self):
        calm = assets_gen.audio_mood(
            {"Audio mood (music + SFX character)": "calm, quiet"})
        self.assertEqual(calm["name"], "calm")
        hot = assets_gen.audio_mood(
            {"Audio mood (music + SFX character)": "energetic arcade"})
        self.assertEqual(hot["name"], "energetic")
        plain = assets_gen.audio_mood({})
        self.assertEqual(plain["name"], "bright")
        self.assertLess(calm["note_s"], hot["note_s"] * 3)

    def test_win_jingle_has_at_least_three_note_onsets(self):
        for name in ("calm", "energetic", "bright"):
            mood = assets_gen.audio_mood(
                {"Audio mood (music + SFX character)": name})
            files = assets_gen._audio_files(mood)
            self.assertGreaterEqual(
                len(_onsets(files["win.wav"])), 3,
                f"{name} win jingle onsets")

    def test_collect_is_a_two_note_chime(self):
        files = assets_gen._audio_files(assets_gen.audio_mood({}))
        self.assertGreaterEqual(len(_onsets(files["collect.wav"])), 2)

    def test_ambient_pad_is_long_quiet_and_deterministic(self):
        import math
        mood = assets_gen.audio_mood(
            {"Audio mood (music + SFX character)": "calm"})
        pad = assets_gen._audio_files(mood)["music.wav"]
        seconds = len(pad) / assets_gen.SAMPLE_RATE
        self.assertGreaterEqual(seconds, 4.0)
        rms = math.sqrt(sum(v * v for v in pad) / len(pad))
        self.assertGreater(rms, 0.005)
        self.assertLess(rms, 0.2)
        again = assets_gen._audio_files(mood)["music.wav"]
        self.assertEqual(pad, again)

    def test_hurt_is_a_noise_burst_not_a_beep(self):
        files = assets_gen._audio_files(assets_gen.audio_mood({}))
        hurt = files["hurt.wav"][:2205]
        crossings = sum(1 for a, b in zip(hurt, hurt[1:])
                        if (a < 0) != (b < 0))
        self.assertGreater(crossings, 60)


class CollectibleVariantTests(unittest.TestCase):
    """0.12.0 (4/4): per-instance collectible variation is
    recorded in the world data, deterministically."""

    def _plan(self, movement):
        from weaverbird import plan as plan_module
        values = {
            "Title (working)": "Variant Fields",
            "Palette": "accent #FF7A1A",
            "Dimension": "3D" if movement == "top_down_3d" else "2D",
            "Core verb (what the player does most)": "collect orbs",
            "Win condition": "collect every orb in each round",
            "Lose / fail condition": "the timer runs out or a "
                                     "hazard touches you",
            "Content counts (levels / waves / rooms / items, as "
            "applicable)": "3 rounds",
        }
        return plan_module.derive_plan(values)

    def test_variants_recorded_aligned_and_in_range(self):
        from weaverbird import world as world_module
        for movement in ("top_down_2d", "top_down_3d"):
            data = world_module.world_from_plan(self._plan(movement))
            for unit in data["units"]:
                variants = unit["collectible_variants"]
                self.assertEqual(len(variants),
                                 len(unit["collectibles"]))
                for variant in variants:
                    self.assertTrue(0.92 <= variant["tint"] <= 1.08)
                    self.assertTrue(0.90 <= variant["scale"] <= 1.10)
            distinct = {tuple(sorted(v.items()))
                        for unit in data["units"]
                        for v in unit["collectible_variants"]}
            self.assertGreater(len(distinct), 3)

    def test_variants_are_deterministic(self):
        from weaverbird import world as world_module
        first = world_module.world_from_plan(self._plan("top_down_3d"))
        second = world_module.world_from_plan(self._plan("top_down_3d"))
        self.assertEqual(
            [u["collectible_variants"] for u in first["units"]],
            [u["collectible_variants"] for u in second["units"]])


if __name__ == "__main__":
    unittest.main()
