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


if __name__ == "__main__":
    unittest.main()
