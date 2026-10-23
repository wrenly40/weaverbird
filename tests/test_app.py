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


def wait_for_job(base, body, timeout=60):
    """Actions are jobs now: pull the job id out of the returned
    page's live card and poll the JSON API until it lands."""
    import re
    import time as _time
    match = re.search(r'data-job-id="([^"]+)"', body)
    assert match, "the page carries no job card"
    job_id = match.group(1)
    deadline = _time.monotonic() + timeout
    while _time.monotonic() < deadline:
        _status, payload = get(f"{base}/api/jobs/{job_id}")
        job = json.loads(payload)
        if job["status"] in ("done", "failed", "cancelled",
                             "interrupted"):
            return job
        _time.sleep(0.1)
    raise AssertionError(f"job {job_id} did not finish in {timeout}s")


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
        job = wait_for_job(self.base, page)
        self.assertEqual(job["status"], "done")
        self.assertIn("Finish check done", job["result"])
        _status, page = get(self.base + "/project/bee-game")
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
        # The scope page is the interview chat now: its live panel
        # shows every field, the missing count, and the approve
        # button (locked while incomplete). The form lives at
        # /scope/form with the same information.
        status, page = get(self.base + "/project/soup-game/scope")
        self.assertEqual(status, 200)
        self.assertIn("Title (working)", page)
        self.assertIn("Specified 0 of", page)
        self.assertIn("missing", page)
        self.assertIn("Approve scope", page)
        self.assertIn("disabled", page)  # approve locked while incomplete
        status, form_page = get(self.base + "/project/soup-game/scope/form")
        self.assertEqual(status, 200)
        self.assertIn("answers to go", form_page)
        self.assertIn('name="Title (working)"', form_page)

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


class TestJobActionRedirects(AppTestCase):
    """Redirect-after-POST for every job action: a POST that starts
    a job must answer 303 to the GET page the button lives on,
    carrying ?job=<id> — never render the page at the POST URL,
    whose GET is a 404 the job card's reload would land on."""

    def _raw(self, method, path, fields=None):
        import http.client
        host, port = self.server.server_address[:2]
        conn = http.client.HTTPConnection(host, port, timeout=30)
        body = urllib.parse.urlencode(fields or {})
        headers = ({"Content-Type": "application/x-www-form-urlencoded"}
                   if method == "POST" else {})
        conn.request(method, path, body=body if method == "POST" else None,
                     headers=headers)
        response = conn.getresponse()
        response.read()
        result = (response.status, response.getheader("Location"))
        conn.close()
        return result

    def assert_redirects_to_job_page(self, path, fields, page_path):
        status, location = self._raw("POST", path, fields)
        self.assertEqual(status, 303, path)
        self.assertIsNotNone(location, path)
        parsed = urllib.parse.urlparse(location)
        self.assertEqual(parsed.path, page_path, path)
        job_id = urllib.parse.parse_qs(parsed.query)["job"][0]
        status, page = get(self.base + location)
        self.assertEqual(status, 200, location)
        self.assertIn(f'data-job-id="{job_id}"', page)
        return job_id

    def test_run_tests_redirects_to_project_page(self):
        self.assert_redirects_to_job_page(
            "/project/bee-game/run-tests", {}, "/project/bee-game")

    def test_build_skeleton_redirects_to_project_page(self):
        self.assert_redirects_to_job_page(
            "/project/bee-game/build-skeleton", {}, "/project/bee-game")

    def test_finish_check_redirects_to_project_page(self):
        self.assert_redirects_to_job_page(
            "/project/bee-game/finish-check", {}, "/project/bee-game")

    def test_balance_compare_redirects_to_balance_page(self):
        self.assert_redirects_to_job_page(
            "/project/bee-game/balance/compare", {"suggestion": "0"},
            "/project/bee-game/balance")

    def test_assets_regenerate_redirects_to_assets_page(self):
        self.assert_redirects_to_job_page(
            "/project/bee-game/assets/regenerate", {"asset": "player"},
            "/project/bee-game/assets")

    def test_feedback_triage_redirects_to_feedback_page(self):
        self.assert_redirects_to_job_page(
            "/project/bee-game/feedback/triage", {},
            "/project/bee-game/feedback")

    def test_patch_start_redirects_to_feedback_page(self):
        self.assert_redirects_to_job_page(
            "/project/bee-game/patch/some-item", {},
            "/project/bee-game/feedback")

    def test_post_url_itself_is_not_a_page(self):
        # Nothing links to the bare POST URL any more; its GET is a
        # 404, which is exactly why the browser must never land there.
        status, _location = self._raw("GET", "/project/bee-game/run-tests")
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()


class TestGenerationAndTestCards(AppTestCase):
    """The v0.4 project view: generation/test cards and stage actions."""

    def _add_reports(self):
        import json as _json
        wb = self.project / ".weaverbird"
        (wb / "generation_report.json").write_text(_json.dumps({
            "archetype": "platformer",
            "archetype_reason": "the scope's genre mentions platforms",
            "units_delivered": 3, "units_requested": 3,
            "brain_used": False, "brain_note": "no model configured",
            "validation": {"passed": True},
        }), encoding="utf-8")
        (wb / "assets_report.json").write_text(_json.dumps({
            "files": ["game/assets/player.svg"] * 19,
            "palette": {"accent_hex": "#112233", "target_hex": "#AABBCC"},
        }), encoding="utf-8")
        (wb / "test_report.json").write_text(_json.dumps({
            "ok": True, "win_proven": True, "lose_proven": True,
            "performance": {"avg_fps": 121.0, "target_fps": 30,
                            "load_time_s": 0.31},
            "playtesters": {
                "competent": {"ok": True,
                              "win": {"time_s": 21.5, "deaths": 0},
                              "lose": {"time_s": 4.0}},
                "breaker": {"ok": True, "time_s": 30.0,
                            "findings": ["pause overlay opened and closed"]},
                "first_time": {"ok": True, "time_s": 40.0,
                               "first_objective_s": 1.2, "finished": True,
                               "findings": []},
            },
            "runs": [],
        }), encoding="utf-8")

    def test_project_page_shows_generation_and_test_cards(self):
        self._add_reports()
        status, page = get(self.base + "/project/bee-game")
        self.assertEqual(status, 200)
        self.assertIn("Archetype", page)
        self.assertIn("platformer", page)
        self.assertIn("Content delivered", page)
        self.assertIn("Test summary", page)
        self.assertIn("Win proven: yes", page)
        self.assertIn("First-time player", page)
        self.assertIn("Generate assets", page)
        self.assertIn("Build game", page)
        self.assertIn("Run tests", page)

    def test_stage_buttons_disabled_with_reasons_before_skeleton(self):
        status, page = get(self.base + "/project/bee-game")
        self.assertEqual(status, 200)
        self.assertIn("Build the skeleton first", page)
        self.assertIn("Build the game first", page)

    def test_generate_assets_action_works(self):
        from weaverbird import skeleton as skeleton_module
        skeleton_module.build_skeleton(self.project, validate=False)
        status, page = post(self.base + "/project/bee-game/generate-assets",
                            {})
        self.assertEqual(status, 200)
        job = wait_for_job(self.base, page)
        self.assertEqual(job["status"], "done")
        self.assertIn("Assets generated", job["result"])
        self.assertTrue(
            (self.project / ".weaverbird" / "assets_report.json").exists())
        self.assertTrue(
            (self.project / "game" / "assets" / "player.svg").exists())

    def test_run_tests_action_reports_refusal_before_build(self):
        status, page = post(self.base + "/project/bee-game/run-tests", {})
        self.assertEqual(status, 200)
        job = wait_for_job(self.base, page)
        self.assertEqual(job["status"], "failed")
        self.assertIn("No game found", job["error"])
