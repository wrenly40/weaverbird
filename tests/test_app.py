"""Tests for the local web app (app.py).

The server runs on an ephemeral port in a thread and is driven with
urllib — the same way a browser would, minus the rendering. The
contract: the dashboard lists the workspace's projects, the project
page shows the pipeline stage and the definition of done, the scope
form saves back into SCOPE.md, and the finish-check action marks
what the evidence supports.
"""

import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import filled_scope_text, template_scope_text  # noqa: E402

from weaverbird import app as app_module  # noqa: E402
from weaverbird import scope, state  # noqa: E402


def get(url):
    with urllib.request.urlopen(url, timeout=10) as response:
        return response.status, response.read().decode("utf-8")


def post(url, fields):
    data = urllib.parse.urlencode(fields).encode("utf-8")
    request = urllib.request.Request(url, data=data, method="POST")
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.status, response.read().decode("utf-8")


class AppTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.workspace = Path(self.tmp) / "workspace"
        self.project = self.workspace / "bee-game"
        self.project.mkdir(parents=True)
        (self.project / "SCOPE.md").write_text(filled_scope_text(),
                                              encoding="utf-8")
        scope.approve_scope(self.project / "SCOPE.md", by="Tester")
        state.init_state(self.project)
        self.server = app_module.create_server(self.workspace, port=0)
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       daemon=True)
        self.thread.start()
        self.base = self.server.url.rstrip("/")

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


class TestDashboard(AppTestCase):
    def test_dashboard_lists_the_project(self):
        status, page = get(self.base + "/")
        self.assertEqual(status, 200)
        self.assertIn("Filled in for the test", page)  # title/pitch from scope
        self.assertIn("Scope", page)  # current stage label
        self.assertIn("Factory", page)
        self.assertIn("Brain", page)

    def test_dashboard_binds_localhost_only(self):
        host = self.server.server_address[0]
        self.assertEqual(host, "127.0.0.1")

    def test_static_assets_are_served_from_the_package(self):
        status, css = get(self.base + "/style.css")
        self.assertEqual(status, 200)
        self.assertIn("#6C4CF1", css)
        status, js = get(self.base + "/app.js")
        self.assertEqual(status, 200)
        self.assertIn("Working", js)


class TestProjectPage(AppTestCase):
    def test_project_page_shows_stage_and_dod(self):
        status, page = get(self.base + "/project/bee-game")
        self.assertEqual(status, 200)
        self.assertIn("Definition of done", page)
        self.assertIn("Scenario tests pass", page)
        self.assertIn("Build skeleton", page)
        self.assertIn("Run finish check", page)

    def test_finish_check_action_marks_from_evidence(self):
        # Give the project skeleton evidence, then press the button.
        scripts = self.project / "game" / "scripts"
        scripts.mkdir(parents=True)
        for name in ("main_menu.gd", "settings.gd", "settings_store.gd",
                     "pause_input.gd"):
            (scripts / name).write_text("# fixture\n", encoding="utf-8")
        report = {
            "files": [], "known_good_tag": "known-good/skeleton",
            "validation": {"passed": True, "godot_version": "4.7.2",
                           "runs": [
                               {"name": "import", "ok": True, "errors": []},
                               {"name": "flow smoke", "ok": True,
                                "errors": [], "smoke_pass": True}]},
        }
        wb = self.project / ".weaverbird"
        (wb / "skeleton_report.json").write_text(json.dumps(report),
                                                 encoding="utf-8")
        status, page = post(self.base + "/project/bee-game/finish-check", {})
        self.assertEqual(status, 200)
        self.assertIn("Finish check done", page)
        self.assertIn("Still open", page)  # losable etc. stay honest
        current = state.load_state(self.project)
        self.assertTrue(
            current["definition_of_done"]["scenario_tests_pass"])
        self.assertEqual(
            current["dod_evidence"]["scenario_tests_pass"]["by"],
            "weaverbird")

    def test_unknown_project_is_a_plain_404(self):
        try:
            get(self.base + "/project/nope")
            self.fail("expected a 404")
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, 404)


class TestScopeForm(AppTestCase):
    def make_unapproved_project(self):
        other = self.workspace / "soup-game"
        other.mkdir()
        (other / "SCOPE.md").write_text(template_scope_text(),
                                        encoding="utf-8")
        state.init_state(other)
        return other

    def test_scope_page_shows_fields_and_missing_count(self):
        self.make_unapproved_project()
        status, page = get(self.base + "/project/soup-game/scope")
        self.assertEqual(status, 200)
        self.assertIn("Title (working)", page)
        self.assertIn("answers to go", page)
        self.assertIn("Approve scope", page)
        self.assertIn("disabled", page)  # approve locked while incomplete

    def test_scope_save_round_trips_into_the_file(self):
        other = self.make_unapproved_project()
        status, page = post(self.base + "/project/soup-game/scope",
                            {"Title (working)": "Soup Quest"})
        self.assertEqual(status, 200)
        parsed = scope.load_scope(other / "SCOPE.md")
        values = {f.name: f.value for f in parsed.fields}
        self.assertEqual(values["Title (working)"], "Soup Quest")
        # Structure preserved: the untouched fields are all still there.
        self.assertIn("Genre / archetype", values)
        self.assertIn("## Style lock", (other / "SCOPE.md").read_text(
            encoding="utf-8"))

    def test_extract_without_a_brain_still_stores_the_prompt(self):
        other = self.make_unapproved_project()
        status, page = post(self.base + "/project/soup-game/scope/extract",
                            {"prompt": "A tiny game about soup"})
        self.assertEqual(status, 200)
        self.assertIn("Prompt stored", page)
        parsed = scope.load_scope(other / "SCOPE.md")
        self.assertIn("soup", parsed.original_prompt)
        self.assertFalse(parsed.is_complete)


class TestSettingsPage(AppTestCase):
    def test_settings_page_without_a_file_explains(self):
        status, page = get(self.base + "/settings")
        self.assertEqual(status, 200)
        self.assertIn("global-settings.yaml", page)

    def test_budget_edit_saves_and_validates(self):
        settings_text = (
            "budgets:\n"
            "  tokens_per_run: 1000\n"
            "  tokens_per_game: 5000\n"
            "  cost_ceiling_per_month: null\n"
            "  max_build_iterations: 40\n"
            "  warn_at_percent_of_cap: 80\n"
            "models:\n"
            "  cheap: {provider: openai, model: '', api_key_env: OPENAI_API_KEY}\n"
        )
        (self.workspace / "global-settings.yaml").write_text(
            settings_text, encoding="utf-8")
        status, page = get(self.base + "/settings")
        self.assertEqual(status, 200)
        self.assertIn("OPENAI_API_KEY", page)
        self.assertNotIn("sk-", page)  # never a key value (none set anyway)
        status, page = post(self.base + "/settings", {
            "tokens_per_run": "2000", "tokens_per_game": "9000",
            "cost_ceiling_per_month": "", "max_build_iterations": "40",
            "warn_at_percent_of_cap": "80"})
        self.assertEqual(status, 200)
        self.assertIn("saved", page.lower())
        import yaml
        saved = yaml.safe_load(
            (self.workspace / "global-settings.yaml").read_text(
                encoding="utf-8"))
        self.assertEqual(saved["budgets"]["tokens_per_game"], 9000)
        self.assertEqual(saved["models"]["cheap"]["provider"], "openai")


if __name__ == "__main__":
    unittest.main()
