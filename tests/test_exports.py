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
sys.path.insert(0, str(Path(__file__).resolve().parent))

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
    from helpers import filled_scope_text
    (project / "SCOPE.md").write_text(filled_scope_text(),
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


class ExportCliTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.stub = _write_stub(self.root)
        self.home = _fake_home(self.root, with_templates=False)
        self._env = dict(os.environ)
        os.environ["WEAVERBIRD_GODOT"] = str(self.stub)
        os.environ["HOME"] = str(self.home)
        self.addCleanup(self._restore)

    def _restore(self):
        os.environ.clear()
        os.environ.update(self._env)
        self._tmp.cleanup()

    def _main(self, argv):
        import contextlib
        import io
        from weaverbird import cli
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main(argv)
        return code, out.getvalue()

    def test_cli_export_prints_outcomes_and_report_path(self):
        project = _finished_project(self.root)
        code, text = self._main(["export", str(project)])
        self.assertEqual(code, 0, text)
        self.assertIn("✓ source:", text)
        self.assertIn("– linux: blocked", text)
        self.assertIn("EXPORT_REPORT.md", text)

    def test_cli_export_all_blocked_exits_nonzero(self):
        project = _finished_project(self.root)
        code, text = self._main(
            ["export", str(project), "--targets", "linux"])
        self.assertEqual(code, 1, text)
        self.assertIn("blocked", text)

    def test_cli_export_refuses_an_unfinished_project(self):
        project = _finished_project(self.root, stage="build")
        code, text = self._main(["export", str(project)])
        self.assertEqual(code, 1, text)

    def test_cli_export_rejects_unknown_targets(self):
        project = _finished_project(self.root)
        code, text = self._main(
            ["export", str(project), "--targets", "amiga"])
        self.assertEqual(code, 1, text)
        self.assertIn("Unknown target", text)


class ExportAppTests(unittest.TestCase):
    """The Exports card, the run-exports job and the download
    route, over real HTTP against a throwaway workspace."""

    def setUp(self):
        import threading
        from weaverbird import app as app_module
        self._app_module = app_module
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.workspace = self.root / "ws"
        self.workspace.mkdir()
        self.stub = _write_stub(self.root)
        self.home = _fake_home(self.root, with_templates=False)
        self._env = dict(os.environ)
        os.environ["WEAVERBIRD_GODOT"] = str(self.stub)
        os.environ["HOME"] = str(self.home)
        self.project = _finished_project(self.workspace)
        self.server = app_module.create_server(self.workspace, port=0)
        self.thread = threading.Thread(
            target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = self.server.url.rstrip("/")
        self.addCleanup(self._restore)

    def _restore(self):
        self.server.shutdown()
        self.server.server_close()
        os.environ.clear()
        os.environ.update(self._env)
        self._tmp.cleanup()

    def _get(self, path):
        import urllib.error
        import urllib.request
        try:
            with urllib.request.urlopen(self.base + path,
                                        timeout=15) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()

    def _post(self, path, fields):
        import urllib.error
        import urllib.parse
        import urllib.request

        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **k):
                return None

        opener = urllib.request.build_opener(NoRedirect)
        data = urllib.parse.urlencode(fields).encode()
        request = urllib.request.Request(self.base + path, data=data)
        try:
            with opener.open(request, timeout=15) as response:
                return response.status, response.headers, response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.headers, exc.read()

    def test_card_job_and_download(self):
        import time
        import urllib.parse
        quoted = urllib.parse.quote(self.project.name)
        status, body = self._get(f"/project/{quoted}")
        self.assertEqual(status, 200)
        page = body.decode("utf-8")
        self.assertIn("Exports — local files", page)
        self.assertIn('name="target_linux"', page)
        # Run exports for every target, through the job runner:
        # source exports; the desktop trio blocks on the fake
        # home's missing templates.
        status, headers, _ = self._post(
            f"/project/{quoted}/run-exports",
            {"target_linux": "on", "target_windows": "on",
             "target_macos": "on", "target_source": "on"})
        self.assertEqual(status, 303)
        location = headers.get("Location", "")
        self.assertIn("?job=", location)
        job_id = location.rsplit("?job=", 1)[1]
        deadline = time.time() + 30
        job = None
        while time.time() < deadline:
            status, raw = self._get(f"/api/jobs/{job_id}")
            job = json.loads(raw)
            if job["status"] in ("done", "failed", "cancelled"):
                break
            time.sleep(0.2)
        self.assertEqual(job["status"], "done", job)
        self.assertEqual(job["worker"], "export")
        # The card now lists the artifact with a download link
        # and shows the blocked desktop reasons inline.
        status, body = self._get(f"/project/{quoted}")
        page = body.decode("utf-8")
        artifact = "export-test-game-0.1.0-source.zip"
        self.assertIn(f"/export-file/{artifact}", page)
        self.assertIn("Blocked:", page)
        # The download serves the real zip.
        status, data = self._get(
            f"/project/{quoted}/export-file/{artifact}")
        self.assertEqual(status, 200)
        self.assertEqual(data[:2], b"PK")
        # Traversal attempts and unknown files are 404s.
        status, _ = self._get(
            f"/project/{quoted}/export-file/..%2F..%2FSCOPE.md")
        self.assertEqual(status, 404)
        status, _ = self._get(
            f"/project/{quoted}/export-file/nope.zip")
        self.assertEqual(status, 404)

    def test_no_card_before_the_project_is_finished(self):
        import shutil
        unfinished = _finished_project(self.root, stage="build")
        target = self.workspace / "unfinished-game"
        shutil.copytree(unfinished, target)
        status, body = self._get("/project/unfinished-game")
        self.assertEqual(status, 200)
        self.assertNotIn("Exports — local files",
                         body.decode("utf-8"))


if __name__ == "__main__":
    unittest.main()