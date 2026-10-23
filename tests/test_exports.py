"""Tests for the export worker (0.10.0): presets, packaging,
the report, and prerequisite honesty.

Everything runs against a STUB Godot binary and a fake home —
the suite never depends on the real export templates being
installed. The stub answers --version and --export-release by
writing a plausible-sized dummy artifact at the export path
(or failing with a Godot-style ERROR line when FAKE_GODOT_FAIL
is set in its environment).
"""

import hashlib
import json
import os
import stat
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from weaverbird import exports, state as state_module, workers  # noqa: E402

STUB_GODOT = '''#!/usr/bin/env python3
import os, sys, zipfile

args = sys.argv[1:]
if "--version" in args:
    print("4.7.2.stable.official.testhash")
    raise SystemExit(0)
if "--export-release" in args:
    if os.environ.get("FAKE_GODOT_FAIL"):
        print('ERROR: Project export for preset "X" failed.',
              file=sys.stderr)
        raise SystemExit(1)
    out = args[-1]
    os.makedirs(os.path.dirname(out), exist_ok=True)
    payload = os.urandom(1_100_000)
    if out.endswith(".zip"):
        with zipfile.ZipFile(out, "w", zipfile.ZIP_STORED) as zf:
            zf.writestr("Game.app/Contents/MacOS/Game", payload)
    else:
        with open(out, "wb") as fh:
            fh.write(payload)
    raise SystemExit(0)
raise SystemExit(2)
'''

PROJECT_GODOT = """; Engine configuration file.
config_version=5

[application]

config/name="Export Test Game"
"""


def _write_stub(directory: Path) -> Path:
    stub = directory / "fake-godot"
    stub.write_text(STUB_GODOT, encoding="utf-8")
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    return stub


def _fake_home(directory: Path, with_templates: bool) -> Path:
    home = directory / "home"
    home.mkdir(parents=True, exist_ok=True)
    if with_templates:
        folder = (home / ".local" / "share" / "godot"
                  / "export_templates" / "4.7.2.stable")
        folder.mkdir(parents=True)
        for name in ("linux_release.x86_64",
                     "windows_release_x86_64.exe", "macos.zip"):
            (folder / name).write_bytes(b"template")
    return home


def _finished_project(directory: Path, stage: str = "finished") -> Path:
    project = directory / "export-test-game"
    game = project / "game"
    (game / "scripts").mkdir(parents=True)
    (game / ".godot").mkdir()
    (game / ".godot" / "junk.tmp").write_text("cache", encoding="utf-8")
    (game / "project.godot").write_text(PROJECT_GODOT, encoding="utf-8")
    (game / "game_config.json").write_text("{}", encoding="utf-8")
    (game / "scripts" / "game.gd").write_text("extends Node\n",
                                              encoding="utf-8")
    (project / "FINISH_REPORT.md").write_text("# done\n",
                                              encoding="utf-8")
    saved = state_module.init_state(project)
    saved["stage"] = stage
    state_module.save_state(project, saved)
    return project


class ExportsTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.stub = _write_stub(self.root)
        self._env = dict(os.environ)
        self.addCleanup(self._restore_env)

    def _restore_env(self):
        os.environ.clear()
        os.environ.update(self._env)
        self._tmp.cleanup()

    def _run(self, project, home, targets=None):
        return exports.run_exports(project, targets=targets,
                                   godot_path=str(self.stub), home=home)

    # -- prerequisite honesty -------------------------------------

    def test_blocked_without_templates_names_the_missing_piece(self):
        project = _finished_project(self.root)
        home = _fake_home(self.root, with_templates=False)
        report = self._run(project, home)
        by_target = {r["target"]: r for r in report["targets"]}
        self.assertEqual(by_target["source"]["status"], "exported")
        for target in ("linux", "windows", "macos"):
            record = by_target[target]
            self.assertEqual(record["status"], "blocked", target)
            self.assertIn(exports.TEMPLATE_FILES[target],
                          record["reason"])
            self.assertIn("export_templates", record["reason"])
        text = (project / "EXPORT_REPORT.md").read_text(encoding="utf-8")
        self.assertIn("blocked", text)
        self.assertIn("Android and iOS are not exported", text)
        record = exports.load_record(project)
        self.assertIsNotNone(record)
        self.assertEqual(record["version"], "0.1.0")

    def test_missing_godot_binary_is_a_named_blocker(self):
        reason = exports.missing_prerequisite("linux", None, None)
        self.assertIn("Godot binary was not found", reason)

    def test_templates_dir_parsing(self):
        home = self.root / "h"
        path = exports.templates_dir_for(
            "4.7.2.stable.official.ed1daf0bf", home=home)
        self.assertTrue(str(path).endswith(
            "export_templates/4.7.2.stable"))
        self.assertIsNone(exports.templates_dir_for("junk", home=home))
        self.assertIsNone(exports.templates_dir_for(None, home=home))

    # -- the happy path against the stub ---------------------------

    def test_all_targets_export_with_fake_templates(self):
        project = _finished_project(self.root)
        home = _fake_home(self.root, with_templates=True)
        report = self._run(project, home)
        by_target = {r["target"]: r for r in report["targets"]}
        for target in exports.TARGETS:
            record = by_target[target]
            self.assertEqual(record["status"], "exported", record)
            artifact = project / record["artifact"]
            self.assertTrue(artifact.is_file(), record["artifact"])
            self.assertEqual(artifact.stat().st_size, record["size"])
            digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
            self.assertEqual(digest, record["sha256"])
            self.assertEqual(
                artifact.name,
                f"export-test-game-0.1.0-{target}.zip")
            if target in exports.DESKTOP_TARGETS:
                self.assertIn("unsigned", record["note"])
        # Source zip: source in, caches and staging out.
        with zipfile.ZipFile(
                project / by_target["source"]["artifact"]) as zf:
            names = zf.namelist()
        self.assertIn("project.godot", names)
        self.assertIn("scripts/game.gd", names)
        self.assertFalse(any(n.startswith(".godot/") for n in names))
        self.assertFalse(any(n.startswith("build/") for n in names))
        # Linux zip carries the binary itself.
        with zipfile.ZipFile(
                project / by_target["linux"]["artifact"]) as zf:
            self.assertEqual(zf.namelist(), ["export-test-game.x86_64"])
        # The factory's staging folder is cleaned up.
        self.assertFalse((project / "game" / "build").exists())
        # macOS notes carry the bundle identifier.
        self.assertEqual(by_target["macos"]["bundle_identifier"],
                         "com.weaverbird.exporttestgame")

    def test_godot_failure_is_blocked_with_its_error_line(self):
        os.environ["FAKE_GODOT_FAIL"] = "1"
        project = _finished_project(self.root)
        home = _fake_home(self.root, with_templates=True)
        report = self._run(project, home)
        by_target = {r["target"]: r for r in report["targets"]}
        self.assertEqual(by_target["source"]["status"], "exported")
        for target in exports.DESKTOP_TARGETS:
            record = by_target[target]
            self.assertEqual(record["status"], "blocked", record)
            self.assertIn('ERROR: Project export for preset "X" '
                          "failed.", record["reason"])

    def test_version_comes_from_game_version_file(self):
        project = _finished_project(self.root)
        (project / "game" / "VERSION").write_text("0.3.2\n",
                                                   encoding="utf-8")
        home = _fake_home(self.root, with_templates=True)
        report = self._run(project, home, targets=["source"])
        self.assertEqual(report["version"], "0.3.2")
        record = report["targets"][0]
        self.assertIn("-0.3.2-source.zip", record["artifact"])

    # -- presets: add missing, never clobber ------------------------

    def test_presets_added_without_clobbering_a_makers(self):
        project = _finished_project(self.root)
        game = project / "game"
        maker = (
            '[preset.0]\n\nname="My Linux"\nplatform="Linux"\n'
            'runnable=true\nexport_path="out/game.x86_64"\n\n'
            "[preset.0.options]\n\n"
            'binary_format/architecture="x86_64"\n')
        (game / "export_presets.cfg").write_text(maker,
                                                 encoding="utf-8")
        info = exports.ensure_presets(game, list(exports.TARGETS),
                                      "export-test-game")
        self.assertEqual(info["existing"], ["linux"])
        self.assertEqual(sorted(info["added"]), ["macos", "windows"])
        self.assertEqual(info["presets"]["linux"]["name"], "My Linux")
        text = (game / "export_presets.cfg").read_text(encoding="utf-8")
        self.assertIn("My Linux", text)
        self.assertIn('export_path="out/game.x86_64"', text)
        # Idempotent: a second pass adds nothing.
        again = exports.ensure_presets(game, list(exports.TARGETS),
                                       "export-test-game")
        self.assertEqual(again["added"], [])
        self.assertEqual(sorted(again["existing"]),
                         ["linux", "macos", "windows"])

    def test_etc2_astc_setting_added_once(self):
        project = _finished_project(self.root)
        game = project / "game"
        self.assertTrue(exports.ensure_etc2_astc(game))
        self.assertFalse(exports.ensure_etc2_astc(game))
        text = (game / "project.godot").read_text(encoding="utf-8")
        self.assertIn("textures/vram_compression/import_etc2_astc=true",
                      text)

    # -- the worker gate ---------------------------------------------

    def test_worker_refuses_an_unfinished_project(self):
        project = _finished_project(self.root, stage="build")
        outcome = workers.run_worker("export", project,
                                     params={"targets": ["source"]})
        self.assertFalse(outcome["ok"])
        self.assertEqual(outcome["issues"][0]["owner"], "finish")

    def test_worker_exports_a_finished_project(self):
        project = _finished_project(self.root)
        outcome = workers.run_worker("export", project,
                                     params={"targets": ["source"]})
        self.assertTrue(outcome["ok"], outcome)
        self.assertIn("exports/EXPORTS.json", outcome["artifacts"])
        record = workers.load_record(project, "export")
        self.assertIsNotNone(record)
        self.assertTrue(record["ok"])
        self.assertEqual(record["metrics"]["target_source"], "exported")

    def test_export_worker_is_registered_with_its_contract(self):
        spec = workers.WORKERS["export"]
        self.assertIn("game/", spec.inputs)
        self.assertIn("exports/", spec.outputs)
        self.assertIn("finish", spec.depends_on)
        self.assertFalse(spec.can_skip)


if __name__ == "__main__":
    unittest.main()
