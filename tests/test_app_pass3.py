"""App tests for Pass 3: the Feedback view (list, fetch, triage,
add, approve/dismiss), the patch action's refusal path, and the
repair / model-playtest buttons failing plainly on a project with
no game yet.
"""

import sys
import tempfile
import threading
import unittest
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import filled_scope_text  # noqa: E402,F401  (sets sys.path)

from weaverbird import app as app_module  # noqa: E402
from weaverbird import feedback, scope, state  # noqa: E402


def get(url):
    with urllib.request.urlopen(url, timeout=15) as response:
        return response.status, response.read().decode("utf-8")


def post(url, fields=None):
    data = urllib.parse.urlencode(fields or {}).encode("utf-8")
    request = urllib.request.Request(url, data=data, method="POST")
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.status, response.read().decode("utf-8")


def wait_for_job(base, body, timeout=60):
    """Actions are jobs now: pull the job id out of the returned
    page's live card and poll the JSON API until it lands."""
    import json as _json
    import re
    import time as _time
    match = re.search(r'data-job-id="([^"]+)"', body)
    assert match, "the page carries no job card"
    job_id = match.group(1)
    deadline = _time.monotonic() + timeout
    while _time.monotonic() < deadline:
        _status, payload = get(f"{base}/api/jobs/{job_id}")
        job = _json.loads(payload)
        if job["status"] in ("done", "failed", "cancelled",
                             "interrupted"):
            return job
        _time.sleep(0.1)
    raise AssertionError(f"job {job_id} did not finish in {timeout}s")


class Pass3AppTests(unittest.TestCase):
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

    def test_project_page_links_feedback(self):
        _status, body = get(f"{self.base}/project/bee-game")
        self.assertIn("/project/bee-game/feedback", body)

    def test_feedback_flow(self):
        base = f"{self.base}/project/bee-game/feedback"
        status, body = get(base)
        self.assertEqual(status, 200)
        self.assertIn("No feedback yet", body)

        status, body = post(base + "/add",
                            {"text": "The timer is way too fast, unfair"})
        self.assertEqual(status, 200)
        self.assertIn("fb-001", body)

        status, body = post(base + "/triage")
        self.assertEqual(status, 200)
        job = wait_for_job(self.base, body)
        self.assertEqual(job["status"], "done")
        self.assertIn("Triage done", job["result"])
        status, body = get(base)
        self.assertIn("balance", body)

        status, body = post(base + "/fb-001/approve")
        self.assertEqual(status, 200)
        item = feedback.get_item(self.project, "fb-001")
        self.assertEqual(item["status"], "approved")
        self.assertIn("Ship as patch", body)

        status, body = post(base + "/fb-001/dismiss")
        self.assertEqual(status, 200)
        self.assertEqual(feedback.get_item(self.project, "fb-001")["status"],
                         "dismissed")

    def test_feedback_fetch_reports_sources(self):
        inbox = self.project / "feedback-inbox"
        inbox.mkdir()
        (inbox / "note.md").write_text("# It crashes on start\nEvery time.\n",
                                       encoding="utf-8")
        status, body = post(f"{self.base}/project/bee-game/feedback/fetch")
        self.assertEqual(status, 200)
        job = wait_for_job(self.base, body)
        self.assertEqual(job["status"], "done")
        self.assertIn("Fetch done", job["result"])
        store = feedback.load_store(self.project)
        self.assertEqual(len(store["items"]), 1)

    def test_patch_refused_without_game(self):
        item, _ = feedback.add_item(self.project, source="manual",
                                    text="too hard")
        feedback.update_item(self.project, item["id"], category="balance",
                             status="approved")
        status, body = post(
            f"{self.base}/project/bee-game/patch/{item['id']}")
        self.assertEqual(status, 200)
        job = wait_for_job(self.base, body)
        self.assertEqual(job["status"], "failed")
        self.assertIn("game_config", job["error"])

    def test_repair_and_model_buttons_fail_plainly(self):
        status, body = post(f"{self.base}/project/bee-game/repair")
        self.assertEqual(status, 200)
        job = wait_for_job(self.base, body)
        self.assertEqual(job["status"], "failed")
        self.assertIn("No built game found", job["error"])
        status, body = post(f"{self.base}/project/bee-game/model-playtest")
        self.assertEqual(status, 200)
        job = wait_for_job(self.base, body)
        self.assertEqual(job["status"], "failed")
        self.assertIn("No game found", job["error"])


if __name__ == "__main__":
    unittest.main()
