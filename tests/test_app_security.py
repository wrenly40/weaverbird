"""Security regression tests for the app (0.13.0).

Covers the live-probe findings of ~/workspace/.secreview:
F1 (cross-site POSTs and foreign Host headers must not act),
F4 (job-id traversal must not read files outside the job store),
F7 (security response headers; no version in the Server banner).
Requests are made with http.client so headers are fully explicit
— urllib would hide exactly the headers under test.
"""

import http.client
import json
import sys
import tempfile
import threading
import unittest
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import filled_scope_text  # noqa: E402

from weaverbird import __version__  # noqa: E402
from weaverbird import app as app_module  # noqa: E402
from weaverbird import jobs as jobs_module  # noqa: E402
from weaverbird import scope, state  # noqa: E402

EVIL = {"Origin": "https://evil.example",
        "Referer": "https://evil.example/attack.html",
        "Sec-Fetch-Site": "cross-site"}


class AppSecurityTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.workspace = Path(self.tmp) / "workspace"
        self.workspace.mkdir(parents=True)
        self.project = self.workspace / "bee-game"
        self.project.mkdir()
        (self.project / "SCOPE.md").write_text(filled_scope_text(),
                                               encoding="utf-8")
        scope.approve_scope(self.project / "SCOPE.md", by="Tester")
        state.init_state(self.project)
        self.server = app_module.create_server(self.workspace, port=0)
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def raw(self, method, path, fields=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port,
                                          timeout=15)
        body = None
        hdrs = dict(headers or {})
        if fields is not None:
            body = urllib.parse.urlencode(fields)
            hdrs.setdefault("Content-Type",
                            "application/x-www-form-urlencoded")
        conn.request(method, path, body=body, headers=hdrs)
        response = conn.getresponse()
        text = response.read().decode("utf-8", "replace")
        result = (response.status, dict(response.getheaders()), text)
        conn.close()
        return result


class TestCrossSitePosts(AppSecurityTestCase):
    """F1: a cross-site form POST executes nothing."""

    def test_cross_site_create_is_refused_and_creates_nothing(self):
        status, _h, body = self.raw(
            "POST", "/new", {"name": "CSRF Created"}, headers=EVIL)
        self.assertEqual(status, 403)
        self.assertIn("different site", body)
        self.assertFalse((self.workspace / "csrf-created").exists())

    def test_cross_site_settings_save_is_refused(self):
        settings = self.workspace / "global-settings.yaml"
        settings.write_text("budgets:\n  tokens_per_game: 10\n",
                            encoding="utf-8")
        before = settings.read_text(encoding="utf-8")
        status, _h, _b = self.raw(
            "POST", "/settings",
            {"tokens_per_game": "222222",
             "m_cheap_provider": "openai", "m_cheap_model": "csrf-model",
             "m_cheap_base_url": "https://evil.example/v1",
             "m_cheap_api_key_env": "EVIL_KEY_ENV"}, headers=EVIL)
        self.assertEqual(status, 403)
        self.assertEqual(settings.read_text(encoding="utf-8"), before)

    def test_cross_site_delete_is_refused_and_project_survives(self):
        status, _h, _b = self.raw(
            "POST", "/project/bee-game/delete",
            {"confirm_name": "bee-game"}, headers=EVIL)
        self.assertEqual(status, 403)
        self.assertTrue((self.project / "SCOPE.md").exists())

    def test_cross_site_feedback_add_is_refused(self):
        status, _h, _b = self.raw(
            "POST", "/project/bee-game/feedback/add",
            {"text": "planted by a cross-site form"}, headers=EVIL)
        self.assertEqual(status, 403)
        store = self.project / ".weaverbird" / "feedback.json"
        if store.exists():
            self.assertNotIn("planted by a cross-site form",
                             store.read_text(encoding="utf-8"))

    def test_sec_fetch_site_cross_site_alone_is_refused(self):
        status, _h, _b = self.raw(
            "POST", "/new", {"name": "Fetch Site"},
            headers={"Sec-Fetch-Site": "cross-site"})
        self.assertEqual(status, 403)
        self.assertFalse((self.workspace / "fetch-site").exists())

    def test_no_origin_post_still_works(self):
        # curl / CLI / the existing suite: no Origin header at all.
        status, headers, _b = self.raw(
            "POST", "/new", {"name": "Local Tools"})
        self.assertEqual(status, 303)
        self.assertTrue((self.workspace / "local-tools").is_dir())

    def test_own_origin_post_still_works(self):
        status, _h, _b = self.raw(
            "POST", "/new", {"name": "Own Origin"},
            headers={"Origin": f"http://127.0.0.1:{self.port}"})
        self.assertEqual(status, 303)
        self.assertTrue((self.workspace / "own-origin").is_dir())


class TestHostHeader(AppSecurityTestCase):
    """F1: DNS-rebinding defence — foreign Host refused on
    GET and POST alike."""

    def test_foreign_host_get_refused(self):
        status, _h, _b = self.raw("GET", "/",
                                  headers={"Host": "evil.example"})
        self.assertEqual(status, 403)

    def test_foreign_host_post_refused_and_creates_nothing(self):
        status, _h, _b = self.raw(
            "POST", "/new", {"name": "Host CSRF"},
            headers={"Host": "evil.example"})
        self.assertEqual(status, 403)
        self.assertFalse((self.workspace / "host-csrf").exists())

    def test_loopback_host_with_wrong_port_refused(self):
        status, _h, _b = self.raw(
            "GET", "/", headers={"Host": "127.0.0.1:9999"})
        self.assertEqual(status, 403)

    def test_localhost_name_accepted(self):
        status, _h, body = self.raw(
            "GET", "/", headers={"Host": f"localhost:{self.port}"})
        self.assertEqual(status, 200)
        self.assertIn("Projects", body)


class TestSecurityHeaders(AppSecurityTestCase):
    """F7: every response carries the headers; the Server banner
    no longer advertises the version."""

    def test_headers_on_html(self):
        status, headers, _b = self.raw("GET", "/")
        self.assertEqual(status, 200)
        csp = headers.get("Content-Security-Policy", "")
        self.assertIn("default-src 'self'", csp)
        self.assertIn("frame-ancestors 'none'", csp)
        self.assertIn("script-src 'self'", csp)
        self.assertNotIn("script-src 'self' 'unsafe-inline'", csp)
        self.assertEqual(headers.get("X-Content-Type-Options"),
                         "nosniff")
        self.assertEqual(headers.get("X-Frame-Options"), "DENY")
        server = headers.get("Server", "")
        self.assertNotIn(__version__, server)
        self.assertNotIn("Python", server)

    def test_headers_on_json_and_assets(self):
        _s, headers, _b = self.raw("GET", "/api/jobs")
        self.assertIn("Content-Security-Policy", headers)
        _s, headers, _b = self.raw("GET", "/app.js")
        self.assertIn("Content-Security-Policy", headers)
        self.assertEqual(headers.get("X-Content-Type-Options"),
                         "nosniff")


class TestJobTraversal(AppSecurityTestCase):
    """F4: job ids cannot walk out of the job store."""

    def _plant_canary(self):
        canary = self.workspace / "canary.json"
        canary.write_text(json.dumps(
            {"id": "canary", "project": "CANARY-PROJECT",
             "label": "CANARY-LABEL-SECRET"}), encoding="utf-8")
        return canary

    def test_api_job_traversal_404(self):
        self._plant_canary()
        for raw_id in ("..%2f..%2fcanary", "%2e%2e%2f%2e%2e%2fcanary",
                       "..%2fcanary"):
            status, _h, body = self.raw("GET", f"/api/jobs/{raw_id}")
            self.assertEqual(status, 404, raw_id)
            self.assertNotIn("CANARY-LABEL-SECRET", body)

    def test_job_page_traversal_shows_no_canary(self):
        self._plant_canary()
        _s, _h, body = self.raw("GET", "/jobs/..%2f..%2fcanary")
        self.assertNotIn("CANARY-LABEL-SECRET", body)
        self.assertNotIn("CANARY-PROJECT", body)

    def test_cancel_traversal_404_and_no_stray_file(self):
        self._plant_canary()
        status, _h, _b = self.raw(
            "POST", "/api/jobs/..%2f..%2fcanary/cancel", {})
        self.assertEqual(status, 404)
        store_dir = jobs_module.jobs_dir(self.workspace)
        if store_dir.is_dir():
            self.assertEqual(list(store_dir.glob("canary*.json")), [])

    def test_real_job_id_still_served(self):
        store = jobs_module.JobStore(self.workspace)
        store.save({"id": "job-20261024120000-abcdef",
                    "project": "bee-game", "label": "Real job",
                    "status": "done", "events": []})
        status, _h, body = self.raw(
            "GET", "/api/jobs/job-20261024120000-abcdef")
        self.assertEqual(status, 200)
        self.assertIn("Real job", body)


class TestJobIdValidation(unittest.TestCase):
    """F4 at the store boundary itself."""

    def test_path_for_rejects_traversal(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = jobs_module.JobStore(tmp)
            for bad in ("../../canary", "/tmp/wb-canary", "..", "",
                        "a/b", "a\\b", "job-\x00x"):
                with self.assertRaises(ValueError, msg=repr(bad)):
                    store.path_for(bad)
                self.assertIsNone(store.load(bad))

    def test_path_for_accepts_factory_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = jobs_module.JobStore(tmp)
            path = store.path_for("job-20261024120000-abcdef")
            self.assertEqual(path.parent,
                             jobs_module.jobs_dir(tmp).resolve())


if __name__ == "__main__":
    unittest.main()
