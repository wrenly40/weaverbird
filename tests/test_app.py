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

import yaml  # noqa: E402

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


class TestSettingsLadder(AppTestCase):
    """0.9.0 (S): the settings form edits the full file — budgets
    AND the model ladder — in the workspace-root file only, and
    can create that file from the shipped template."""

    def setUp(self):
        super().setUp()
        self.settings_path = self.workspace / "global-settings.yaml"
        self.settings_path.write_text(
            "budgets:\n"
            "  tokens_per_run: 4000\n"
            "  tokens_per_game: 9000\n"
            "  cost_ceiling_per_month: null\n"
            "  max_build_iterations: 3\n"
            "  warn_at_percent_of_cap: 80\n"
            "models:\n"
            "  cheap: {provider: openai, model: '', base_url: '', api_key_env: OPENAI_API_KEY, cost_per_1k_tokens: 0.0}\n",
            encoding="utf-8")

    def _budgets(self):
        return {"tokens_per_run": "4000", "tokens_per_game": "9000",
                "cost_ceiling_per_month": "", "max_build_iterations": "3",
                "warn_at_percent_of_cap": "80"}

    def test_ladder_fields_render(self):
        status, page = get(self.base + "/settings")
        self.assertEqual(status, 200)
        self.assertIn('name="m_cheap_provider"', page)
        self.assertIn('name="m_mid_model"', page)
        self.assertIn('name="m_frontier_api_key_env"', page)
        self.assertIn('value="OPENAI_API_KEY"', page)

    def test_ladder_save_roundtrip_and_dashboard_reflects_it(self):
        import os
        os.environ.pop("WB_TEST_KEY", None)
        fields = self._budgets()
        fields.update({
            "m_cheap_provider": "openai", "m_cheap_model": "test-model",
            "m_cheap_base_url": "", "m_cheap_api_key_env": "WB_TEST_KEY",
            "m_cheap_cost_per_1k_tokens": "0.002"})
        status, page = post(self.base + "/settings", fields)
        self.assertEqual(status, 200)
        self.assertIn("saved", page)
        data = yaml.safe_load(self.settings_path.read_text())
        cheap = data["models"]["cheap"]
        self.assertEqual(cheap["provider"], "openai")
        self.assertEqual(cheap["model"], "test-model")
        self.assertEqual(cheap["api_key_env"], "WB_TEST_KEY")
        self.assertEqual(cheap["cost_per_1k_tokens"], 0.002)
        # the form shows the saved tier, with the honest key state
        status, page = get(self.base + "/settings")
        self.assertIn('value="test-model"', page)
        self.assertIn("WB_TEST_KEY — not set", page)
        # and the dashboard brain card derives the same status
        status, page = get(self.base + "/")
        self.assertIn("cheap — configured, key missing (WB_TEST_KEY)",
                      page)

    def test_local_provider_needs_no_key(self):
        fields = self._budgets()
        fields.update({
            "m_mid_provider": "local", "m_mid_model": "llama3",
            "m_mid_base_url": "", "m_mid_api_key_env": "",
            "m_mid_cost_per_1k_tokens": ""})
        status, page = post(self.base + "/settings", fields)
        self.assertEqual(status, 200)
        self.assertIn("saved", page)
        data = yaml.safe_load(self.settings_path.read_text())
        mid = data["models"]["mid"]
        self.assertEqual(mid["provider"], "openai")
        self.assertEqual(mid["model"], "llama3")
        self.assertEqual(mid["base_url"], "http://localhost:11434/v1")
        status, page = get(self.base + "/")
        self.assertIn("mid — openai/llama3, key set", page)

    def test_tier_off_clears_the_model(self):
        fields = self._budgets()
        fields.update({
            "m_cheap_provider": "", "m_cheap_model": "ignored",
            "m_cheap_base_url": "", "m_cheap_api_key_env": "",
            "m_cheap_cost_per_1k_tokens": ""})
        post(self.base + "/settings", fields)
        data = yaml.safe_load(self.settings_path.read_text())
        self.assertEqual(data["models"]["cheap"]["model"], "")

    def test_invalid_save_leaves_the_file_byte_identical(self):
        before = self.settings_path.read_bytes()
        fields = self._budgets()
        fields["tokens_per_run"] = "-5"
        status, page = post(self.base + "/settings", fields)
        self.assertEqual(status, 200)
        self.assertIn("Not saved", page)
        self.assertEqual(self.settings_path.read_bytes(), before)

    def test_bad_key_env_name_refused_and_file_untouched(self):
        before = self.settings_path.read_bytes()
        fields = self._budgets()
        fields.update({
            "m_cheap_provider": "openai", "m_cheap_model": "m",
            "m_cheap_base_url": "",
            "m_cheap_api_key_env": "sk-not-a-name!",
            "m_cheap_cost_per_1k_tokens": ""})
        status, page = post(self.base + "/settings", fields)
        self.assertEqual(status, 200)
        self.assertIn("Not saved", page)
        self.assertIn("never the key itself", page)
        self.assertEqual(self.settings_path.read_bytes(), before)

    def test_a_posted_key_value_is_never_persisted(self):
        secret = "sk-live-SECRET-0123456789"
        fields = self._budgets()
        fields.update({
            "api_key": secret, "m_cheap_api_key": secret,
            "OPENAI_API_KEY": secret,
            "m_cheap_provider": "openai", "m_cheap_model": "test-model",
            "m_cheap_base_url": "", "m_cheap_api_key_env": "WB_TEST_KEY",
            "m_cheap_cost_per_1k_tokens": ""})
        status, page = post(self.base + "/settings", fields)
        self.assertEqual(status, 200)
        self.assertNotIn(secret, self.settings_path.read_text())
        self.assertNotIn(secret, page)
        status, page = get(self.base + "/settings")
        self.assertNotIn(secret, page)
        status, page = get(self.base + "/")
        self.assertNotIn(secret, page)

    def test_project_level_file_is_noted_as_taking_precedence(self):
        (self.project / "global-settings.yaml").write_text(
            "budgets:\n  tokens_per_run: 1\n  tokens_per_game: 2\n",
            encoding="utf-8")
        status, page = get(self.base + "/settings")
        self.assertEqual(status, 200)
        self.assertIn("takes", page)
        self.assertIn("precedence", page)
        self.assertIn("bee-game", page)


class TestSettingsCreation(AppTestCase):
    """No settings file anywhere: the page offers one-click
    creation from the shipped template."""

    def test_create_writes_the_template_to_the_workspace_root(self):
        from weaverbird import template_text
        root = self.workspace / "global-settings.yaml"
        self.assertFalse(root.exists())
        status, page = get(self.base + "/settings")
        self.assertEqual(status, 200)
        self.assertIn("Create settings file", page)
        self.assertIn('action="/settings/create"', page)
        status, page = post(self.base + "/settings/create", {})
        self.assertEqual(status, 200)
        self.assertIn("Settings file created", page)
        self.assertTrue(root.exists())
        self.assertEqual(root.read_text(encoding="utf-8"),
                         template_text("global-settings.yaml"))
        # and the editable form is what we landed on
        self.assertIn('name="m_cheap_provider"', page)
        self.assertIn('name="tokens_per_run"', page)


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


class TestNewProject(AppTestCase):
    """0.9.0 (R): a project can be created entirely in the app —
    the dashboard's New project form runs the same scaffold as
    `weaverbird init`, seeds the scope title from the maker's
    name for the game, stores an optional starting prompt for
    the interview, and refuses cleanly (nothing left on disk)
    when the folder cannot be used."""

    def _raw_post(self, path, fields):
        import http.client
        host, port = self.server.server_address[:2]
        conn = http.client.HTTPConnection(host, port, timeout=30)
        conn.request("POST", path,
                     body=urllib.parse.urlencode(fields),
                     headers={"Content-Type":
                              "application/x-www-form-urlencoded"})
        response = conn.getresponse()
        body = response.read().decode("utf-8")
        result = (response.status, response.getheader("Location"), body)
        conn.close()
        return result

    def test_dashboard_has_a_new_project_button(self):
        status, page = get(self.base + "/")
        self.assertEqual(status, 200)
        self.assertIn('href="/new"', page)
        self.assertIn("New project", page)

    def test_empty_workspace_leads_with_new_project(self):
        import tempfile as _tempfile
        tmp = _tempfile.mkdtemp()
        empty = Path(tmp) / "empty-ws"
        empty.mkdir(parents=True)
        server = app_module.create_server(empty, port=0)
        thread = threading.Thread(target=server.serve_forever,
                                  daemon=True)
        thread.start()
        try:
            status, page = get(server.url.rstrip("/") + "/")
            self.assertEqual(status, 200)
            self.assertIn("No projects in this workspace", page)
            self.assertIn('href="/new"', page)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_form_renders(self):
        status, page = get(self.base + "/new")
        self.assertEqual(status, 200)
        self.assertIn('name="name"', page)
        self.assertIn('name="folder"', page)
        self.assertIn('name="prompt"', page)

    def test_create_without_prompt_redirects_to_the_project_page(self):
        status, location, _body = self._raw_post(
            "/new", {"name": "Star Shepherd", "folder": "",
                     "prompt": ""})
        self.assertEqual(status, 303)
        self.assertEqual(location, "/project/star-shepherd")
        target = self.workspace / "star-shepherd"
        self.assertTrue((target / "SCOPE.md").exists())
        self.assertEqual(state.current_stage(target), "scope")
        parsed = scope.load_scope(target / "SCOPE.md")
        title = next(f for f in parsed.fields
                     if f.name == "Title (working)")
        self.assertEqual(title.value, "Star Shepherd")
        # and the dashboard card shows the seeded title, not the slug
        status, page = get(self.base + "/")
        self.assertIn("Star Shepherd", page)

    def test_create_with_prompt_redirects_to_the_interview(self):
        status, location, _body = self._raw_post(
            "/new", {"name": "Star Shepherd", "folder": "ss",
                     "prompt": "A herding game in space."})
        self.assertEqual(status, 303)
        self.assertEqual(location, "/project/ss/scope")
        parsed = scope.load_scope(self.workspace / "ss" / "SCOPE.md")
        self.assertIn("A herding game in space.",
                      parsed.original_prompt)
        title = next(f for f in parsed.fields
                     if f.name == "Title (working)")
        self.assertEqual(title.value, "Star Shepherd")

    def test_duplicate_folder_rerenders_with_error_and_creates_nothing(self):
        before = (self.project / "SCOPE.md").read_bytes()
        status, _location, body = self._raw_post(
            "/new", {"name": "Bee Game", "folder": "bee-game",
                     "prompt": ""})
        self.assertEqual(status, 200)
        self.assertIn("Not created", body)
        self.assertIn("already exists", body)
        # the existing project is untouched
        self.assertEqual((self.project / "SCOPE.md").read_bytes(), before)

    def test_bad_folder_rerenders_and_no_partial_folder_survives(self):
        before = sorted(p.name for p in self.workspace.iterdir())
        status, _location, body = self._raw_post(
            "/new", {"name": "Bad", "folder": "Not A Folder",
                     "prompt": ""})
        self.assertEqual(status, 200)
        self.assertIn("Not created", body)
        self.assertEqual(
            sorted(p.name for p in self.workspace.iterdir()), before)

    def test_reserved_folder_refused(self):
        status, _location, body = self._raw_post(
            "/new", {"name": "Sad", "folder": "global-settings.yaml",
                     "prompt": ""})
        self.assertEqual(status, 200)
        self.assertIn("reserved", body)
        self.assertFalse((self.workspace / "global-settings.yaml"
                          / "SCOPE.md").exists())

    def test_missing_name_refused(self):
        status, _location, body = self._raw_post(
            "/new", {"name": "", "folder": "", "prompt": ""})
        self.assertEqual(status, 200)
        self.assertIn("Not created", body)
        self.assertEqual(
            sorted(p.name for p in self.workspace.iterdir()),
            ["bee-game"])
