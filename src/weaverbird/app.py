"""The Weaverbird app — a local web front end over the same core.

Bare ``weaverbird`` starts this app (workspace = the current folder;
``--workspace``, ``--port`` and ``--no-open`` adjust the launch). It
is bound to 127.0.0.1 only. It has no authentication because it never leaves the
machine it runs on: it is a local tool, like the CLI, drawn as
screens. Everything it shows comes from the library (scope, state,
ledger, provenance, brain, finish, skeleton) — the app holds no
logic of its own, so the CLI and the app can never drift apart.

Screens: a dashboard of the workspace's projects (with a Jobs
strip), a project view (pipeline stepper, definition of done
with its evidence, ledger, provenance, job history, and the
stage actions), the scope interview as a form (with prompt
extraction when a brain is configured), a settings view
(budgets editable, the rest — including which key environment
variables are set, never their values — read-only), a Feedback
view, the balance lab, and the asset studio.

Every long action is a background job (see jobs.py): the POST
answers at once with a live job card the page polls to
completion, so no screen ever freezes on a Godot run.

All HTML/CSS/JS is package data in ``weaverbird/web/``, styled from
the design tokens to match docs/visual-prototype/.
"""

from __future__ import annotations

import html
import json
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path

import yaml

from . import __version__
from . import assets_gen as assets_module
from . import balance as balance_module
from . import brain as brain_module
from . import feedback as feedback_module
from . import finish as finish_module
from . import generate as generate_module
from . import jobs as jobs_module
from . import testing as testing_module
from . import godot as godot_module
from . import ledger as ledger_module
from . import provenance as provenance_module
from . import scope as scope_module
from . import settings as settings_module
from . import orchestrator as orchestrator_module
from . import skeleton as skeleton_module
from . import state as state_module
from . import vcs as vcs_module
from . import workers as workers_module

STAGE_LABELS = {
    "scope": "Scope", "skeleton": "Skeleton", "assets": "Assets",
    "build": "Build", "headless_test": "Test", "playtest": "Playtest",
    "finished": "Finished", "approval": "Approval", "publish": "Publish",
    "live": "Live",
}


class AppError(Exception):
    """Raised for requests the app cannot serve (shown as a page)."""


def _esc(value) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)


def _web_text(name: str) -> str:
    return resources.files(__package__).joinpath("web", name).read_text(
        encoding="utf-8")


def _asset_bytes(name: str) -> bytes:
    return resources.files(__package__).joinpath("assets", name).read_bytes()


# ---------------------------------------------------------------------------
# Workspace discovery

def find_projects(workspace) -> list[dict]:
    """Projects in a workspace: directories containing a SCOPE.md.

    The workspace root itself counts when it is a project. Returns a
    list of {name, path} sorted by name; names are unique.
    """
    root = Path(workspace)
    found: list[dict] = []
    seen: set[str] = set()

    def add(path: Path) -> None:
        name = path.name
        if name in seen:
            name = f"{name} ({path.parent.name})"
        seen.add(name)
        found.append({"name": name, "path": path})

    if (root / "SCOPE.md").is_file():
        add(root)
    if root.is_dir():
        for child in sorted(root.iterdir()):
            if child.is_dir() and not child.name.startswith("."):
                if (child / "SCOPE.md").is_file():
                    add(child)
    return sorted(found, key=lambda p: p["name"].lower())


def workspace_settings(workspace):
    """(path, data) for the workspace's global-settings.yaml, if any."""
    candidate = Path(workspace) / "global-settings.yaml"
    if candidate.is_file():
        try:
            return candidate, settings_module.load_settings(candidate)
        except settings_module.SettingsError:
            return candidate, {}
    return None, None


def _project_facts(project: dict) -> dict:
    """Everything the screens show about one project, from the core."""
    path = project["path"]
    parsed = None
    try:
        parsed = scope_module.load_scope(scope_module.scope_path_for(path))
    except scope_module.ScopeError:
        parsed = None
    values = {f.name: f.value for f in parsed.fields} if parsed else {}
    state = None
    try:
        state = state_module.load_state(path)
    except state_module.PipelineError:
        state = None
    try:
        ledger_summary = ledger_module.summarise(path)
    except ledger_module.LedgerError:
        ledger_summary = {"entries": 0, "tokens": 0, "cost": 0.0,
                          "by_stage": {}, "by_tier": {}}
    try:
        ledger_entries = ledger_module.read_entries(path)
    except ledger_module.LedgerError:
        ledger_entries = []
    try:
        records = provenance_module.list_records(path)
    except provenance_module.ProvenanceError:
        records = []
    generation = generate_module.load_report(path)
    assets_report = assets_module.load_report(path)
    test_report = finish_module.load_test_report(path)
    return {
        "project": project,
        "scope": parsed,
        "values": values,
        "title": values.get("Title (working)") or project["name"],
        "pitch": values.get("One-line pitch") or "",
        "state": state,
        "stage": state["stage"] if state else "scope",
        "ledger": ledger_summary,
        "ledger_entries": ledger_entries,
        "provenance": records,
        "generation": generation,
        "assets_report": assets_report,
        "test_report": test_report,
    }


# ---------------------------------------------------------------------------
# HTML helpers

_BIRD_PATHS = (
    '<path fill="#fff" d="M4 15c5 1 9-1 11-6l4-4-1 5c3 0 4 2 4 2s-2 1-4 1c-1 4-5 8-10 8-2 0-4-1-4-1l2-3c-1 0-2-1-2-2z"/>'
)


def _layout(title: str, active: str, body: str, foot: str = "") -> str:
    nav = ""
    for href, key, label in (("/", "projects", "Projects"),
                             ("/settings", "settings", "Settings")):
        on = " on" if key == active else ""
        nav += (f'<a href="{href}" class="{on.strip()}">'
                f'<span class="dot"></span>{label}</a>')
    foot_html = f'<div class="foot">{foot}</div>' if foot else ""
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_esc(title)} — Weaverbird</title>
<link rel="stylesheet" href="/style.css">
<script src="/app.js" defer></script>
</head><body><div class="app">
<div class="side">
  <div class="brand"><div class="mark"><svg width="21" height="21" viewBox="0 0 24 24">{_BIRD_PATHS}</svg></div>
  <div><b>Weaverbird</b><small>Game factory</small></div></div>
  <div class="nav">{nav}</div>
  {foot_html}
</div>
<div class="main">{body}</div>
</div></body></html>"""


def _stepper(current_stage: str) -> str:
    stages = state_module.STAGES
    try:
        current = stages.index(current_stage)
    except ValueError:
        current = 0
    steps = ""
    for index, stage in enumerate(stages):
        cls = "done" if index < current else ("now" if index == current else "")
        inner = "✓" if index < current else str(index + 1)
        steps += (f'<div class="step {cls}"><div class="n">{inner}</div>'
                  f'<label>{STAGE_LABELS.get(stage, stage)}</label></div>')
    return f'<div class="stepper">{steps}</div>'


def _stage_pips(current_stage: str) -> str:
    stages = state_module.STAGES
    try:
        current = stages.index(current_stage)
    except ValueError:
        current = 0
    pips = ""
    for index in range(len(stages)):
        cls = "done" if index < current else ("now" if index == current else "")
        pips += f'<i class="{cls}"></i>'
    return f'<div class="stages">{pips}</div>'


# ---------------------------------------------------------------------------
# Screens

def render_dashboard(server) -> str:
    workspace = server.workspace
    projects = find_projects(workspace)
    _, settings_data = workspace_settings(workspace)
    statuses = brain_module.brain_status(settings_data)

    cards = ""
    totals = {"tokens": 0, "cost": 0.0}
    for project in projects:
        facts = _project_facts(project)
        totals["tokens"] += facts["ledger"]["tokens"]
        totals["cost"] += facts["ledger"]["cost"]
        name = urllib.parse.quote(project["name"])
        cards += f"""<div class="card">
<span class="chip">{_esc(STAGE_LABELS.get(facts['stage'], facts['stage']))}</span>
<h3 style="margin-top:12px"><a href="/project/{name}" style="color:inherit;text-decoration:none">{_esc(facts['title'])}</a></h3>
<p class="pitch">{_esc(facts['pitch'] or 'No pitch in the scope yet.')}</p>
{_stage_pips(facts['stage'])}
<div class="meta"><span>Stage: <b>{_esc(STAGE_LABELS.get(facts['stage'], facts['stage']))}</b></span><span><b>{facts['ledger']['tokens']}</b> tokens</span></div>
</div>"""
    if not cards:
        cards = ('<div class="empty">No projects in this workspace yet. '
                 'A project is a folder with a SCOPE.md — create one with '
                 '<code>weaverbird init &lt;dir&gt;</code> inside this '
                 'workspace, then reload.</div>')

    gh = vcs_module.github_status()
    if gh["available"] and gh["logged_in"]:
        gh_line = f"GitHub — <b>{_esc(gh['account'])}</b> connected"
    elif gh["available"]:
        gh_line = "GitHub CLI — installed, not logged in"
    else:
        gh_line = "GitHub CLI — not found (local git still works)"
    godot_path, godot_version = godot_module.find_godot()
    if godot_path:
        short = ".".join((godot_version or "").split(".")[:2])
        godot_line = (f"Godot <b>{_esc(short)}</b> ready" if short
                      else "Godot found")
    else:
        godot_line = "Godot — not found"
    brain_lines = ""
    for tier in brain_module.TIERS:
        st = statuses[tier]
        if st["usable"]:
            text = f"{tier} — {_esc(st['provider'])}/{_esc(st['model'])}, key set"
        elif st["configured"]:
            text = f"{tier} — configured, key missing ({_esc(st['api_key_env'] or 'no env name')})"
        else:
            text = f"{tier} — not configured"
        brain_lines += f'<div class="check" style="border:none;padding:5px 0"><div class="box{" open" if not st["usable"] else ""}">{"✓" if st["usable"] else "–"}</div><div style="font-size:13.5px">{_esc(text)}</div></div>'

    budget_html = ""
    if isinstance(settings_data, dict) and settings_data:
        budgets = settings_data.get("budgets", {})
        cap = budgets.get("tokens_per_game")
        if isinstance(cap, (int, float)) and cap:
            per_project = []
            for project in projects:
                per_project.append(_project_facts(project)["ledger"]["tokens"])
            highest = max(per_project) if per_project else 0
            pct = min(100.0, highest / cap * 100.0)
            cls = "over" if highest >= cap else ("warn" if pct >= 80 else "")
            budget_html = f"""<div style="margin-top:14px"><span style="font-size:13px;color:#68707F">Highest project against the per-game cap</span>
<div class="bar"><i class="{cls}" style="width:{pct:.0f}%"></i></div>
<div class="meta"><span><b>{highest}</b> tokens</span><span>cap {int(cap)}</span></div></div>"""

    body = f"""
<div class="top"><div><h1>Projects</h1><div class="sub">Every game in this workspace, where it is, and what it has cost.</div></div></div>
<div class="grid">{cards}</div>
{_jobs_strip(server)}
<div class="row">
  <div class="card"><h3>Factory</h3>
    <div class="check"><div class="box">{'✓' if gh['logged_in'] else '–'}</div><div>{gh_line}</div></div>
    <div class="check"><div class="box">{'✓' if godot_path else '–'}</div><div>{godot_line}</div></div>
    {budget_html}
  </div>
  <div class="card"><h3>Brain (bring your own key)</h3>{brain_lines}
    <p class="hint" style="margin-top:10px">Keys are read from environment variables at call time and are never stored. See <a href="/settings">Settings</a>.</p>
  </div>
</div>"""
    foot = (f"<b>Workspace</b><br>{_esc(str(workspace))}<br><br>"
            f"<b>Total spend</b><br>{totals['tokens']} tokens · cost {totals['cost']:.4f}")
    return _layout("Projects", "projects", body, foot)


def _plan_card(server, project: dict, quoted: str) -> str:
    """The Plan card: the orchestrator's DAG as a vertical worker
    list — per-worker status from the run records (done / skipped
    when inputs are unchanged / failed / pending), the last-run
    summary, a Run plan button and a Run button per worker."""
    path = project["path"]
    try:
        data = orchestrator_module.plan(path, persist=False)
    except Exception:
        return ""
    stored = orchestrator_module.load_plan(path) or {}
    stored_steps = {s.get("worker"): s for s in stored.get("steps", [])}
    rows = ""
    for step in data["steps"]:
        worker_name = step["worker"]
        record = workers_module.load_record(path, worker_name)
        stored_step = stored_steps.get(worker_name, {})
        if stored_step.get("status") == "skipped":
            status, cls, box = "skipped — inputs unchanged", " open", "–"
            summary = "Skipped on the last plan run: its inputs " \
                      "had not changed."
        elif step["status"] == "done":
            status, cls, box = "done", "", "✓"
            summary = (record or {}).get("summary", "")
        elif step["status"] == "stale":
            status, cls, box = "inputs changed — will re-run", " open", "–"
            summary = (record or {}).get("summary", "")
        elif step["status"] == "failed":
            status, cls, box = "failed", " open", "✗"
            summary = (record or {}).get("summary", "")
        else:
            status, cls, box = "pending", " open", "–"
            summary = step.get("reason", "")
        rows += (
            f'<div class="check"><div class="box{cls}">{box}</div>'
            f'<div style="flex:1">{_esc(step["title"])} '
            f'<span class="hint">({status})</span>'
            f'<small>{_esc(summary)}</small></div>'
            f'<form class="action" method="post" '
            f'action="/project/{quoted}/worker/{_esc(worker_name)}">'
            f'<button class="btn ghost" type="submit">Run</button>'
            f'</form></div>')
    outcome = stored.get("outcome")
    outcome_html = ""
    if outcome:
        outcome_html = (f'<p class="hint" style="margin-top:8px">'
                        f'Last plan run: {_esc(outcome)}.</p>')
    return f"""
<div class="card" style="margin-top:20px"><h3>Plan — the workers</h3>
<p class="hint" style="margin:6px 0 10px">The orchestrator runs each
independent worker in order and skips any whose inputs have not
changed. Every step can also be run on its own.</p>
{rows}
<form class="action" method="post" action="/project/{quoted}/run-plan" style="margin-top:12px">
<button class="btn" type="submit">Run plan</button></form>
{outcome_html}</div>"""


def render_project(server, name: str, notice: str = "",
                   notice_kind: str = "ok",
                   job_id: str | None = None) -> str:
    project = server.project_by_name(name)
    facts = _project_facts(project)
    state = facts["state"]
    path = project["path"]
    quoted = urllib.parse.quote(name)

    notice_html = (f'<div class="notice {notice_kind}">{notice}</div>'
                   if notice else "")
    job_html = _job_card_for(server, job_id)

    dod_html = ""
    if state:
        dod = state.get("definition_of_done", {})
        evidence = state.get("dod_evidence", {})
        for key in state_module.DEFINITION_OF_DONE:
            label = finish_module.DOD_LABELS.get(key, key)
            if dod.get(key):
                note = evidence.get(key, {}).get("note", "Marked.")
                by = evidence.get(key, {}).get("by", "")
                by_text = f" — marked by {_esc(by)}" if by else ""
                dod_html += (f'<div class="check"><div class="box">✓</div>'
                             f'<div>{_esc(label)}<small>{_esc(note)}{by_text}</small></div></div>')
            else:
                dod_html += (f'<div class="check"><div class="box open">–</div>'
                             f'<div>{_esc(label)}<small>Still open — run the finish check for the reason.</small></div></div>')
    else:
        dod_html = '<p class="hint">No pipeline state yet for this project.</p>'

    ledger_rows = ""
    for entry in facts["ledger_entries"]:
        ledger_rows += (f"<tr><td>{_esc(entry.get('ts', ''))}</td>"
                        f"<td>{_esc(entry.get('stage', ''))}</td>"
                        f"<td>{_esc(entry.get('tier', ''))}</td>"
                        f"<td class='r'>{_esc(entry.get('tokens', 0))}</td>"
                        f"<td class='r'>{float(entry.get('cost', 0)):.4f}</td></tr>")
    if not ledger_rows:
        ledger_rows = ('<tr><td colspan="5" class="hint">Nothing spent yet — '
                       'the ledger is empty.</td></tr>')

    prov_rows = ""
    for record in facts["provenance"]:
        prov_rows += (f"<tr><td>{_esc(record.get('asset', ''))}</td>"
                      f"<td>{_esc(record.get('source', ''))}</td>"
                      f"<td>{_esc(record.get('provider', '') or '—')}</td>"
                      f"<td>{_esc(record.get('note', ''))}</td></tr>")
    if not prov_rows:
        prov_rows = ('<tr><td colspan="4" class="hint">No provenance records '
                     'yet. An asset with no record is not used.</td></tr>')

    parsed = facts["scope"]
    if parsed is None:
        skeleton_reason = "No scope file found."
    elif not parsed.is_complete:
        skeleton_reason = (f"Scope incomplete — {len(parsed.missing_fields)} "
                           "required fields still missing.")
    elif not parsed.is_approved:
        skeleton_reason = "Scope complete but not approved yet."
    else:
        skeleton_reason = ""
    disabled = " disabled" if skeleton_reason else ""
    skeleton_button = (
        f'<form class="action" method="post" action="/project/{quoted}/build-skeleton">'
        f'<button class="btn" type="submit"{disabled}>Build skeleton</button></form>')
    if skeleton_reason:
        skeleton_button += f'<span class="hint">{_esc(skeleton_reason)}</span>'

    game_dir_exists = (path / skeleton_module.GAME_DIR_NAME
                       / "project.godot").exists()
    if not game_dir_exists:
        assets_reason = "Build the skeleton first — assets live inside its game."
        build_reason = "Build the skeleton first — the real game is generated on top of it."
    elif skeleton_reason:
        assets_reason = ""
        build_reason = skeleton_reason
    else:
        assets_reason = ""
        build_reason = ""
    stage_buttons = ""
    for action, label, reason in (
        ("generate-assets", "Generate assets", assets_reason),
        ("build-game", "Build game", build_reason),
    ):
        dis = " disabled" if reason else ""
        stage_buttons += (
            f'<form class="action" method="post" action="/project/{quoted}/{action}">'
            f'<button class="btn ghost" type="submit"{dis}>{label}</button></form>')
        if reason:
            stage_buttons += f'<span class="hint">{_esc(reason)}</span>'
    generation = facts["generation"]
    if generation:
        test_reason = ""
    else:
        test_reason = "Build the game first — the test suite runs the generated game and its scripted players."
    dis = " disabled" if test_reason else ""
    stage_buttons += (
        f'<form class="action" method="post" action="/project/{quoted}/run-tests">'
        f'<button class="btn ghost" type="submit"{dis}>Run tests</button></form>')
    if test_reason:
        stage_buttons += f'<span class="hint">{_esc(test_reason)}</span>'

    generation_card = ""
    if generation:
        assets_report = facts["assets_report"]
        assets_line = ""
        if assets_report:
            assets_line = (
                f"<p class='hint' style='margin-top:8px'>Assets: "
                f"{len(assets_report.get('files', []))} files generated "
                f"(palette {_esc(assets_report.get('palette', {}).get('accent_hex', ''))} / "
                f"{_esc(assets_report.get('palette', {}).get('target_hex', ''))}), "
                f"icon at game/icon.svg.</p>")
        generation_card = f"""
<div class="card" style="margin-top:20px"><h3>The game</h3>
<p style="margin-top:8px">Archetype: <b>{_esc(generation.get('archetype', ''))}</b>
<span class="hint">— {_esc(generation.get('archetype_reason', ''))}</span></p>
<p class="hint" style="margin-top:6px">Content delivered: <b>{_esc(generation.get('units_delivered', '?'))}</b> units (scope asked for {_esc(generation.get('units_requested', '?'))}) ·
Tuning: {'proposed by the mid-tier model, validated and clamped' if generation.get('brain_used') else 'deterministic — ' + _esc(generation.get('brain_note', 'no model used'))} ·
Validation: {'passed in Godot' if generation.get('validation', {}).get('passed') else 'not passed'}</p>{assets_line}</div>"""

    test_card = ""
    test_report = facts["test_report"]
    if test_report:
        players = test_report.get("playtesters", {})
        perf = test_report.get("performance", {})

        def player_line(label, data):
            if not isinstance(data, dict) or not data:
                return ""
            bits = []
            if data.get("time_s") is not None:
                bits.append(f"{data['time_s']}s")
            if data.get("deaths"):
                bits.append(f"{data['deaths']} deaths")
            if data.get("first_objective_s") is not None \
                    and isinstance(data.get("first_objective_s"), (int, float)) \
                    and data["first_objective_s"] >= 0:
                bits.append(f"first objective at {data['first_objective_s']}s")
            if data.get("finished") is not None:
                bits.append("finished" if data["finished"] else "did not finish")
            findings = data.get("findings") or []
            for finding in findings[:2]:
                bits.append(str(finding))
            for note in (data.get("confusion") or [])[:2]:
                bits.append("confused: " + str(note))
            if data.get("decisions"):
                bits.append(f"{data['decisions']} decisions")
            if data.get("result"):
                bits.append("result: " + str(data["result"]))
            ok = data.get("ok")
            box = "✓" if ok else "–"
            cls = "" if ok else " open"
            return (f'<div class="check"><div class="box{cls}">{box}</div>'
                    f'<div>{label}<small>{_esc(", ".join(bits))}</small></div></div>')

        competent = players.get("competent", {}) if isinstance(players, dict) else {}
        comp_line = ""
        if isinstance(competent, dict) and competent:
            win = competent.get("win", {})
            lose = competent.get("lose", {})
            bits = []
            if win.get("time_s") is not None:
                bits.append(f"won in {win['time_s']}s")
            if competent.get("ok"):
                bits.append("win and lose proven")
            comp_line = (f'<div class="check"><div class="box">✓</div>'
                         f'<div>Competent player<small>{_esc(", ".join(bits))}</small></div></div>')
        model_data = players.get("model", {}) if isinstance(players, dict) else {}
        model_line = ""
        if isinstance(model_data, dict) and model_data:
            model_line = player_line("Model playtester (vision)", model_data)
        repair_button = ""
        if not test_report.get("ok"):
            repair_button = (
                f'<form class="action" method="post" '
                f'action="/project/{quoted}/repair" style="margin-top:12px">'
                '<button class="btn" type="submit">Repair automatically</button></form>'
                '<span class="hint">Bounded self-repair: classify the '
                'failures, try config fixes behind git checkpoints, keep '
                'only what improves the suite.</span>')
        model_button = (
            f'<form class="action" method="post" '
            f'action="/project/{quoted}/model-playtest" style="margin-top:12px">'
            '<button class="btn ghost" type="submit">Model playtest</button></form>'
            '<span class="hint">A vision model (your key) plays from '
            'screenshots. Its confusion is filed as feedback.</span>')
        test_card = f"""
<div class="card" style="margin-top:20px"><h3>Test summary</h3>
<p class="hint" style="margin:6px 0 10px">Verdict: <b>{'PASS' if test_report.get('ok') else 'FAIL'}</b> ·
Win proven: {'yes' if test_report.get('win_proven') else 'no'} · Lose proven: {'yes' if test_report.get('lose_proven') else 'no'} ·
Performance: {_esc(perf.get('avg_fps', '?'))} avg FPS (target {_esc(perf.get('target_fps', '?'))}), scene load {_esc(perf.get('load_time_s', '?'))}s ·
Scripted players are deterministic bots; the model playtester plays from screenshots</p>
{comp_line}{player_line("Breaker", players.get("breaker", {}))}{player_line("First-time player", players.get("first_time", {}))}{model_line}
{repair_button}{model_button}</div>"""

    plan_card = _plan_card(server, project, quoted)

    reasons = state_module.gate_reasons(path, state) if state else []
    gate_html = ""
    if reasons:
        gate_html = ('<p class="hint" style="margin-top:10px">Advance is '
                     'blocked: ' + _esc(" ".join(reasons)) + "</p>")

    body = f"""
<div class="top"><div><h1>{_esc(facts['title'])}</h1><div class="sub">{_esc(facts['pitch'] or 'No pitch in the scope yet.')}</div></div>
<div><a class="btn ghost" href="/project/{quoted}/scope">Scope interview</a>
<a class="btn ghost" href="/project/{quoted}/feedback">Feedback</a>
<a class="btn ghost" href="/project/{quoted}/balance">Balance lab</a>
<a class="btn ghost" href="/project/{quoted}/assets">Asset studio</a></div></div>
{notice_html}
{job_html}
{_stepper(facts['stage'])}
<div class="actions">
  {skeleton_button}
  {stage_buttons}
  <form class="action" method="post" action="/project/{quoted}/finish-check"><button class="btn ghost" type="submit">Run finish check</button></form>
  <form class="action" method="post" action="/project/{quoted}/advance"><button class="btn ghost" type="submit">Advance pipeline</button></form>
  <form method="post" action="/project/{quoted}/finish-report"><button class="btn ghost" type="submit">Write finish report</button></form>
</div>
{gate_html}
{plan_card}
{generation_card}
{test_card}
{_job_history_card(server, name)}
<div class="row">
  <div class="card"><h3>Definition of done</h3>{dod_html}</div>
  <div class="card"><h3>Cost so far</h3>
    <p style="font-size:22px;font-weight:700;margin:8px 0 2px">{facts['ledger']['tokens']} tokens</p>
    <p class="hint">{facts['ledger']['entries']} entries · cost {facts['ledger']['cost']:.4f}</p>
    <p class="hint" style="margin-top:10px">The skeleton stage is deterministic: its entries are 0 tokens.</p>
  </div>
</div>
<div class="card" style="margin-top:20px"><h3>Ledger</h3>
<table><tr><th>When</th><th>Stage</th><th>Tier</th><th class="r">Tokens</th><th class="r">Cost</th></tr>{ledger_rows}</table></div>
<div class="card" style="margin-top:20px"><h3>Provenance</h3>
<table><tr><th>Asset</th><th>Source</th><th>Provider</th><th>Note</th></tr>{prov_rows}</table></div>"""
    return _layout(facts["title"], "projects", body)


def render_feedback(server, name: str, notice: str = "",
                    notice_kind: str = "ok",
                    job_id: str | None = None) -> str:
    project = server.project_by_name(name)
    path = project["path"]
    quoted = urllib.parse.quote(name)
    store = feedback_module.load_store(path)
    items = feedback_module.ranked_queue(store)
    notice_html = (f'<div class="notice {notice_kind}">{notice}</div>'
                   if notice else "")
    job_html = _job_card_for(server, job_id)
    rows = ""
    for item in items:
        item_id = _esc(item.get("id", ""))
        actions = ""
        status = item.get("status", "new")
        if status in ("new", "triaged"):
            actions = (
                f'<form class="action" method="post" action="/project/{quoted}/feedback/{item_id}/approve">'
                '<button class="btn ghost" type="submit">Approve</button></form>'
                f'<form class="action" method="post" action="/project/{quoted}/feedback/{item_id}/dismiss">'
                '<button class="btn ghost" type="submit">Dismiss</button></form>')
        elif status == "approved":
            actions = (
                f'<form class="action" method="post" action="/project/{quoted}/patch/{item_id}">'
                '<button class="btn" type="submit">Ship as patch</button></form>'
                f'<form class="action" method="post" action="/project/{quoted}/feedback/{item_id}/dismiss">'
                '<button class="btn ghost" type="submit">Dismiss</button></form>')
        elif status == "patched":
            actions = f'<span class="chip green">patched in {_esc(item.get("patch_version", ""))}</span>'
        version = (f' · v{_esc(item["patch_version"])}'
                   if item.get("patch_version") else "")
        rows += f"""
<div class="check"><div class="box{' open' if status in ('new', 'triaged') else ''}">{'–' if status in ('new', 'triaged') else '✓'}</div>
<div style="flex:1"><b>[{item_id}]</b> {_esc(item.get('text', ''))}
<small>{_esc(item.get('category', 'unknown'))} · severity {_esc(item.get('severity', '?'))} · score {_esc(item.get('score', 0))} · cluster {_esc(item.get('cluster', '') or '—')} · {_esc(status)}{version} · from {_esc(item.get('source', ''))}{(' — ' + _esc(item['note'])) if item.get('note') else ''}</small></div>
<div>{actions}</div></div>"""
    if not rows:
        rows = ('<p class="hint">No feedback yet. Fetch from GitHub '
                'issues, itch comments and the feedback-inbox, add an '
                'item below, or let the model playtester file its '
                'confusion here.</p>')
    body = f"""
<div class="top"><div><h1>Feedback</h1><div class="sub">{_esc(name)} — what players said, triaged and ranked. Approving an item lets the patch loop try to ship it as a tested patch; requests and price are always the maker's call.</div></div>
<div><a class="btn ghost" href="/project/{quoted}">Back to project</a></div></div>
{notice_html}
{job_html}
<div class="actions">
  <form class="action" method="post" action="/project/{quoted}/feedback/fetch"><button class="btn" type="submit">Fetch feedback</button></form>
  <form class="action" method="post" action="/project/{quoted}/feedback/triage"><button class="btn ghost" type="submit">Triage</button></form>
</div>
<div class="card" style="margin-top:20px"><h3>The queue</h3>{rows}</div>
<div class="card" style="margin-top:20px"><h3>Add feedback by hand</h3>
<form method="post" action="/project/{quoted}/feedback/add">
<p style="margin:10px 0"><textarea name="text" rows="2" style="width:100%" placeholder="What did the player say?"></textarea></p>
<button class="btn ghost" type="submit">Add item</button></form></div>"""
    return _layout("Feedback", "projects", body)


def render_scope(server, name: str, notice: str = "",
                 notice_kind: str = "ok") -> str:
    project = server.project_by_name(name)
    path = project["path"]
    parsed = scope_module.load_scope(scope_module.scope_path_for(path))
    quoted = urllib.parse.quote(name)
    notice_html = (f'<div class="notice {notice_kind}">{notice}</div>'
                   if notice else "")

    groups: dict[str, list] = {}
    order: list[str] = []
    for fld in parsed.fields:
        if fld.section not in groups:
            groups[fld.section] = []
            order.append(fld.section)
        groups[fld.section].append(fld)

    form_groups = ""
    for section in order:
        fields_html = ""
        for fld in groups[section]:
            fields_html += (f'<label for="f_{_esc(fld.name)}">{_esc(fld.name)}'
                            f'{" — still missing" if not fld.filled else ""}</label>'
                            f'<input type="text" id="f_{_esc(fld.name)}" '
                            f'name="{_esc(fld.name)}" value="{_esc(fld.value)}">')
        form_groups += (f'<div class="q"><h4>{_esc(section)}</h4>'
                        f'<form class="stack" method="post" action="/project/{quoted}/scope">'
                        f'{fields_html}'
                        f'<div class="actions"><button class="btn" type="submit">Save scope</button></div>'
                        f'</form></div>')

    questions = scope_module.interview_questions(parsed)
    q_html = ""
    if questions:
        items = ""
        for section, qs in questions:
            items += f"<p style='font-weight:600;margin-top:10px'>{_esc(section)}</p>"
            for q in qs:
                items += f"<p class='hint' style='margin:3px 0'>– {_esc(q)}</p>"
        q_html = (f'<div class="card" style="margin-top:4px"><h3>Still to answer '
                  f'({len(parsed.missing_fields)} of {len(parsed.fields)})</h3>{items}</div>')
    else:
        q_html = ('<div class="notice ok">Every REQUIRED field is filled. '
                  'The scope can be approved below.</div>')

    _, data = workspace_settings(server.workspace)
    status = brain_module.tier_status(data, "cheap") if data else None
    if status and status["usable"]:
        brain_note = (f"Brain: the cheap tier ({_esc(status['provider'])}/"
                      f"{_esc(status['model'])}) will read the prompt and fill "
                      "in what it can. Fields it is unsure about stay empty.")
    else:
        reason = status["reason"] if status else "No global settings file in this workspace."
        brain_note = (f"No model is configured ({_esc(reason)}), so extraction "
                      "is off — the prompt is still stored, and the questions "
                      "below are the interview.")
    approve_disabled = "" if parsed.is_complete else " disabled"
    approve_hint = ("" if parsed.is_complete else
                    '<span class="hint">Approve unlocks when every REQUIRED field is filled.</span>')
    approval_line = (f'<p class="hint" style="margin-top:8px">{_esc(parsed.approval)}</p>'
                     if parsed.is_approved else "")

    body = f"""
<div class="top"><div><h1>Scope interview</h1><div class="sub">{_esc(name)} — the scope file fills in as answers land. The build stays locked until every REQUIRED field is filled and the scope is approved.</div></div>
<div><a class="btn ghost" href="/project/{quoted}">Back to project</a></div></div>
{notice_html}
<div class="prog card" style="display:flex;align-items:center;gap:14px;margin-bottom:20px">
  <div style="font-size:21px;font-weight:800;color:#6C4CF1">{len(parsed.filled_fields)} / {len(parsed.fields)}</div>
  <div style="flex:1"><b>{'Complete — ready to approve' if parsed.is_complete else f'{len(parsed.missing_fields)} answers to go'}</b>
  <div class="bar"><i style="width:{(100 * len(parsed.filled_fields) // max(1, len(parsed.fields)))}%"></i></div></div>
  <span class="chip{' green' if parsed.is_approved else ''}">{'Approved' if parsed.is_approved else 'Build locked'}</span>
</div>
<div class="q"><h4>Start from a prompt</h4>
  <form class="stack action" method="post" action="/project/{quoted}/scope/extract">
    <label for="prompt">The prompt</label>
    <textarea id="prompt" name="prompt" rows="3" placeholder="Describe the game in plain words…">{_esc(parsed.original_prompt)}</textarea>
    <p class="hint" style="margin-top:8px">{brain_note}</p>
    <div class="actions"><button class="btn" type="submit">Store prompt &amp; extract answers</button></div>
  </form>
</div>
{form_groups}
{q_html}
<div class="q"><h4>Approve</h4>
  <form class="stack" method="post" action="/project/{quoted}/scope/approve">
    <label for="by">Approved by (your name)</label>
    <input type="text" id="by" name="by" value="">
    <div class="actions"><button class="btn" type="submit"{approve_disabled}>Approve scope</button>{approve_hint}</div>
    {approval_line}
  </form>
</div>"""
    return _layout("Scope interview", "projects", body)


def render_settings(server, notice: str = "", notice_kind: str = "ok") -> str:
    path, data = workspace_settings(server.workspace)
    notice_html = (f'<div class="notice {notice_kind}">{notice}</div>'
                   if notice else "")
    if path is None:
        body = f"""
<div class="top"><div><h1>Settings</h1><div class="sub">One global settings file serves the whole factory.</div></div></div>
{notice_html}
<div class="empty">No <code>global-settings.yaml</code> in this workspace yet.
Create one from the template that ships with Weaverbird
(<code>weaverbird init &lt;dir&gt; --with-settings</code> copies it into a
project; for the app, place it in the workspace root), fill in the budgets
and the model ladder, and reload.</div>"""
        return _layout("Settings", "settings", body)
    data = data or {}
    budgets = data.get("budgets", {}) if isinstance(data.get("budgets"), dict) else {}

    def bfield(name, step="1"):
        value = budgets.get(name)
        return (f'<label for="b_{name}">{name}</label>'
                f'<input type="number" step="{step}" id="b_{name}" name="{name}" '
                f'value="{_esc("" if value is None else value)}">')

    statuses = brain_module.brain_status(data)
    ladder_rows = ""
    for tier in brain_module.TIERS:
        st = statuses[tier]
        key_text = "—"
        if st["api_key_env"]:
            key_text = (f"{_esc(st['api_key_env'])} — "
                        f"{'set' if st['key_present'] else 'not set'}")
        ladder_rows += (f"<tr><td>{tier}</td><td>{_esc(st['provider'] or '—')}</td>"
                        f"<td>{_esc(st['model'] or '—')}</td>"
                        f"<td>{_esc(st['base_url'] or '—')}</td>"
                        f"<td>{key_text}</td>"
                        f"<td>{'usable' if st['usable'] else 'not usable'}</td></tr>")

    validation = settings_module.validate_settings(data)
    valid_line = ("Global settings are valid."
                  if validation.ok else
                  "Problems: " + "; ".join(
                      [f"missing {m}" for m in validation.missing_required]
                      + validation.errors))
    body = f"""
<div class="top"><div><h1>Settings</h1><div class="sub">{_esc(str(path))} — budgets are edited here; everything else is shown read-only. Key values are never shown — only whether each key's environment variable is set.</div></div></div>
{notice_html}
<div class="notice {'ok' if validation.ok else 'warn'}">{_esc(valid_line)}</div>
<div class="row" style="grid-template-columns:1fr 1fr;margin-top:0">
  <div class="q" style="margin-bottom:0"><h4>Budgets (global — never per game)</h4>
    <form class="stack" method="post" action="/settings">
      {bfield("tokens_per_run")}
      {bfield("tokens_per_game")}
      {bfield("cost_ceiling_per_month", "0.01")}
      {bfield("max_build_iterations")}
      {bfield("warn_at_percent_of_cap")}
      <div class="actions"><button class="btn" type="submit">Save budgets</button></div>
      <p class="hint">Saving rewrites this YAML file (comments in it are not kept). Nothing else in the file changes.</p>
    </form>
  </div>
  <div class="card"><h3>Model ladder (bring your own key)</h3>
    <table><tr><th>Tier</th><th>Provider</th><th>Model</th><th>Base URL</th><th>Key env</th><th>Status</th></tr>{ladder_rows}</table>
    <p class="hint" style="margin-top:10px">To change a tier, edit the <code>models</code> section of the settings file. A local server (Ollama, LM Studio) needs no key when its base_url is on this machine.</p>
  </div>
</div>"""
    return _layout("Settings", "settings", body)


def render_result(server, title: str, message: str, back_href: str,
                  kind: str = "ok") -> str:
    body = f"""
<div class="top"><div><h1>{_esc(title)}</h1></div></div>
<div class="notice {kind}">{message}</div>
<p><a class="btn ghost" href="{_esc(back_href)}">Back</a></p>"""
    return _layout(title, "projects", body)


# ---------------------------------------------------------------------------
# Jobs UI — live cards, the dashboard strip, history, the job page

_JOB_STATUS_CHIP = {
    "done": "green", "failed": "red", "cancelled": "grey",
    "interrupted": "grey", "running": "", "queued": "grey",
}


def _job_card(job: dict, reload_on_done: bool = True) -> str:
    """A live job card. The server renders the current snapshot;
    app.js polls /api/jobs/<id> and updates it in place, then
    reloads the page (keeping this card via ?job=) when the job
    lands, so the rest of the page refreshes too."""
    status = job.get("status", "queued")
    chip = _JOB_STATUS_CHIP.get(status, "")
    progress = job.get("progress") or {}
    total = progress.get("total")
    done = progress.get("done")
    if status == "done":
        pct, bar_cls = 100, ""
    elif total:
        pct = int(100 * (done or 0) / total)
        bar_cls = ""
    elif status in ("queued", "running"):
        pct, bar_cls = 35, "indeterminate"
    else:
        pct, bar_cls = 100, ""
    events = ""
    for event in (job.get("events") or [])[-30:]:
        events += (f"<li><span class='hint'>{_esc(event.get('at', ''))}"
                   f"</span> {_esc(event.get('text', ''))}</li>")
    outcome = ""
    if job.get("result"):
        outcome = (f'<p data-role="outcome" style="margin-top:10px">'
                   f'{_esc(job["result"])}</p>')
    elif job.get("error"):
        outcome = (f'<p data-role="outcome" style="margin-top:10px;'
                   f'color:#D24545">{_esc(job["error"])}</p>')
    cancel = ""
    if status in ("queued", "running"):
        cancel = (f'<form method="post" action="/jobs/{_esc(job["id"])}/cancel" '
                  'data-cancel-form style="margin-top:10px">'
                  '<button class="btn ghost small" type="submit">Cancel job</button></form>')
    reload_attr = ' data-reload-on-done="1"' if reload_on_done else ""
    return f"""
<div class="card jobcard" data-job-id="{_esc(job['id'])}" data-job-status="{_esc(status)}"{reload_attr} style="margin-top:20px">
<h3>{_esc(job.get('label', job.get('action', 'Job')))} <span class="chip {chip}" data-role="status">{_esc(status)}</span></h3>
<p class="hint" style="margin:6px 0">Elapsed <span data-role="elapsed">{jobs_module.elapsed_seconds(job):.0f}s</span> · job {_esc(job['id'])}</p>
<div class="bar jobbar"><i data-role="fill" class="{bar_cls}" style="width:{pct}%"></i></div>
<ol class="joblog" data-role="log">{events}</ol>
{outcome}{cancel}
</div>"""


def _job_card_for(server, job_id: str | None) -> str:
    if not job_id:
        return ""
    job = server.jobs.get(job_id)
    if job is None:
        return ""
    return _job_card(job)


def _jobs_strip(server) -> str:
    jobs = server.jobs.list(limit=30)
    if not jobs:
        return ""
    running = [j for j in jobs if j.get("status") in ("queued", "running")]
    recent = [j for j in jobs if j.get("status") not in ("queued", "running")]
    rows = ""
    for job in (running + recent)[:6]:
        status = job.get("status", "")
        chip = _JOB_STATUS_CHIP.get(status, "")
        detail = job.get("error") or job.get("result") or ""
        rows += (f'<div class="check"><div class="box'
                 f'{" open" if status in ("queued", "running") else ""}">'
                 f'{"…" if status in ("queued", "running") else "✓"}</div>'
                 f'<div style="flex:1"><a href="/jobs/{_esc(job["id"])}">'
                 f'{_esc(job.get("label", ""))}</a> '
                 f'<span class="chip {chip}">{_esc(status)}</span>'
                 f'<small>{_esc(job.get("project", ""))} · '
                 f'{_esc(job.get("created", ""))}'
                 f'{(" — " + _esc(detail)) if detail else ""}</small>'
                 f'</div></div>')
    heading = "Jobs — running now" if running else "Jobs — recent"
    return (f'<div class="card" style="margin-top:20px"><h3>{heading}</h3>'
            f'{rows}</div>')


def _job_history_card(server, name: str) -> str:
    jobs = server.jobs.list(project=name, limit=8)
    if not jobs:
        return ""
    rows = ""
    for job in jobs:
        status = job.get("status", "")
        chip = _JOB_STATUS_CHIP.get(status, "")
        detail = job.get("error") or job.get("result") or ""
        rows += (f'<div class="check"><div class="box'
                 f'{" open" if status in ("queued", "running") else ""}">'
                 f'{"…" if status in ("queued", "running") else "✓"}</div>'
                 f'<div style="flex:1"><a href="/jobs/{_esc(job["id"])}">'
                 f'{_esc(job.get("label", ""))}</a> '
                 f'<span class="chip {chip}">{_esc(status)}</span>'
                 f'<small>{_esc(job.get("created", ""))}'
                 f'{(" — " + _esc(detail)) if detail else ""}</small>'
                 f'</div></div>')
    return (f'<div class="card" style="margin-top:20px"><h3>Job history</h3>'
            f'{rows}</div>')


def render_job_page(server, job_id: str) -> str:
    job = server.jobs.get(job_id)
    if job is None:
        raise AppError(f"No job with id {job_id!r} in this workspace.")
    back = "/"
    if job.get("project"):
        back = f"/project/{urllib.parse.quote(str(job['project']))}"
    body = f"""
<div class="top"><div><h1>{_esc(job.get('label', 'Job'))}</h1><div class="sub">{_esc(job.get('project', ''))} · started from the app; the record lives in .weaverbird-app/jobs/ and survives restarts.</div></div>
<div><a class="btn ghost" href="{_esc(back)}">Back to project</a></div></div>
{_job_card(job, reload_on_done=False)}"""
    return _layout("Job", "projects", body)


# ---------------------------------------------------------------------------
# Balance view

def _unit_cell(record) -> str:
    """One player's per-unit record, formatted from the fields
    the record itself carries — a metric a game does not record
    is simply absent, never guessed from the game's kind."""
    if record is None:
        return '<span class="hint">no record</span>'
    bits = ["completed" if record.get("completed") else "NOT completed"]
    if record.get("deaths"):
        bits.append(f"{record['deaths']} deaths")
    if record.get("time_s") is not None:
        bits.append(f"{record['time_s']}s")
    if record.get("moves_used") is not None:
        bits.append(f"{record['moves_used']}/{record.get('move_budget', '?')} moves")
    return _esc(", ".join(bits))


def render_balance(server, name: str, notice: str = "",
                   notice_kind: str = "ok", job_id: str | None = None) -> str:
    project = server.project_by_name(name)
    path = project["path"]
    quoted = urllib.parse.quote(name)
    notice_html = (f'<div class="notice {notice_kind}">{notice}</div>'
                   if notice else "")
    job_html = _job_card_for(server, job_id)
    report = finish_module.load_test_report(path)
    if report is None:
        body = f"""
<div class="top"><div><h1>Balance lab</h1><div class="sub">{_esc(name)} — per-unit difficulty evidence from the scripted players.</div></div>
<div><a class="btn ghost" href="/project/{quoted}">Back to project</a></div></div>
{notice_html}{job_html}
<div class="empty">No test report yet. Run the test stage first — the lab reads the per-unit records the scripted players write into the test report.</div>"""
        return _layout("Balance lab", "projects", body)
    units = balance_module.load_units(path)
    flags = balance_module.analyse(units)
    flags_by_unit: dict = {}
    for flag in flags:
        flags_by_unit.setdefault(flag.get("unit"), []).append(flag)
    rows = ""
    for row in balance_module.unit_rows(units):
        badges = ""
        for flag in flags_by_unit.get(row["unit"], []):
            cls = "red" if flag["kind"] in ("wall", "spike") else ""
            badges += f' <span class="chip {cls}">{_esc(flag["kind"])}</span>'
        rows += (f"<tr><td>Unit {_esc(row['unit'])}</td>"
                 f"<td>{_unit_cell(row['competent'])}</td>"
                 f"<td>{_unit_cell(row['first_time'])}</td>"
                 f"<td>{badges}</td></tr>")
    if not rows:
        rows = ('<tr><td colspan="4" class="hint">The latest test report '
                'has no per-unit records — it was written by an older '
                'build. Re-run the test stage and the scripted players '
                'will record per-unit stats.</td></tr>')
    flag_html = ""
    for flag in flags:
        flag_html += (f'<div class="check"><div class="box open">!</div>'
                      f'<div><b>{_esc(flag["kind"])}</b>'
                      f'<small>{_esc(flag["text"])}</small></div></div>')
    if not flag_html and units:
        flag_html = ('<p class="hint">No flags — no spikes, walls, '
                     'stalls or breeze in the recorded units.</p>')
    model = balance_module.model_summary(path)
    model_html = ""
    if model:
        model_html = (f'<p class="hint" style="margin-top:10px">Model '
                      f'playtest on record: {_esc(model["outcome"])} — '
                      f'{_esc(model["decisions"])} decisions, '
                      f'{_esc(model["deaths"])} deaths, finished: '
                      f'{_esc(model["finished"])}.</p>')
    try:
        found = balance_module.suggestions(path)
    except balance_module.BalanceError as exc:
        found = []
        flag_html += f'<p class="hint">{_esc(exc)}</p>'
    sugg_html = ""
    for index, suggestion in enumerate(found, start=1):
        changes = "".join(f"<small>{_esc(line)}</small><br>"
                           for line in suggestion["changes"])
        sugg_html += f"""
<div class="check"><div class="box open">{index}</div>
<div style="flex:1"><b>{_esc(suggestion['kind'])}</b> — {_esc(suggestion['rationale'])}
<div style="margin-top:4px">{changes}</div></div>
<div>
<form class="action" method="post" action="/project/{quoted}/balance/apply"><input type="hidden" name="suggestion" value="{index}"><button class="btn" type="submit">Apply (patch gates)</button></form>
<form class="action" method="post" action="/project/{quoted}/balance/compare"><input type="hidden" name="suggestion" value="{index}"><button class="btn ghost" type="submit">Compare A/B</button></form>
</div></div>"""
    if not sugg_html:
        sugg_html = ('<p class="hint">No suggestions on offer — either '
                     'nothing is flagged, or the flagged fixes are '
                     'already at their bounds.</p>')
    compare = balance_module.load_compare_report(path)
    compare_html = ""
    if compare:
        base = compare.get("baseline", {})
        cand = compare.get("candidate_metrics", {})

        def metric(label, key, fmt="{}"):
            b, c = base.get(key), cand.get(key)
            return (f"<tr><td>{label}</td><td>{_esc(fmt.format(b) if b is not None else '—')}</td>"
                    f"<td>{_esc(fmt.format(c) if c is not None else '—')}</td></tr>")

        unit_rows_cmp = ""
        base_units = {(u.get("unit"), u.get("source")): u
                      for u in base.get("units", [])}
        cand_units = {(u.get("unit"), u.get("source")): u
                      for u in cand.get("units", [])}
        for key in sorted(set(base_units) | set(cand_units),
                          key=lambda k: (k[0] or 0, k[1] or "")):
            b = base_units.get(key, {})
            c = cand_units.get(key, {})
            unit_rows_cmp += (
                f"<tr><td>Unit {_esc(key[0])} ({_esc(key[1])})</td>"
                f"<td>{_esc(b.get('deaths', '—'))} deaths · {_esc(b.get('time_s', '—'))}s</td>"
                f"<td>{_esc(c.get('deaths', '—'))} deaths · {_esc(c.get('time_s', '—'))}s</td></tr>")
        compare_html = f"""
<div class="card" style="margin-top:20px"><h3>Last compare — {_esc(compare.get('candidate', ''))}</h3>
<p style="margin:8px 0">{_esc(compare.get('verdict', ''))}</p>
<p class="hint">{_esc('; '.join(compare.get('changes', [])))} · compared {_esc(compare.get('compared_at', ''))}; the project was restored to its starting state afterwards.</p>
<table><tr><th>Metric</th><th>Baseline</th><th>Candidate</th></tr>
{metric("Test suite", "ok")}{metric("Win proven", "win_proven")}{metric("Lose proven", "lose_proven")}{metric("Average FPS", "avg_fps")}{metric("Total unit deaths (competent)", "total_deaths")}{metric("Total unit time (competent)", "total_time_s", "{}s")}{metric("First-time finished", "first_time_finished")}
{unit_rows_cmp}</table></div>"""
    body = f"""
<div class="top"><div><h1>Balance lab</h1><div class="sub">{_esc(name)} — what the scripted players found, unit by unit. Suggestions use only the patch loop's bounded knobs; applying one ships through the full patch gates.</div></div>
<div><a class="btn ghost" href="/project/{quoted}">Back to project</a></div></div>
{notice_html}{job_html}
<div class="card" style="margin-top:20px"><h3>Per-unit records</h3>
<table><tr><th>Unit</th><th>Competent</th><th>First-time</th><th>Flags</th></tr>{rows}</table>
{model_html}</div>
<div class="card" style="margin-top:20px"><h3>Flags</h3>{flag_html}</div>
<div class="card" style="margin-top:20px"><h3>Suggestions</h3>{sugg_html}</div>
{compare_html}"""
    return _layout("Balance lab", "projects", body)


# ---------------------------------------------------------------------------
# Asset studio view

def render_assets(server, name: str, notice: str = "",
                  notice_kind: str = "ok", job_id: str | None = None,
                  palette_before: dict | None = None) -> str:
    project = server.project_by_name(name)
    path = project["path"]
    quoted = urllib.parse.quote(name)
    notice_html = (f'<div class="notice {notice_kind}">{notice}</div>'
                   if notice else "")
    job_html = _job_card_for(server, job_id)
    try:
        listing = assets_module.list_assets(path)
    except assets_module.AssetsError as exc:
        body = f"""
<div class="top"><div><h1>Asset studio</h1><div class="sub">{_esc(name)}</div></div>
<div><a class="btn ghost" href="/project/{quoted}">Back to project</a></div></div>
{notice_html}{job_html}
<div class="empty">{_esc(exc)}</div>"""
        return _layout("Asset studio", "projects", body)
    cards = ""
    for entry in listing:
        rel = entry["path"]
        kind = entry.get("kind", "file")
        file_url = f"/project/{quoted}/asset-file/{urllib.parse.quote(rel)}"
        # Visuals follow the entry's kind (from the generator
        # registry / manifest data), never a hardcoded name list.
        if entry["missing"]:
            visual = '<span class="chip red">missing</span>'
        elif kind == "icon":
            visual = (f'<img src="{file_url}" width="96" height="96" alt="icon"> '
                      f'<img src="{file_url}" width="48" height="48" alt="icon"> '
                      f'<img src="{file_url}" width="24" height="24" alt="icon">')
        elif kind in ("sprite", "image"):
            visual = (f'<img src="{file_url}" width="96" height="96" '
                      f'alt="{_esc(entry["name"])}" '
                      'style="background:#F0EFF6;border-radius:8px">')
        elif kind == "audio":
            visual = f'<audio controls src="{file_url}"></audio>'
        else:
            visual = ""
        size = ("missing" if entry["missing"]
                else f"{entry['size']} bytes")
        regen = ""
        if entry.get("regenerable"):
            regen = (
                f'<form class="action" method="post" '
                f'action="/project/{quoted}/assets/regenerate" '
                'style="margin-top:10px">'
                f'<input type="hidden" name="asset" '
                f'value="{_esc(entry["name"])}">'
                '<button class="btn ghost small" type="submit">'
                'Regenerate</button></form>')
        cards += f"""
<div class="card"><h3>{_esc(entry['name'])}</h3>
<div style="margin:10px 0">{visual}</div>
<p class="hint">{_esc(rel)} · {size}</p>
<p class="hint" style="margin-top:6px">{_esc(entry['note'] or 'No provenance note.')}</p>
<p class="hint">{_esc(entry['provider'] or entry['source'] or '')}</p>
{regen}
</div>"""
    # Palette editor.
    state = assets_module.palette_state(path)
    role_rows = ""
    for role in state["roles"]:
        role_rows += (
            f'<label for="pal_{_esc(role["role"])}">{_esc(role["label"])}'
            f'{" (pinned in the scope)" if role["pinned"] else " (derived)"}</label>'
            f'<div style="display:flex;gap:8px;align-items:center">'
            f'<input type="color" id="pal_{_esc(role["role"])}" '
            f'name="{_esc(role["role"])}" value="{_esc(role["hex"].lower())}" '
            'style="width:56px;height:34px;padding:2px">'
            f'<span class="hint">{_esc(role["hex"])}</span></div>')
    before_after = ""
    if palette_before:
        def swatches(palette: dict) -> str:
            out = ""
            for _role, label, key in assets_module.PALETTE_ROLES:
                out += (f'<span title="{label}: {_esc(palette.get(key, ""))}" '
                        f'style="display:inline-block;width:34px;height:34px;'
                        f'border-radius:8px;background:{_esc(palette.get(key, ""))};'
                        'margin-right:6px;border:1px solid #E5E3EE"></span>')
            return out
        before_after = (
            f'<div style="margin-top:14px"><p class="hint">Before</p>'
            f'<div>{swatches(palette_before)}</div>'
            f'<p class="hint" style="margin-top:8px">After</p>'
            f'<div>{swatches(state["palette"])}</div></div>')
    body = f"""
<div class="top"><div><h1>Asset studio</h1><div class="sub">{_esc(name)} — every generated asset with its provenance. Regenerating redraws one file from the current palette; it costs no tokens.</div></div>
<div><a class="btn ghost" href="/project/{quoted}">Back to project</a></div></div>
{notice_html}{job_html}
<div class="actions" style="margin-top:16px">
  <form class="action" method="post" action="/project/{quoted}/generate-assets"><button class="btn" type="submit">Regenerate all assets</button></form>
</div>
<div class="grid" style="margin-top:16px">{cards}</div>
<div class="card" style="margin-top:20px"><h3>Palette — the style lock</h3>
<p class="hint" style="margin:6px 0 10px">Saving writes the colours back into the scope's Palette field, in role order (accent, target, hazard, platform, crate). Asset files pick the new palette up when they are regenerated; the game's own template colours need a game rebuild.</p>
<form class="stack" method="post" action="/project/{quoted}/assets/palette">
{role_rows}
<div class="actions"><button class="btn" type="submit">Save palette to the scope</button></div>
</form>
{before_after}
<div class="actions" style="margin-top:14px">
  <form class="action" method="post" action="/project/{quoted}/generate-assets"><button class="btn ghost" type="submit">Regenerate all assets</button></form>
  <form class="action" method="post" action="/project/{quoted}/build-game"><button class="btn ghost" type="submit">Build game (picks up template colours)</button></form>
</div>
</div>"""
    return _layout("Asset studio", "projects", body)


# ---------------------------------------------------------------------------
# The server

class _Handler(BaseHTTPRequestHandler):
    server_version = f"Weaverbird/{__version__}"
    protocol_version = "HTTP/1.1"

    # -- plumbing ------------------------------------------------------
    def log_message(self, *args):  # keep the terminal quiet
        pass

    @property
    def app(self) -> "WeaverbirdAppServer":
        return self.server  # type: ignore[return-value]

    def _send_html(self, text: str, status: int = 200) -> None:
        data = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_bytes(self, data: bytes, content_type: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_json(self, payload, status: int = 200) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _form(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8", "replace")
        parsed = urllib.parse.parse_qs(raw, keep_blank_values=True)
        return {key: values[-1] for key, values in parsed.items()}

    def _fail_page(self, message: str, status: int = 400) -> None:
        self._send_html(render_result(self.app, "Something needs attention",
                                      _esc(message), "/"), status)

    # -- routing ---------------------------------------------------------
    def do_GET(self):  # noqa: N802 (stdlib handler name)
        parsed = urllib.parse.urlparse(self.path)
        parts = [p for p in parsed.path.split("/") if p]
        query = urllib.parse.parse_qs(parsed.query)
        job_id = (query.get("job") or [None])[0]
        try:
            if parsed.path == "/style.css":
                return self._send_bytes(_web_text("style.css").encode(), "text/css")
            if parsed.path == "/app.js":
                return self._send_bytes(_web_text("app.js").encode(),
                                        "text/javascript")
            if parsed.path == "/mark.svg":
                return self._send_bytes(_asset_bytes("weaverbird.svg"),
                                        "image/svg+xml")
            if parts == ["api", "jobs"]:
                project = (query.get("project") or [None])[0]
                jobs = self.app.jobs.list(project=project, limit=30)
                return self._send_json(
                    {"jobs": [jobs_module.public_job(j) for j in jobs]})
            if len(parts) == 3 and parts[:2] == ["api", "jobs"]:
                job = self.app.jobs.get(urllib.parse.unquote(parts[2]))
                if job is None:
                    return self._send_json(
                        {"error": "no such job"}, status=404)
                return self._send_json(jobs_module.public_job(job))
            if len(parts) == 2 and parts[0] == "jobs":
                return self._send_html(render_job_page(
                    self.app, urllib.parse.unquote(parts[1])))
            if not parts:
                return self._send_html(render_dashboard(self.app))
            if parts == ["settings"]:
                return self._send_html(render_settings(self.app))
            if parts[0] == "project" and len(parts) >= 2:
                name = urllib.parse.unquote(parts[1])
                if len(parts) == 2:
                    return self._send_html(render_project(
                        self.app, name, job_id=job_id))
                if len(parts) == 3 and parts[2] == "scope":
                    return self._send_html(render_scope(self.app, name))
                if len(parts) == 3 and parts[2] == "feedback":
                    return self._send_html(render_feedback(
                        self.app, name, job_id=job_id))
                if len(parts) == 3 and parts[2] == "balance":
                    return self._send_html(render_balance(
                        self.app, name, job_id=job_id))
                if len(parts) == 3 and parts[2] == "assets":
                    return self._send_html(render_assets(
                        self.app, name, job_id=job_id))
                if len(parts) >= 4 and parts[2] == "asset-file":
                    return self._get_asset_file(name, parts[3:])
            return self._fail_page("That page does not exist here.", 404)
        except AppError as exc:
            return self._fail_page(str(exc), 404)
        except Exception as exc:  # a page must fail plainly, never hang
            return self._fail_page(f"{type(exc).__name__}: {exc}", 500)

    def _get_asset_file(self, name: str, rest: list) -> None:
        """Serve one generated asset file, read-only, confined to
        the project's game/assets/ tree (plus the game icon)."""
        project = self.app.project_by_name(name)
        rel = "/".join(urllib.parse.unquote(p) for p in rest)
        game_dir = (project["path"] / skeleton_module.GAME_DIR_NAME)
        candidate = (project["path"] / rel).resolve()
        assets_root = (game_dir / "assets").resolve()
        icon = (game_dir / "icon.svg").resolve()
        allowed = candidate == icon or candidate.is_relative_to(assets_root)
        if not allowed or not candidate.is_file():
            return self._fail_page("That asset does not exist here.", 404)
        content_types = {".svg": "image/svg+xml", ".wav": "audio/wav",
                         ".png": "image/png"}
        content_type = content_types.get(candidate.suffix.lower(),
                                         "application/octet-stream")
        return self._send_bytes(candidate.read_bytes(), content_type)

    def do_POST(self):  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        parts = [p for p in parsed.path.split("/") if p]
        try:
            if parts == ["settings"]:
                return self._post_settings()
            if len(parts) == 4 and parts[:2] == ["api", "jobs"] \
                    and parts[3] == "cancel":
                job = self.app.jobs.cancel(urllib.parse.unquote(parts[2]))
                if job is None:
                    return self._send_json(
                        {"error": "no such job"}, status=404)
                return self._send_json(jobs_module.public_job(job))
            if len(parts) == 3 and parts[0] == "jobs" \
                    and parts[2] == "cancel":
                job_id = urllib.parse.unquote(parts[1])
                if self.app.jobs.cancel(job_id) is None:
                    return self._fail_page("No such job.", 404)
                return self._send_html(render_job_page(self.app, job_id))
            if parts[0] == "project" and len(parts) >= 3:
                name = urllib.parse.unquote(parts[1])
                action = parts[2]
                if action == "feedback":
                    return self._post_feedback(name, parts)
                if action == "patch" and len(parts) == 4:
                    return self._post_patch_start(name, parts[3])
                if action == "run-plan":
                    return self._post_job_action(name, "orchestrate")
                if action == "worker" and len(parts) == 4:
                    return self._post_job_action(
                        name, "run-worker",
                        params={"worker": urllib.parse.unquote(parts[3])})
                if action == "repair":
                    return self._post_job_action(name, "repair")
                if action == "model-playtest":
                    return self._post_job_action(name, "model-playtest")
                if action == "build-skeleton":
                    return self._post_job_action(name, "build-skeleton")
                if action == "generate-assets":
                    return self._post_job_action(name, "generate-assets")
                if action == "build-game":
                    return self._post_job_action(name, "build-game")
                if action == "run-tests":
                    return self._post_job_action(name, "run-tests")
                if action == "finish-check":
                    return self._post_job_action(name, "finish-check")
                if action == "finish-report":
                    return self._post_job_action(name, "finish-report")
                if action == "balance" and len(parts) == 4 \
                        and parts[3] in ("apply", "compare"):
                    return self._post_balance(name, parts[3])
                if action == "assets" and len(parts) == 4 \
                        and parts[3] == "regenerate":
                    return self._post_assets_regenerate(name)
                if action == "assets" and len(parts) == 4 \
                        and parts[3] == "palette":
                    return self._post_assets_palette(name)
                if action == "advance":
                    return self._post_advance(name)
                if action == "scope" and len(parts) == 3:
                    return self._post_scope_save(name)
                if action == "scope" and len(parts) == 4 and parts[3] == "extract":
                    return self._post_scope_extract(name)
                if action == "scope" and len(parts) == 4 and parts[3] == "approve":
                    return self._post_scope_approve(name)
            return self._fail_page("That action does not exist here.", 404)
        except AppError as exc:
            return self._fail_page(str(exc), 404)
        except Exception as exc:
            return self._fail_page(f"{type(exc).__name__}: {exc}", 500)

    # -- job-backed actions ------------------------------------------------
    def _post_job_action(self, name: str, action: str,
                         params: dict | None = None,
                         view: str = "project") -> None:
        """Every long project action is a job now: submit it,
        answer at once with the page carrying the live job card."""
        project = self.app.project_by_name(name)
        job = self.app.jobs.submit(project["name"], project["path"],
                                   action, params=params)
        if view == "feedback":
            return self._send_html(render_feedback(
                self.app, name, job_id=job["id"]))
        if view == "balance":
            return self._send_html(render_balance(
                self.app, name, job_id=job["id"]))
        if view == "assets":
            return self._send_html(render_assets(
                self.app, name, job_id=job["id"]))
        return self._send_html(render_project(
            self.app, name, job_id=job["id"]))

    def _post_balance(self, name: str, sub: str) -> None:
        form = self._form()
        try:
            index = int(form.get("suggestion", ""))
        except ValueError:
            return self._fail_page("That suggestion number is not "
                                   "a number.", 400)
        action = "balance-apply" if sub == "apply" else "balance-compare"
        return self._post_job_action(name, action,
                                     params={"suggestion": index},
                                     view="balance")

    def _post_assets_regenerate(self, name: str) -> None:
        form = self._form()
        asset = form.get("asset", "").strip()
        if not asset:
            return self._fail_page("No asset was named.", 400)
        return self._post_job_action(name, "assets-regenerate",
                                     params={"asset": asset},
                                     view="assets")

    def _post_assets_palette(self, name: str) -> None:
        project = self.app.project_by_name(name)
        form = self._form()
        try:
            before = assets_module.palette_state(project["path"])
            hexes = [form.get(role, "")
                     for role, _label, _key in assets_module.PALETTE_ROLES]
            assets_module.save_palette(project["path"], hexes)
        except (assets_module.AssetsError,
                scope_module.ScopeError) as exc:
            return self._send_html(render_assets(
                self.app, name,
                notice=_esc(f"Palette not saved. {exc}"),
                notice_kind="bad"))
        return self._send_html(render_assets(
            self.app, name,
            notice="Palette saved — the colours are written back "
                   "into the scope's Palette field. Regenerate the "
                   "assets to redraw them in the new palette; a "
                   "game rebuild picks up palette-driven template "
                   "colours.",
            notice_kind="ok", palette_before=before["palette"]))

    # -- actions ---------------------------------------------------------
    def _post_feedback(self, name: str, parts: list) -> None:
        project = self.app.project_by_name(name)
        path = project["path"]
        sub = parts[3] if len(parts) > 3 else ""
        if sub == "fetch" and len(parts) == 4:
            return self._post_job_action(name, "feedback-fetch",
                                         view="feedback")
        if sub == "triage" and len(parts) == 4:
            return self._post_job_action(name, "feedback-triage",
                                         view="feedback")
        if sub == "add" and len(parts) == 4:
            form = self._form()
            text = form.get("text", "").strip()
            if not text:
                return self._send_html(render_feedback(
                    self.app, name, notice="The item was empty — nothing added.",
                    notice_kind="warn"))
            item, created = feedback_module.add_item(
                path, source="manual", text=text)
            return self._send_html(render_feedback(
                self.app, name,
                notice=_esc(f"{'Added' if created else 'Already on record'}: "
                            f"[{item['id']}] {item['text']}"),
                notice_kind="ok"))
        if len(parts) == 5 and parts[4] in ("approve", "dismiss"):
            try:
                item = feedback_module.set_status(
                    path, parts[3],
                    "approved" if parts[4] == "approve" else "dismissed")
            except feedback_module.FeedbackError as exc:
                return self._send_html(render_feedback(
                    self.app, name, notice=_esc(str(exc)), notice_kind="bad"))
            return self._send_html(render_feedback(
                self.app, name,
                notice=_esc(f"[{item['id']}] is now {item['status']}."),
                notice_kind="ok"))
        return self._fail_page("That feedback action does not exist here.", 404)

    def _post_patch_start(self, name: str, item_id: str) -> None:
        return self._post_job_action(name, "patch-start",
                                     params={"item": item_id},
                                     view="feedback")

    def _post_advance(self, name: str) -> None:
        project = self.app.project_by_name(name)
        # Stage handlers delegate to the worker adapters, so the
        # app, the CLI and the orchestrator run the same steps.
        workers_module.register_stage_handlers()
        try:
            state = state_module.advance(project["path"])
        except state_module.PipelineError as exc:
            return self._send_html(render_project(
                self.app, name, notice=_esc(f"Not advanced. {exc}"),
                notice_kind="warn"))
        return self._send_html(render_project(
            self.app, name,
            notice=f"Advanced. The pipeline is now at stage: {_esc(state['stage'])}.",
            notice_kind="ok"))

    def _post_scope_save(self, name: str) -> None:
        project = self.app.project_by_name(name)
        scope_file = scope_module.scope_path_for(project["path"])
        form = self._form()
        parsed = scope_module.load_scope(scope_file)
        changed = []
        for fld in parsed.fields:
            if fld.name in form and form[fld.name].strip() != fld.value:
                scope_module.set_field_value(scope_file, fld.name,
                                             form[fld.name])
                changed.append(fld.name)
        note = ("Saved: " + ", ".join(changed)) if changed else "No changes."
        return self._send_html(render_scope(
            self.app, name, notice=_esc(note + " The scope file has been updated."),
            notice_kind="ok"))

    def _post_scope_extract(self, name: str) -> None:
        project = self.app.project_by_name(name)
        form = self._form()
        prompt = form.get("prompt", "")
        if not prompt.strip():
            return self._send_html(render_scope(
                self.app, name, notice="Write a prompt first — the box was empty.",
                notice_kind="warn"))
        settings_path, _ = workspace_settings(self.app.workspace)
        outcome = brain_module.interview_from_prompt(
            project["path"], prompt, settings_path=settings_path)
        bits = ["Prompt stored in the scope file."]
        if outcome["applied"]:
            bits.append("Extracted: " + ", ".join(
                f"{k} = {v}" for k, v in outcome["applied"].items()))
        if outcome.get("note"):
            bits.append(str(outcome["note"]))
        bits.append(f"{outcome['missing_count']} required field(s) still missing.")
        return self._send_html(render_scope(
            self.app, name, notice=_esc(" ".join(bits)),
            notice_kind="ok" if outcome["brain_used"] else "warn"))

    def _post_scope_approve(self, name: str) -> None:
        project = self.app.project_by_name(name)
        form = self._form()
        scope_file = scope_module.scope_path_for(project["path"])
        try:
            parsed = scope_module.approve_scope(scope_file,
                                                by=form.get("by", ""))
        except scope_module.ScopeError as exc:
            return self._send_html(render_scope(
                self.app, name, notice=_esc(str(exc)), notice_kind="bad"))
        return self._send_html(render_scope(
            self.app, name,
            notice=f"Scope approved and frozen. {_esc(parsed.approval)}",
            notice_kind="ok"))

    def _post_settings(self) -> None:
        path, data = workspace_settings(self.app.workspace)
        if path is None:
            return self._fail_page("No global-settings.yaml exists in this workspace.")
        form = self._form()
        data = dict(data or {})
        budgets = dict(data.get("budgets", {}))
        numeric = {"tokens_per_run": int, "tokens_per_game": int,
                   "max_build_iterations": int, "warn_at_percent_of_cap": int,
                   "cost_ceiling_per_month": float}
        errors = []
        for field_name, convert in numeric.items():
            raw = form.get(field_name, "")
            if raw is None or str(raw).strip() == "":
                budgets[field_name] = None
                continue
            try:
                budgets[field_name] = convert(str(raw).strip())
            except ValueError:
                errors.append(f"{field_name} must be a number.")
        if errors:
            return self._send_html(render_settings(
                self.app, notice="Not saved: " + "; ".join(errors),
                notice_kind="bad"))
        candidate = dict(data)
        candidate["budgets"] = budgets
        validation = settings_module.validate_settings(candidate)
        if not validation.ok:
            problems = [f"missing {m}" for m in validation.missing_required] \
                + validation.errors
            return self._send_html(render_settings(
                self.app,
                notice="Not saved — the budgets would be invalid: "
                       + "; ".join(problems),
                notice_kind="bad"))
        path.write_text(yaml.safe_dump(candidate, sort_keys=False),
                        encoding="utf-8")
        return self._send_html(render_settings(
            self.app, notice="Budgets saved to the workspace global settings.",
            notice_kind="ok"))


class WeaverbirdAppServer(ThreadingHTTPServer):
    """The local app server. Binds to 127.0.0.1 only — see module doc."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, workspace, port: int = 8765):
        self.workspace = Path(workspace)
        if not self.workspace.is_dir():
            raise AppError(f"Workspace not found: {self.workspace}")
        # The job runner: every project action runs through it.
        # Creating it also marks any job a previous server left
        # mid-flight as interrupted, honestly.
        self.jobs = jobs_module.JobManager(self.workspace)
        super().__init__(("127.0.0.1", port), _Handler)

    def server_close(self) -> None:
        try:
            self.jobs.shutdown()
        finally:
            super().server_close()

    def project_by_name(self, name: str) -> dict:
        for project in find_projects(self.workspace):
            if project["name"] == name:
                return project
        raise AppError(f"No project named {name!r} in this workspace.")

    @property
    def url(self) -> str:
        host, port = self.server_address[:2]
        return f"http://{host}:{port}/"


def create_server(workspace, port: int = 8765) -> WeaverbirdAppServer:
    return WeaverbirdAppServer(workspace, port)


def run_app(workspace, port: int = 8765, open_browser: bool = False) -> int:
    """Serve the app until interrupted. Returns a process exit code.

    This is what bare ``weaverbird`` runs: the app is the product
    surface. With open_browser, the app is opened in the user's
    browser; when no browser is available the URL is printed and the
    server keeps serving.
    """
    try:
        server = create_server(workspace, port)
    except (AppError, OSError) as exc:
        print(f"The app could not start: {exc}")
        return 1
    # A launcher that wraps the app in the background can leave
    # SIGINT set to "ignore"; restore the normal Ctrl+C behaviour so
    # stopping the app always works the way the banner promises.
    try:
        import signal
        signal.signal(signal.SIGINT, signal.default_int_handler)
    except (ValueError, OSError):
        pass
    print(f"Weaverbird — workspace {server.workspace}", flush=True)
    opened = False
    if open_browser:
        try:
            import webbrowser
            opened = bool(webbrowser.open(server.url))
        except Exception:
            opened = False
    if opened:
        print(f"Opening {server.url} in your browser.", flush=True)
    else:
        print(f"Open {server.url} in a browser.", flush=True)
    print("Local only (127.0.0.1) — no account, no sign-in, it never "
          "leaves this machine. Stop with Ctrl+C.", flush=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        thread.join()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        server.server_close()
    print("Goodbye — Weaverbird is stopped.")
    return 0
