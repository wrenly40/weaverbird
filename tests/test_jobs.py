"""Tests for the job runner (jobs.py): lifecycle with fake
actions, persistence, failure records, cooperative cancel
between steps, the interrupted-on-load rewrite, per-project
serialisation, the app's job endpoints, and one real Godot
test-run job (gated).
"""

import json
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import filled_scope_text  # noqa: E402,F401  (sets sys.path)

from weaverbird import app as app_module  # noqa: E402
from weaverbird import jobs  # noqa: E402
from weaverbird import state  # noqa: E402
from weaverbird import godot as godot_module  # noqa: E402

GODOT_PRESENT = godot_module.find_godot()[0] is not None
TERMINAL = ("done", "failed", "cancelled", "interrupted")


def wait_job(manager, job_id, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = manager.get(job_id)
        if job["status"] in TERMINAL:
            return job
        time.sleep(0.02)
    raise AssertionError(f"job {job_id} did not finish")


class JobManagerTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self._tmp.name)
        self.manager = jobs.JobManager(self.workspace)

    def tearDown(self):
        self.manager.shutdown()
        self._tmp.cleanup()

    def test_lifecycle_done_and_persisted(self):
        def fake(ctx):
            ctx.reporter("step one", done=1, total=2)
            ctx.reporter("step two", done=2, total=2)
            return "all good"

        job = self.manager.submit("proj-a", self.workspace / "proj-a",
                                  "run-tests", runner=fake)
        self.assertEqual(job["status"], "queued")
        done = wait_job(self.manager, job["id"])
        self.assertEqual(done["status"], "done")
        self.assertEqual(done["result"], "all good")
        # The record names the worker that ran the action — the
        # Pass 5 orchestrator keys its plan view off this field.
        self.assertEqual(done["worker"], "testing")
        self.assertIsNotNone(done["started"])
        self.assertIsNotNone(done["finished"])
        texts = [e["text"] for e in done["events"]]
        self.assertTrue(any("step one" in t for t in texts))
        # Reloaded from disk by a fresh store: history survives.
        fresh = jobs.JobStore(self.workspace).load(job["id"])
        self.assertEqual(fresh["status"], "done")
        self.assertEqual(fresh["result"], "all good")
        self.assertEqual(fresh["action"], "run-tests")

    def test_failure_records_the_error(self):
        def fake(ctx):
            raise RuntimeError("boom")

        job = self.manager.submit("proj-a", self.workspace / "proj-a",
                                  "run-tests", runner=fake)
        failed = wait_job(self.manager, job["id"])
        self.assertEqual(failed["status"], "failed")
        self.assertIn("RuntimeError: boom", failed["error"])
        self.assertIsNone(failed["result"])

    def test_cancel_lands_between_steps(self):
        def stepping(ctx):
            for i in range(500):
                ctx.reporter(f"step {i}", done=i, total=500)
                time.sleep(0.01)
            return "ran to the end"

        job = self.manager.submit("proj-a", self.workspace / "proj-a",
                                  "run-tests", runner=stepping)
        deadline = time.monotonic() + 10
        while self.manager.get(job["id"])["status"] != "running":
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.01)
        self.manager.cancel(job["id"])
        landed = wait_job(self.manager, job["id"])
        self.assertEqual(landed["status"], "cancelled")
        self.assertEqual(landed["result"],
                         jobs.CANCELLED_BETWEEN_STEPS)
        texts = [e["text"] for e in landed["events"]]
        self.assertTrue(any("Cancel requested" in t for t in texts))

    def test_cancel_queued_job_never_runs(self):
        release = threading.Event()
        ran = []

        def blocker(ctx):
            release.wait(timeout=10)
            return "blocker done"

        def second(ctx):
            ran.append(True)
            return "second ran"

        first = self.manager.submit("proj-a", self.workspace / "proj-a",
                                    "run-tests", runner=blocker)
        other = self.manager.submit("proj-b", self.workspace / "proj-b",
                                    "run-tests", runner=second)
        # proj-b's job sits behind the per-project lock of nothing —
        # force it to stay queued by holding both pool workers:
        # simpler: cancel it before the pool reaches it is racy, so
        # instead cancel a job on proj-a (serialised behind first).
        queued = self.manager.submit("proj-a", self.workspace / "proj-a",
                                     "run-tests", runner=second)
        cancelled = self.manager.cancel(queued["id"])
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertIn("before it started", cancelled["result"])
        release.set()
        wait_job(self.manager, first["id"])
        wait_job(self.manager, other["id"])
        self.assertEqual(ran, [True])  # only proj-b's copy ran

    def test_interrupted_on_load(self):
        store = jobs.JobStore(self.workspace)
        for status in ("running", "queued", "done"):
            store.save({
                "id": f"job-old-{status}", "workspace": str(self.workspace),
                "project": "proj-a", "project_path": str(self.workspace),
                "action": "run-tests", "label": "Run tests",
                "status": status, "created": "2026-01-01T00:00:00+00:00",
                "started": ("2026-01-01T00:00:01+00:00"
                            if status == "running" else None),
                "finished": None, "events": [],
                "progress": {"done": 0, "total": None},
                "result": None, "error": None, "params": {}})
        manager = jobs.JobManager(self.workspace)
        try:
            self.assertEqual(manager.get("job-old-running")["status"],
                             "interrupted")
            self.assertEqual(manager.get("job-old-queued")["status"],
                             "interrupted")
            error = manager.get("job-old-running")["error"]
            self.assertIn("interrupted", error)
            self.assertIn("not resumed", error)
            self.assertIn("while this job was running", error)
            self.assertEqual(manager.get("job-old-done")["status"], "done")
        finally:
            manager.shutdown()

    def test_same_project_jobs_serialise(self):
        marks = []
        lock = threading.Lock()

        def make(tag):
            def runner(ctx):
                with lock:
                    marks.append((tag, "start", time.monotonic()))
                time.sleep(0.4)
                with lock:
                    marks.append((tag, "end", time.monotonic()))
                return tag
            return runner

        one = self.manager.submit("proj-a", self.workspace / "proj-a",
                                  "run-tests", runner=make("one"))
        two = self.manager.submit("proj-a", self.workspace / "proj-a",
                                  "run-tests", runner=make("two"))
        wait_job(self.manager, one["id"])
        wait_job(self.manager, two["id"])
        starts = {tag: t for tag, phase, t in marks if phase == "start"}
        ends = {tag: t for tag, phase, t in marks if phase == "end"}
        self.assertGreaterEqual(starts["two"], ends["one"] - 0.05)

    def test_unknown_action_is_refused(self):
        with self.assertRaises(ValueError):
            self.manager.submit("proj-a", self.workspace / "proj-a",
                                "dance")


def _get(url):
    try:
        with urllib.request.urlopen(url, timeout=60) as response:
            return response.status, response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8")


def _post(url, fields=None):
    data = urllib.parse.urlencode(fields or {}).encode("utf-8")
    request = urllib.request.Request(url, data=data, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status, response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8")


class JobEndpointTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self._tmp.name)
        self.project = self.workspace / "bee-game"
        self.project.mkdir()
        (self.project / "SCOPE.md").write_text(filled_scope_text(),
                                               encoding="utf-8")
        state.init_state(self.project)
        self.server = app_module.create_server(self.workspace, port=0)
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self._tmp.cleanup()

    def test_action_post_returns_job_and_api_reports_it(self):
        import re
        status, body = _post(self.base + "/project/bee-game/finish-check")
        self.assertEqual(status, 200)
        match = re.search(r'data-job-id="([^"]+)"', body)
        self.assertIsNotNone(match, body[:400])
        job_id = match.group(1)
        job = None
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            _s, payload = _get(f"{self.base}/api/jobs/{job_id}")
            job = json.loads(payload)
            if job["status"] in TERMINAL:
                break
            time.sleep(0.1)
        self.assertEqual(job["status"], "done")
        self.assertIn("Finish check done", job["result"])
        self.assertEqual(job["worker"], "finish")
        # The list endpoint and the pages know the job too.
        _s, payload = _get(self.base + "/api/jobs?project=bee-game")
        self.assertTrue(any(j["id"] == job_id
                            for j in json.loads(payload)["jobs"]))
        _s, dashboard = _get(self.base + "/")
        self.assertIn("Jobs", dashboard)
        _s, page = _get(self.base + "/project/bee-game")
        self.assertIn("Job history", page)
        _s, job_page = _get(f"{self.base}/jobs/{job_id}")
        self.assertIn("Finish check", job_page)
        self.assertIn('data-job-status="done"', job_page)
        # Unknown job ids are a clean 404.
        status, _ = _get(self.base + "/api/jobs/job-nope")
        self.assertEqual(status, 404)


@unittest.skipUnless(GODOT_PRESENT, "Godot not available")
class RealTestRunJobTests(unittest.TestCase):
    """One real job end to end: a built game, the run-tests
    action through the manager, done with a report on disk."""

    def test_run_tests_job_completes(self):
        from helpers import filled_scope_text as filled
        from weaverbird import generate, skeleton, testing  # noqa
        from weaverbird import assets_gen, scope
        counts = ("- REQUIRED Content counts (levels / waves / "
                  "rooms / items, as applicable): Filled in for the test")
        genre = "- REQUIRED Genre / archetype: Filled in for the test"
        win = "- REQUIRED Win condition: Filled in for the test"
        text = filled().replace(counts, counts.split(":")[0] + ": 2 puzzles")
        text = text.replace(genre, genre.split(":")[0] + ": puzzle")
        text = text.replace(win, win.split(":")[0] +
                            ": Push every crate onto its pad")
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            project = workspace / "crate-game"
            project.mkdir()
            (project / "SCOPE.md").write_text(text, encoding="utf-8")
            state.init_state(project)
            scope.approve_scope(project / "SCOPE.md", by="Tester")
            skeleton.build_skeleton(project)
            assets_gen.generate_assets(project)
            generate.build_game(project, use_brain=False)
            manager = jobs.JobManager(workspace)
            try:
                job = manager.submit("crate-game", project, "run-tests")
                done = wait_job(manager, job["id"], timeout=900)
            finally:
                manager.shutdown()
            self.assertEqual(done["status"], "done", done["error"])
            self.assertIn("Test suite finished", done["result"])
            report = json.loads((project / ".weaverbird"
                                 / "test_report.json").read_text())
            self.assertIn("units", report)


if __name__ == "__main__":
    unittest.main()
