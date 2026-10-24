"""The job runner — the platform plumbing that runs the factory's
long operations in the background, with live progress.

Before 0.6.0 the app ran every action synchronously: pressing "Run
tests" blocked a request thread for minutes and the user stared at
a frozen page. Now every project action in the app is a **job**:

- A job record — {id, workspace, project, action, worker, label,
  status, created/started/finished, events, result, error} — is
  persisted as JSON under ``<workspace>/.weaverbird-app/jobs/<id>.json``
  the moment it exists and at every step, so job history survives
  restarts and the CLI can read it (`weaverbird jobs`). ``worker``
  names the step that ran the action (skeleton, assets, build,
  testing, finish, repair, patch, feedback, balance,
  model-playtest, export) — the Pass 5 orchestrator keys its
  plan view off it.
- Execution happens on a small thread pool (max 2 concurrent).
  Jobs for the SAME project serialise behind a per-project lock:
  two Godot stages running on one project at once would corrupt
  each other's files, reports and git history.
- Progress is a *reporter*: a callable the long library entry
  points accept (`reporter(text, done=None, total=None)`). They
  call it at meaningful step boundaries — per validation run, per
  scripted player, per repair attempt, per patch gate — and the
  job reporter appends each call to the job's event log. With no
  reporter passed, the library functions behave exactly as they
  always have (the CLI prints its own summaries from the return
  values, unchanged).
- Cancellation is cooperative. Cancelling a job sets its flag;
  the reporter raises :class:`JobCancelled` at the next step
  boundary, so a cancel lands BETWEEN steps — a Godot run in
  flight finishes first (its own timeout still bounds it). The
  job record says so plainly. A cancelled or failed job never
  marks its stage complete: the library functions own their state
  writes and they simply never reach them.
- On startup, any job still marked "queued" or "running" on disk
  is rewritten as "interrupted": the server died under it, and the
  record says so honestly rather than pretending it is still going.

The action runners live here (not in the app) so the app, the CLI
and tests all start jobs the same way: a runner takes a
:class:`JobContext` and returns a short result summary string.
"""

from __future__ import annotations

import json
import re
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

APP_DIR_NAME = ".weaverbird-app"
JOBS_DIR_NAME = "jobs"
STATUSES = ("queued", "running", "done", "failed", "cancelled",
            "interrupted")
TERMINAL_STATUSES = ("done", "failed", "cancelled", "interrupted")
MAX_EVENTS = 400
CANCELLED_BETWEEN_STEPS = (
    "Cancelled between steps — the step in flight was allowed to "
    "finish, and no stage was marked complete by this job.")


class JobCancelled(Exception):
    """Raised by a job reporter when the job's cancel flag is set.

    Library entry points let it propagate: it unwinds the action
    at a step boundary, and the runner turns it into a cancelled
    job. It is deliberately not one of the library's own error
    types, so no existing error handling swallows it.
    """


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _parse_time(text):
    if not text:
        return None
    try:
        return datetime.fromisoformat(str(text))
    except ValueError:
        return None


def jobs_dir(workspace) -> Path:
    return Path(workspace) / APP_DIR_NAME / JOBS_DIR_NAME


# ---------------------------------------------------------------------------
# Persistence

# The factory's own job-id shape: one filename-safe token (the
# ids submit() mints look like job-20261024120000-abcdef). Anything
# carrying a path separator, a parent reference or a NUL is not a
# job id — before 0.13.0 an id like ../../canary was joined straight
# onto the store directory and the app served arbitrary *.json
# files outside it (security finding F4).
_JOB_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*(\.[A-Za-z0-9_-]+)*$")


def valid_job_id(job_id) -> bool:
    """True when *job_id* has the factory's own id shape — a
    single filename-safe token, never a path."""
    return isinstance(job_id, str) and bool(_JOB_ID_RE.match(job_id)) \
        and ".." not in job_id


class JobStore:
    """One JSON file per job under the workspace's app folder."""

    def __init__(self, workspace):
        self.workspace = Path(workspace)
        self.dir = jobs_dir(workspace)

    def path_for(self, job_id: str) -> Path:
        if not valid_job_id(job_id):
            raise ValueError(f"Not a job id: {job_id!r}")
        path = (self.dir / f"{job_id}.json").resolve()
        if path.parent != self.dir.resolve():
            raise ValueError(f"Not a job id: {job_id!r}")
        return path

    def save(self, job: dict) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        path = self.path_for(job["id"])
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(job, indent=2) + "\n", encoding="utf-8")
        tmp.replace(path)

    def load(self, job_id: str) -> dict | None:
        try:
            path = self.path_for(job_id)
        except ValueError:
            return None
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None
        return data if isinstance(data, dict) else None

    def list(self, project: str | None = None,
             limit: int | None = None) -> list[dict]:
        """Jobs newest first, optionally only one project's."""
        if not self.dir.is_dir():
            return []
        jobs = []
        for path in self.dir.glob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            if isinstance(data, dict) and data.get("id"):
                jobs.append(data)
        jobs.sort(key=lambda j: (j.get("created") or "", j.get("id") or ""),
                  reverse=True)
        if project is not None:
            jobs = [j for j in jobs if j.get("project") == project]
        if limit is not None:
            jobs = jobs[:limit]
        return jobs

    def mark_interrupted(self) -> list[str]:
        """Rewrite jobs left queued/running by a dead server.

        Returns the ids rewritten. Called when the app (or a job
        manager) starts: a job that was mid-flight when the
        process died is not still running, and the record must
        not claim it is.
        """
        rewritten = []
        for job in self.list():
            if job.get("status") in ("queued", "running"):
                job["status"] = "interrupted"
                job["finished"] = _now()
                job["error"] = (
                    "The app stopped while this job was "
                    f"{'queued' if not job.get('started') else 'running'} "
                    "— it was marked interrupted on the next start, "
                    "not resumed.")
                job.setdefault("events", []).append({
                    "at": _now(),
                    "text": "Marked interrupted — the app stopped "
                            "under this job."})
                self.save(job)
                rewritten.append(job["id"])
        return rewritten


# ---------------------------------------------------------------------------
# Reporters

class JobReporter:
    """The reporter handed to library entry points for one job.

    Calling it records an event (and progress, when done/total are
    given). When the job's cancel flag is set it raises
    JobCancelled instead — that is the cooperative cancellation
    check, and it happens at every step boundary the library
    reports at.
    """

    def __init__(self, manager: "JobManager", job_id: str):
        self._manager = manager
        self._job_id = job_id

    def __call__(self, text: str, done: int | None = None,
                 total: int | None = None) -> None:
        if self._manager.cancel_requested(self._job_id):
            raise JobCancelled(CANCELLED_BETWEEN_STEPS)
        self._manager.record_event(self._job_id, str(text),
                                   done=done, total=total)


def print_reporter(text: str, done: int | None = None,
                   total: int | None = None) -> None:
    """A reporter that prints — for embedders who want the CLI's
    step-by-step feel from the library directly."""
    print(text)


def null_reporter(text: str, done: int | None = None,
                  total: int | None = None) -> None:
    """A reporter that swallows steps (keeps a run quiet)."""


# ---------------------------------------------------------------------------
# The manager

class JobContext:
    """What an action runner receives."""

    def __init__(self, manager: "JobManager", job: dict, params: dict):
        self.manager = manager
        self.job = job
        self.workspace = Path(job["workspace"])
        self.project_name = job["project"]
        self.project_dir = Path(job["project_path"])
        self.params = params or {}
        self.reporter = JobReporter(manager, job["id"])

    @property
    def settings_path(self):
        candidate = self.workspace / "global-settings.yaml"
        return candidate if candidate.is_file() else None


class JobManager:
    """Runs jobs on a thread pool and keeps their records current."""

    def __init__(self, workspace, max_workers: int = 2):
        self.workspace = Path(workspace)
        self.store = JobStore(workspace)
        # A fresh manager means a fresh process lifetime: anything
        # a previous one left mid-flight is interrupted, honestly.
        self.store.mark_interrupted()
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="weaverbird-job")
        self._write_lock = threading.Lock()
        self._project_locks: dict[str, threading.Lock] = {}
        self._project_locks_guard = threading.Lock()
        self._cancel_flags: dict[str, threading.Event] = {}

    # -- lifecycle -----------------------------------------------------

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=False)

    def submit(self, project_name: str, project_path, action: str,
               params: dict | None = None, runner=None) -> dict:
        """Create a job and schedule it. Returns the job record.

        ``runner`` overrides the registered action runner — the
        seam tests use with fast fake actions.
        """
        if runner is None:
            if action not in ACTIONS:
                raise ValueError(f"Unknown job action {action!r}.")
            label, runner = ACTIONS[action]
        else:
            label = ACTIONS.get(action, (action, None))[0]
        job_id = "job-" + datetime.now(timezone.utc).strftime(
            "%Y%m%d%H%M%S") + "-" + uuid.uuid4().hex[:6]
        job = {
            "id": job_id,
            "workspace": str(self.workspace),
            "project": project_name,
            "project_path": str(project_path),
            "action": action,
            "worker": (params or {}).get("worker")
                      if action == "run-worker"
                      else ACTION_WORKERS.get(action),
            "label": label,
            "status": "queued",
            "created": _now(),
            "started": None,
            "finished": None,
            "events": [{"at": _now(), "text": f"Queued — {label}."}],
            "progress": None,
            "result": None,
            "error": None,
        }
        self._cancel_flags[job_id] = threading.Event()
        with self._write_lock:
            self.store.save(job)
        ctx = JobContext(self, job, params or {})
        self._executor.submit(self._execute, job_id, ctx, runner)
        return job

    def _project_lock(self, key: str) -> threading.Lock:
        with self._project_locks_guard:
            return self._project_locks.setdefault(key, threading.Lock())

    def _execute(self, job_id: str, ctx: JobContext, runner) -> None:
        # Same-project jobs serialise: the lock is held for the
        # whole run, so a second job for this project waits queued
        # (its record still says "queued", honestly) until the
        # first finishes.
        lock = self._project_lock(ctx.project_name)
        with lock:
            job = self.store.load(job_id)
            if job is None:
                return
            if self.cancel_requested(job_id):
                job["status"] = "cancelled"
                job["finished"] = _now()
                job["result"] = ("Cancelled before it started — "
                                  "nothing was run.")
                self._save(job)
                return
            job["status"] = "running"
            job["started"] = _now()
            job["events"].append({"at": _now(), "text": "Started."})
            self._save(job)
            try:
                result = runner(ctx)
            except JobCancelled:
                job = self.store.load(job_id) or job
                job["status"] = "cancelled"
                job["finished"] = _now()
                job["result"] = CANCELLED_BETWEEN_STEPS
                job["events"].append({
                    "at": _now(), "text": "Cancelled between steps."})
                self._save(job)
                return
            except Exception as exc:  # a job must always land
                job = self.store.load(job_id) or job
                job["status"] = "failed"
                job["finished"] = _now()
                job["error"] = f"{type(exc).__name__}: {exc}"
                job["events"].append({
                    "at": _now(),
                    "text": f"Failed: {type(exc).__name__}: {exc}"})
                self._save(job)
                return
            job = self.store.load(job_id) or job
            job["status"] = "done"
            job["finished"] = _now()
            job["result"] = str(result) if result is not None else "Done."
            job["events"].append({"at": _now(), "text": "Done."})
            self._save(job)

    # -- records ---------------------------------------------------------

    def _save(self, job: dict) -> None:
        with self._write_lock:
            self.store.save(job)

    def get(self, job_id: str) -> dict | None:
        return self.store.load(job_id)

    def list(self, project: str | None = None,
             limit: int | None = 20) -> list[dict]:
        return self.store.list(project=project, limit=limit)

    def running(self) -> list[dict]:
        return [j for j in self.store.list()
                if j.get("status") in ("queued", "running")]

    def record_event(self, job_id: str, text: str,
                     done: int | None = None,
                     total: int | None = None) -> None:
        with self._write_lock:
            job = self.store.load(job_id)
            if job is None:
                return
            events = job.setdefault("events", [])
            events.append({"at": _now(), "text": text})
            if len(events) > MAX_EVENTS:
                # Keep the first steps and the most recent ones.
                job["events"] = events[:20] + events[-(MAX_EVENTS - 20):]
            if done is not None or total is not None:
                progress = job.get("progress") or {}
                if done is not None:
                    progress["done"] = done
                if total is not None:
                    progress["total"] = total
                job["progress"] = progress
            self.store.save(job)

    # -- cancellation ----------------------------------------------------

    def cancel_requested(self, job_id: str) -> bool:
        flag = self._cancel_flags.get(job_id)
        return bool(flag and flag.is_set())

    def cancel(self, job_id: str) -> dict | None:
        """Ask a job to stop. Queued jobs cancel at once; running
        jobs stop at their next step boundary."""
        job = self.store.load(job_id)
        if job is None:
            return None
        flag = self._cancel_flags.setdefault(job_id, threading.Event())
        flag.set()
        if job.get("status") == "queued":
            # It may still be picked up by a worker between this
            # check and the run; _execute re-checks the flag under
            # the project lock before starting, so marking it now
            # is safe either way — a worker that sees the flag
            # writes the same cancelled record.
            job["status"] = "cancelled"
            job["finished"] = _now()
            job["result"] = ("Cancelled before it started — "
                              "nothing was run.")
            job["events"].append({"at": _now(),
                                  "text": "Cancelled while queued."})
            self._save(job)
        else:
            with self._write_lock:
                current = self.store.load(job_id)
                if current is not None \
                        and current.get("status") == "running":
                    current["events"].append({
                        "at": _now(),
                        "text": "Cancel requested — the job will stop "
                                "at its next step boundary."})
                    self.store.save(current)
                    job = current
        return job


def elapsed_seconds(job: dict, now: datetime | None = None) -> float:
    """How long a job has taken (or is taking), from its stamps."""
    started = _parse_time(job.get("started") or job.get("created"))
    if started is None:
        return 0.0
    finished = _parse_time(job.get("finished"))
    end = finished or now or datetime.now(timezone.utc)
    return max(0.0, (end - started).total_seconds())


def public_job(job: dict) -> dict:
    """The job record as the API serves it (adds elapsed seconds)."""
    out = dict(job)
    out["elapsed_s"] = round(elapsed_seconds(job), 1)
    return out


# ---------------------------------------------------------------------------
# Action runners — one per app action. Each returns the short
# summary the old synchronous pages showed as their notice.

def _run_build_skeleton(ctx: JobContext) -> str:
    from . import skeleton as skeleton_module
    report = skeleton_module.build_skeleton(
        ctx.project_dir, reporter=ctx.reporter)
    validation = report.get("validation", {})
    return (f"Skeleton built and validated in Godot "
            f"({validation.get('godot_version', '')}). "
            f"Known-good tag: {report.get('known_good_tag')}.")


def _run_generate_assets(ctx: JobContext) -> str:
    from . import assets_gen as assets_module
    report = assets_module.generate_assets(
        ctx.project_dir, reporter=ctx.reporter)
    return (f"Assets generated: {len(report['files'])} files, every "
            "one recorded in the provenance log. Cost: 0 tokens.")


def _run_build_game(ctx: JobContext) -> str:
    from . import generate as generate_module
    report = generate_module.build_game(
        ctx.project_dir, settings_path=ctx.settings_path,
        reporter=ctx.reporter)
    if report.get("plan"):
        kind = (f"plan {report['plan'].get('movement', '')} / "
                f"{report['plan'].get('objective', '')}")
    else:
        kind = f"archetype {report.get('archetype', '')}"
    return (f"Game built: {kind}, "
            f"{report['units_delivered']} content units, Godot "
            "validation passed, tagged known-good/build.")


def _run_run_tests(ctx: JobContext) -> str:
    from . import testing as testing_module
    report = testing_module.run_tests(
        ctx.project_dir, settings_path=ctx.settings_path,
        reporter=ctx.reporter)
    digest = " ".join(testing_module.digest_lines(report)[-3:])
    return f"Test suite finished. {digest}"


def _run_finish_check(ctx: JobContext) -> str:
    from . import finish as finish_module
    ctx.reporter("Checking the evidence for each item", done=0, total=1)
    result = finish_module.evaluate_finish(ctx.project_dir)
    bits = []
    if result["marked_now"]:
        bits.append("Marked now: " + ", ".join(
            finish_module.DOD_LABELS.get(k, k) for k in result["marked_now"]))
    if result["already_marked"]:
        bits.append("Already marked: " + ", ".join(
            finish_module.DOD_LABELS.get(k, k)
            for k in result["already_marked"]))
    if result["open"]:
        bits.append("Still open: " + "; ".join(
            f"{finish_module.DOD_LABELS.get(k, k)} — {reason}"
            for k, reason in result["open"].items()))
    ctx.reporter("Finish check complete", done=1, total=1)
    return "Finish check done. " + (" ".join(bits) if bits else "")


def _run_finish_report(ctx: JobContext) -> str:
    from . import finish as finish_module
    ctx.reporter("Writing the finish report", done=0, total=1)
    path = finish_module.build_finish_report(
        ctx.project_dir, settings_path=ctx.settings_path)
    ctx.reporter("Finish report written", done=1, total=1)
    return f"Finish report written to {path.name} in the project folder."


def _run_repair(ctx: JobContext) -> str:
    from . import repair as repair_module
    result = repair_module.run_repair(
        ctx.project_dir, settings_path=ctx.settings_path,
        reporter=ctx.reporter)
    return (f"Self-repair: {result['outcome']} — {result['note']} "
            "Full record in REPAIR_REPORT.md.")


def _run_patch_start(ctx: JobContext) -> str:
    from . import patch as patch_module
    item_id = str(ctx.params.get("item", ""))
    result = patch_module.start_patch(
        ctx.project_dir, item_id, settings_path=ctx.settings_path,
        reporter=ctx.reporter)
    if result["outcome"] == "patched":
        return (f"Patch shipped: version {result['version']} — "
                f"{result['change']}. Tests and save compatibility "
                "both passed.")
    if result["outcome"] == "planned":
        return (f"No automatic patch — a plan was written to "
                f"PATCH_PLAN.md. {result['reason']}")
    raise RuntimeError(
        f"Patch rejected and the tree restored: {result['reason']}")


def _run_feedback_fetch(ctx: JobContext) -> str:
    from . import feedback as feedback_module
    ctx.reporter("Fetching feedback from every source", done=0, total=1)
    report = feedback_module.fetch_feedback(ctx.project_dir)
    bits = "; ".join(
        f"{source}: {entry['detail']} (+{entry['added']})"
        for source, entry in report.items())
    ctx.reporter("Fetch complete", done=1, total=1)
    return "Fetch done. " + bits


def _run_feedback_triage(ctx: JobContext) -> str:
    from . import feedback as feedback_module
    ctx.reporter("Triaging the feedback queue", done=0, total=1)
    result = feedback_module.triage(
        ctx.project_dir, settings_path=ctx.settings_path)
    ctx.reporter("Triage complete", done=1, total=1)
    return (f"Triage done: {result['note']}. "
            f"{len(result['items'])} item(s) in the queue.")


def _run_model_playtest(ctx: JobContext) -> str:
    from . import modelplayer as modelplayer_module
    record = modelplayer_module.run_model_playtest(
        ctx.project_dir, settings_path=ctx.settings_path,
        reporter=ctx.reporter)
    return (f"Model playtest: {record['outcome']} — "
            f"{record['decisions']} decisions, {record['deaths']} "
            "deaths. Confusion notes were filed as feedback.")


def _run_balance_apply(ctx: JobContext) -> str:
    from . import balance as balance_module
    index = int(ctx.params.get("suggestion", 0))
    result = balance_module.apply_suggestion(
        ctx.project_dir, index, settings_path=ctx.settings_path,
        reporter=ctx.reporter)
    if result["outcome"] == "patched":
        return (f"Balance suggestion {index} shipped as patch "
                f"version {result['version']} — {result['change']}. "
                "Tests and save compatibility both passed.")
    if result["outcome"] == "planned":
        return (f"Balance suggestion {index} produced a plan, not a "
                f"patch: {result['reason']}")
    raise RuntimeError(
        f"Balance patch rejected and the tree restored: "
        f"{result['reason']}")


def _run_balance_compare(ctx: JobContext) -> str:
    from . import balance as balance_module
    report = balance_module.compare(
        ctx.project_dir,
        suggestion_index=ctx.params.get("suggestion"),
        config_overrides=ctx.params.get("config"),
        settings_path=ctx.settings_path, reporter=ctx.reporter)
    return f"Compare finished. {report['verdict']}"


def _run_orchestrate(ctx: JobContext) -> str:
    from . import orchestrator as orchestrator_module
    plan = orchestrator_module.run(
        ctx.project_dir,
        target=str(ctx.params.get("target", "finished")),
        reactive=ctx.params.get("reactive"),
        force=bool(ctx.params.get("force")),
        settings_path=ctx.settings_path, reporter=ctx.reporter,
        job_id=ctx.job["id"],
        include_model_playtest=bool(
            ctx.params.get("model_playtest")))
    if plan.get("outcome") == "done":
        ran = [s["worker"] for s in plan["steps"]
               if s["status"] == "done"]
        skipped = [s["worker"] for s in plan["steps"]
                   if s["status"] == "skipped"]
        bits = []
        if ran:
            bits.append("ran " + ", ".join(ran))
        if skipped:
            bits.append("skipped (inputs unchanged) "
                        + ", ".join(skipped))
        return "Plan complete" + (": " + "; ".join(bits) if bits else ".")
    escalation = plan.get("escalation") or {}
    raise RuntimeError(
        f"Plan stopped ({plan.get('outcome')}): owner "
        f"{escalation.get('worker', '?')} — "
        + "; ".join(i.get("detail", "") for i in
                    escalation.get("issues", [])))


def _run_exports(ctx: JobContext) -> str:
    from . import workers as workers_module
    targets = ctx.params.get("targets") or None
    outcome = workers_module.run_worker(
        "export", ctx.project_dir, reporter=ctx.reporter,
        settings_path=ctx.settings_path, job_id=ctx.job["id"],
        params={"targets": targets})
    if not outcome["ok"]:
        raise RuntimeError(outcome["summary"])
    return outcome["summary"]


def _run_worker(ctx: JobContext) -> str:
    from . import workers as workers_module
    name = str(ctx.params.get("worker", ""))
    outcome = workers_module.run_worker(
        name, ctx.project_dir, reporter=ctx.reporter,
        settings_path=ctx.settings_path, job_id=ctx.job["id"],
        params=ctx.params.get("worker_params")
        if isinstance(ctx.params.get("worker_params"), dict) else {})
    if not outcome["ok"]:
        raise RuntimeError(outcome["summary"])
    return f"Worker {name}: {outcome['summary']}"


def _run_interview_turn(ctx: JobContext) -> str:
    from . import interview as interview_module
    state = interview_module.respond(
        ctx.project_dir, str(ctx.params.get("message", "")),
        settings_path=ctx.settings_path, reporter=ctx.reporter)
    return "Interview turn. " + interview_module.summary_text(state)


def _run_assets_regenerate(ctx: JobContext) -> str:
    from . import assets_gen as assets_module
    name = str(ctx.params.get("asset", ""))
    result = assets_module.regenerate_asset(
        ctx.project_dir, name, reporter=ctx.reporter)
    return (f"Regenerated {result['path']} from the current palette "
            "and recorded it in the provenance log. Cost: 0 tokens.")


# Which named step/worker runs each action, recorded on the job
# as ``worker``. Today it is simply part of the record; the
# Pass 5 orchestrator keys its plan view off it, so every action
# names its worker here rather than the orchestrator having to
# guess from action strings later.
ACTION_WORKERS = {
    "build-skeleton": "skeleton",
    "generate-assets": "assets",
    "assets-regenerate": "assets",
    "build-game": "assembly",
    "orchestrate": "orchestrator",
    "run-worker": "worker",
    "run-tests": "testing",
    "run-exports": "export",
    "finish-check": "finish",
    "finish-report": "finish",
    "repair": "repair",
    "patch-start": "patch",
    "feedback-fetch": "feedback",
    "feedback-triage": "feedback",
    "model-playtest": "model-playtest",
    "balance-apply": "balance",
    "balance-compare": "balance",
    "interview-turn": "scope",
}

ACTIONS = {
    "build-skeleton": ("Build skeleton", _run_build_skeleton),
    "generate-assets": ("Generate assets", _run_generate_assets),
    "build-game": ("Build game", _run_build_game),
    "run-tests": ("Run tests", _run_run_tests),
    "run-exports": ("Run exports", _run_exports),
    "finish-check": ("Finish check", _run_finish_check),
    "finish-report": ("Write finish report", _run_finish_report),
    "repair": ("Self-repair", _run_repair),
    "patch-start": ("Ship patch", _run_patch_start),
    "feedback-fetch": ("Fetch feedback", _run_feedback_fetch),
    "feedback-triage": ("Triage feedback", _run_feedback_triage),
    "model-playtest": ("Model playtest", _run_model_playtest),
    "balance-apply": ("Balance lab: apply suggestion", _run_balance_apply),
    "balance-compare": ("Balance lab: compare", _run_balance_compare),
    "assets-regenerate": ("Regenerate asset", _run_assets_regenerate),
    "orchestrate": ("Run plan (orchestrator)", _run_orchestrate),
    "run-worker": ("Run one worker", _run_worker),
    "interview-turn": ("Interview turn", _run_interview_turn),
}
