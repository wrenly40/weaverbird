import stat
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import *  # noqa: F401,F403,E402

from weaverbird import godot  # noqa: E402

FIXTURE = """; Engine configuration file.

config_version=5

[application]

config/name="Test Game"
run/main_scene="res://main.tscn"

[display]

window/size/viewport_width=800
"""


class GodotTests(unittest.TestCase):
    def test_parse_project_godot_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "project.godot"
            path.write_text(FIXTURE, encoding="utf-8")
            result = godot.parse_project_godot(path)
            self.assertEqual(result["config_version"], 5)
            self.assertEqual(result["name"], "Test Game")

    def test_parse_project_godot_from_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "project.godot").write_text(FIXTURE, encoding="utf-8")
            result = godot.parse_project_godot(tmp)
            self.assertEqual(result["name"], "Test Game")

    def test_missing_project_godot_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(FileNotFoundError):
                godot.parse_project_godot(tmp)

    def test_find_godot_with_explicit_fake_binary(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp) / "godot"
            fake.write_text("#!/bin/sh\necho '9.9.9.stable.test'\n", encoding="utf-8")
            fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
            path, version = godot.find_godot(explicit=fake)
            self.assertEqual(path, str(fake))
            self.assertEqual(version, "9.9.9.stable.test")

    def test_find_godot_with_missing_explicit_and_no_fallback(self):
        path, _version = godot.find_godot(explicit="/nonexistent/godot-xyz")
        # Either a real Godot is found via PATH/fallback, or none is; the
        # missing explicit path itself must never be returned.
        self.assertNotEqual(path, "/nonexistent/godot-xyz")


if __name__ == "__main__":
    unittest.main()
