"""Tests for the bundled demo project (0.10.0): the packaged
example is a real finished game (built by the factory itself),
small enough to ship, installable into a workspace without ever
overwriting, and openable from the app dashboard.
"""

import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import filled_scope_text  # noqa: E402,F401  (sets sys.path)

from weaverbird import app as app_module  # noqa: E402
from weaverbird import demo, scope, state  # noqa: E402

TWO_MB = 2 * 1024 * 1024


class BundledDemoTests(unittest.TestCase):
    def test_demo_is_packaged_and_small(self):
        self.assertTrue(demo.demo_available())
        files, total = demo.demo_size()
        self.assertGreater(files, 50)
        self.assertLess(total, TWO_MB,
                        f"demo is {total} bytes — too heavy to ship")

    def test_demo_is_a_finished_game_with_an_approved_scope(self):
        source = demo.demo_source()
        parsed = scope.load_scope(source / "SCOPE.md")
        self.assertTrue(parsed.is_complete)
        self.assertTrue(parsed.is_approved)
        project_state = state.load_state(Path(str(source)))
        self.assertEqual(project_state["stage"], "finished")
        self.assertTrue(all(
            project_state["definition_of_done"].values()))
        report = (Path(str(source)) / "FINISH_REPORT.md") \
            .read_text(encoding="utf-8")
        self.assertIn("Finish report", report)
        # The demo's scope states its number and the built config
        # honours it (F7): win text "Collect 6 stars each round".
        import json
        config = json.loads((Path(str(source)) / "game"
                             / "game_config.json").read_text(
                                 encoding="utf-8"))
        self.assertEqual(config["targets_per_round"], 6)


class InstallDemoTests(unittest.TestCase):
    def test_install_copies_a_working_project(self):
        with tempfile.TemporaryDirectory() as tmp:
            installed = demo.install_demo(tmp)
            path = installed["path"]
            self.assertTrue((path / "SCOPE.md").is_file())
            self.assertTrue((path / "game" / "project.godot").is_file())
            self.assertEqual(state.load_state(path)["stage"],
                             "finished")
            # The app discovers it like any other project.
            names = [p["name"] for p in app_module.find_projects(tmp)]
            self.assertEqual(names, ["demo-arena"])

    def test_install_refuses_to_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            demo.install_demo(tmp)
            marker = Path(tmp) / "demo-arena" / "SCOPE.md"
            before = marker.read_text(encoding="utf-8")
            with self.assertRaises(demo.DemoError):
                demo.install_demo(tmp)
            self.assertEqual(marker.read_text(encoding="utf-8"),
                             before)

    def test_install_under_another_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            installed = demo.install_demo(tmp, name="my-demo")
            self.assertEqual(installed["name"], "my-demo")
            self.assertTrue((Path(tmp) / "my-demo"
                             / "SCOPE.md").is_file())

    def test_install_rejects_a_bad_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(demo.DemoError):
                demo.install_demo(tmp, name="Not A Folder")


class DemoAppTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self._tmp.name)
        self.server = app_module.create_server(self.workspace, port=0)
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self._tmp.cleanup()

    def test_dashboard_offers_the_demo_and_post_installs_it(self):
        with urllib.request.urlopen(self.base + "/",
                                    timeout=30) as response:
            dashboard = response.read().decode("utf-8")
        self.assertIn("Open the demo project", dashboard)
        request = urllib.request.Request(
            self.base + "/demo", data=b"", method="POST")
        opener = urllib.request.build_opener(
            urllib.request.HTTPHandler)
        try:
            with opener.open(request, timeout=30) as response:
                landed = response.geturl()
        except urllib.error.HTTPError as exc:
            self.fail(f"POST /demo answered {exc.code}")
        self.assertIn("/project/demo-arena", landed)
        self.assertTrue((self.workspace / "demo-arena"
                         / "SCOPE.md").is_file())
        # A second POST lands on the same project page and does
        # not overwrite anything.
        with opener.open(request, timeout=30) as response:
            self.assertIn("/project/demo-arena", response.geturl())


if __name__ == "__main__":
    unittest.main()
