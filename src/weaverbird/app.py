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
stage actions), the scope interview as a chat (the interview
engine's thread + a live scope panel, with the old form kept as
"Edit as form"), a settings view
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
import re
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path

import yaml

from . import __version__, template_text
from . import assets_gen as assets_module
from . import balance as balance_module
from . import demo as demo_module
from . import exports as exports_module
from . import brain as brain_module
from . import feedback as feedback_module
from . import finish as finish_module
from . import generate as generate_module
from . import interview as interview_module
from . import jobs as jobs_module
from . import testing as testing_module
from . import godot as godot_module
from . import ledger as ledger_module
from . import provenance as provenance_module
from . import scope as scope_module
from . import settings as settings_module
from . import orchestrator as orchestrator_module
from . import projects as projects_module
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


def _notice_html(notice: str, notice_kind: str) -> str:
    """A notice banner — the ONE place notice text becomes HTML.

    Notices are text, never markup: every render_* escapes its
    notice here, and callers pass plain strings (never pre-escaped
    fragments, which would double-escape). Before 0.13.0 the
    renders inserted notices raw and trusted callers to escape;
    one caller didn't, and a settings provider string became live
    markup (security finding F2)."""
    if not notice:
        return ""
    return f'<div class="notice {notice_kind}">{_esc(notice)}</div>'


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
        "title": (values.get("Title (working)")
                  or projects_module.display_name_for(path)
                  or project["name"]),
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


def _layout(title: str, active: str, body: str, foot: str = "",
            app_class: str = "") -> str:
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
</head><body><div class="app{app_class}">
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

def render_dashboard(server, notice: str = "",
                     notice_kind: str = "ok") -> str:
    workspace = server.workspace
    projects = find_projects(workspace)
    _, settings_data = workspace_settings(workspace)
    statuses = brain_module.brain_status(settings_data)
    notice_html = _notice_html(notice, notice_kind)

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
    demo_form = (
        '<form class="action" method="post" action="/demo">'
        '<button class="btn ghost" type="submit">Open the demo '
        'project</button></form>'
        if demo_module.demo_available() else "")
    if not cards:
        cards = ('<div class="empty">No projects in this workspace '
                 'yet — start your first game, or open the bundled '
                 'demo: a finished game the factory built, to look '
                 'around in.<br><br>'
                 '<a class="btn" href="/new">New project</a> '
                 + demo_form +
                 '<p class="hint" style="margin-top:14px">From a '
                 'terminal, <code>weaverbird init &lt;dir&gt;</code> '
                 'inside this workspace does the same thing.</p></div>')

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
        if st["usable"] and st["provider"] == "opencode":
            text = (f"{tier} — opencode {_esc(st['model'])}, via your "
                    "OpenCode server (no key here)")
        elif st["usable"]:
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
<div class="top"><div><h1>Projects</h1><div class="sub">Every game in this workspace, where it is, and what it has cost.</div></div>
<div><a class="btn" href="/new">New project</a> {demo_form}</div></div>
{notice_html}
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


def render_new_project(server, notice: str = "",
                       notice_kind: str = "ok",
                       values: dict | None = None) -> str:
    """The New project form: a name, a folder, an optional
    starting prompt. Creation runs through projects.new_project —
    the same scaffold `weaverbird init` performs — so an app-made
    project and a CLI-made one are identical on disk."""
    values = values or {}
    notice_html = _notice_html(notice, notice_kind)
    body = f"""
<div class="top"><div><h1>New project</h1><div class="sub">Start a game in this workspace. Weaverbird creates the project folder and its scope file; the scope interview takes it from there.</div></div>
<div><a class="btn ghost" href="/">Back to projects</a></div></div>
{notice_html}
<div class="q"><form class="stack" method="post" action="/new">
  <label for="np_name">Project name</label>
  <input type="text" id="np_name" name="name" value="{_esc(values.get('name', ''))}" placeholder="Star Shepherd">
  <p class="hint">The working title of your game — it becomes the scope's Title.</p>
  <label for="np_folder">Folder name</label>
  <input type="text" id="np_folder" name="folder" value="{_esc(values.get('folder', ''))}" placeholder="star-shepherd">
  <p class="hint">Lowercase letters, digits and dashes. Filled in from the name until you type your own.</p>
  <label for="np_prompt">Starting prompt <span class="hint">(optional)</span></label>
  <textarea id="np_prompt" name="prompt" rows="4" placeholder="Describe the game in plain words — the interview reads this first and only asks about what is missing…">{_esc(values.get('prompt', ''))}</textarea>
  <div class="actions"><button class="btn" type="submit">Create project</button></div>
</form></div>"""
    return _layout("New project", "projects", body)


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


def _build_summary_card(server, project: dict) -> str:
    """The Build summary card: what WILL be built, derived from
    the scope before any run — the game plan (movement,
    objective, fail rules, entities with their behaviours and
    the derivation notes that quote the scope), the key numbers
    with their source ("from your scope" vs "default"), the
    style lock and the planned asset kinds. This is where a
    scope-stated number — and a bespoke-required mechanic —
    shows up pre-build, not after generation."""
    try:
        summary = generate_module.build_summary(project["path"])
    except Exception:
        return ""
    if not summary.get("scope_present"):
        return ""
    plan = summary.get("plan") or {}
    bespoke_required = summary.get("bespoke_required")
    entity_line = ""
    notes_line = ""
    if bespoke_required:
        comp_line = (
            "This game's core mechanic needs bespoke work — "
            f"{_esc(bespoke_required.get('mechanic', ''))} "
            f"({_esc(bespoke_required.get('because', ''))}). "
            "Generation stops at the plan unless a brain is "
            "configured to write the mechanics module. No "
            "substitute game will be built.")
    elif plan:
        comp_line = (
            f"{_esc(plan.get('dimension', '2d'))} · movement: "
            f"{_esc(plan.get('movement', ''))} · objective: "
            f"{_esc(plan.get('objective', ''))} · "
            f"fail: {_esc(', '.join(plan.get('fail') or []) or 'none')}")
        entities = plan.get("entities") or []
        if entities:
            entity_line = ("<p class='hint' style='margin-top:6px'>"
                           "Entities: " + ", ".join(
                               f"{_esc(e.get('label', e.get('kind', '')))} "
                               f"({_esc(e.get('behaviour', ''))})"
                               for e in entities) + ".</p>")
        derivations = plan.get("derivations") or []
        if derivations:
            notes_line = ("<p class='hint' style='margin-top:6px'>"
                          "How the plan reads your scope: "
                          + " · ".join(_esc(d) for d in derivations)
                          + "</p>")
    else:
        comp = summary.get("composition") or {}
        if summary.get("bespoke"):
            comp_line = ("Bespoke mechanics — the scope's core loop "
                         "cannot be composed from the factory's "
                         "primitives, so a model-written module "
                         "implements it.")
        elif comp:
            comp_line = (
                f"{_esc(comp.get('dimension', '2d'))} · movement: "
                f"{_esc(comp.get('movement', ''))} · objective: "
                f"{_esc(', '.join(comp.get('objectives') or []))} · "
                f"fail: {_esc(', '.join(comp.get('fail') or []))} · "
                f"structure: {_esc(comp.get('structure', ''))} · "
                f"interactions: "
                f"{_esc(', '.join(comp.get('interactions') or []))}")
        else:
            comp_line = ""
    preset_line = ""
    if not plan and summary.get("preset"):
        preset_line = (f"<p style='margin-top:6px'>Preset: "
                       f"<b>{_esc(summary['preset'])}</b> "
                       f"<span class='hint'>— "
                       f"{_esc(summary.get('reason', ''))}</span></p>")
    rows = ""
    for number in summary.get("numbers", []):
        source = ("from your scope" if number.get("source") == "scope"
                  else "default")
        note = (f" <span class='hint'>({_esc(number['note'])})</span>"
                if number.get("note") else "")
        rows += (f"<tr><td>{_esc(number['label'])}</td>"
                 f"<td><b>{_esc(number['value'])}</b>{note}</td>"
                 f"<td class='hint'>{source}</td></tr>")
    palette = summary.get("palette", {})
    style_line = _esc(summary.get("style") or "not chosen yet")
    kinds = summary.get("asset_kinds") or []
    kinds_line = (f"<p class='hint' style='margin-top:8px'>Planned "
                  f"asset kinds (from the world's entities): "
                  f"{_esc(', '.join(kinds))}.</p>" if kinds else "")
    firm_line = ""
    if not summary.get("complete"):
        firm_line = ('<p class="hint" style="margin-top:8px">The '
                     'scope is not complete yet — this summary '
                     'firms up as the scope fills in.</p>')
    return f"""
<div class="card" style="margin-top:20px"><h3>Build summary — what will be built</h3>
<p style="margin-top:8px">{comp_line}</p>{preset_line}{entity_line}{notes_line}
<table style="margin-top:10px"><tr><th>Number</th><th>Value</th><th>Source</th></tr>{rows}</table>
<p class="hint" style="margin-top:8px">Style lock: {style_line} · Palette accent {_esc(palette.get('accent_hex', ''))}, target {_esc(palette.get('target_hex', ''))}</p>
{kinds_line}{firm_line}</div>"""


def _exports_card(server, project: dict, quoted: str,
                  state: dict | None) -> str:
    """The Exports card (0.10.0): finished projects only. Target
    checkboxes (all on by default) and a Run exports job; after
    a run, the artifact list with sizes and download links, and
    blocked targets with their reason shown here — not just in
    the report file. Exports are local files only."""
    if not state or state.get("stage") != "finished":
        return ""
    record = exports_module.load_record(project["path"])
    rows = ""
    if record:
        for entry in record.get("targets", []):
            target = _esc(entry.get("target", ""))
            if entry.get("status") == "exported":
                filename = _esc(str(entry.get("artifact", ""))
                                .rsplit("/", 1)[-1])
                size = entry.get("size", 0)
                rows += (
                    f'<div class="check"><div class="box">✓</div>'
                    f'<div>{target}<small><a href="/project/{quoted}'
                    f'/export-file/{filename}">{filename}</a> · '
                    f'{size} bytes · {_esc(entry.get("note", ""))}'
                    "</small></div></div>")
            else:
                rows += (
                    f'<div class="check"><div class="box open">–</div>'
                    f'<div>{target}<small>Blocked: '
                    f'{_esc(entry.get("reason", ""))}</small>'
                    "</div></div>")
        rows = (f'<p class="hint" style="margin-top:8px">Last run: '
                f'{_esc(record.get("ran_at", ""))} · game version '
                f'{_esc(record.get("version", ""))}. Full record in '
                "EXPORT_REPORT.md in the project folder.</p>" + rows)
    boxes = ""
    for target in exports_module.TARGETS:
        boxes += (f'<label style="margin-right:14px">'
                  f'<input type="checkbox" name="target_{target}" '
                  f'checked> {_esc(target)}</label>')
    return f"""
<div class="card" style="margin-top:20px"><h3>Exports — local files</h3>
<p class="hint" style="margin-top:6px">Exports produce zip files in the project's exports/ folder. Nothing is uploaded anywhere — publishing to stores is not part of this release. Desktop builds are unsigned; signing and notarisation are your own step. Android and iOS are not exported (they need SDK and keystore setup).</p>
{rows}
<form method="post" action="/project/{quoted}/run-exports" style="margin-top:12px">
{boxes}
<button class="btn" type="submit">Run exports</button></form></div>"""


def render_project(server, name: str, notice: str = "",
                   notice_kind: str = "ok",
                   job_id: str | None = None) -> str:
    project = server.project_by_name(name)
    facts = _project_facts(project)
    state = facts["state"]
    path = project["path"]
    quoted = urllib.parse.quote(name)

    notice_html = _notice_html(notice, notice_kind)
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
        if isinstance(generation.get("plan"), dict):
            gen_plan = generation["plan"]
            plan_entities = ", ".join(
                f"{_esc(e.get('label', e.get('kind', '')))} "
                f"({_esc(e.get('behaviour', ''))})"
                for e in gen_plan.get("entities", []))
            game_line = f"""
<p style="margin-top:8px">Plan: <b>{_esc(gen_plan.get('movement', ''))} · {_esc(gen_plan.get('objective', ''))}</b>
<span class="hint">— fail: {_esc(', '.join(gen_plan.get('fail') or []) or 'none')}</span></p>
<p class="hint" style="margin-top:6px">Entities: {plan_entities}</p>"""
        else:
            game_line = f"""
<p style="margin-top:8px">Archetype: <b>{_esc(generation.get('archetype', ''))}</b>
<span class="hint">— {_esc(generation.get('archetype_reason', ''))}</span></p>"""
        generation_card = f"""
<div class="card" style="margin-top:20px"><h3>The game</h3>{game_line}
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
{_build_summary_card(server, project)}
{plan_card}
{generation_card}
{test_card}
{_exports_card(server, project, quoted, state)}
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
<table><tr><th>Asset</th><th>Source</th><th>Provider</th><th>Note</th></tr>{prov_rows}</table></div>
<div class="card dangerzone" style="margin-top:20px"><h3>Danger zone</h3>
<p class="hint" style="margin:6px 0 10px">Delete this project — permanently. The whole folder goes: the scope, the game, its git history, its exports and its saves. There is no trash folder and no undo.</p>
<form class="stack" method="post" action="/project/{quoted}/delete">
  <label for="confirm_name">Type the folder name <b>{_esc(name)}</b> to confirm</label>
  <input type="text" id="confirm_name" name="confirm_name" value="" autocomplete="off" placeholder="{_esc(name)}">
  <div class="actions"><button class="btn danger" type="submit">Delete this project</button></div>
</form></div>"""
    return _layout(facts["title"], "projects", body)


def render_feedback(server, name: str, notice: str = "",
                    notice_kind: str = "ok",
                    job_id: str | None = None) -> str:
    project = server.project_by_name(name)
    path = project["path"]
    quoted = urllib.parse.quote(name)
    store = feedback_module.load_store(path)
    items = feedback_module.ranked_queue(store)
    notice_html = _notice_html(notice, notice_kind)
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
            if item.get("category", "unknown") in (
                    "unknown", "request", "praise"):
                # Kinds with no automatic remedy (D8): say so on
                # the item, before the maker clicks.
                actions += (
                    '<div class="hint" style="margin-top:6px">'
                    'Shipping writes a plan for this one, not a '
                    "code change — this kind is the maker's "
                    'call.</div>')
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


def render_scope_form(server, name: str, notice: str = "",
                      notice_kind: str = "ok") -> str:
    """The scope as a form — the accessible fallback for the
    interview chat ('Edit as form'). Same file, same writers."""
    project = server.project_by_name(name)
    path = project["path"]
    parsed = scope_module.load_scope(scope_module.scope_path_for(path))
    quoted = urllib.parse.quote(name)
    notice_html = _notice_html(notice, notice_kind)

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
            chip = ('<span class="chip green specchip">✓ Specified</span>'
                    if fld.filled else
                    '<span class="chip warn specchip">Missing</span>')
            fields_html += (f'<label for="f_{_esc(fld.name)}">{_esc(fld.name)}'
                            f'{chip}</label>'
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
<div class="top"><div><h1>Scope form</h1><div class="sub">{_esc(name)} — every field of the scope file, editable directly. The interview chat fills this same file; use whichever suits you.</div></div>
<div><a class="btn ghost" href="/project/{quoted}/scope">Interview chat</a> <a class="btn ghost" href="/project/{quoted}">Back to project</a></div></div>
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
    return _layout("Scope form", "projects", body)


def _sample_cards_html(server, name: str, quoted: str,
                       samples_by_id: dict, turn: dict,
                       state: dict | None = None) -> str:
    """The style sample cards inside a chat turn: the generated
    scene, the palette swatches, and a Pick button per card —
    until a style is chosen. Once chosen, every rendering of the
    samples (including this old turn in the scrollback) locks:
    the chosen card shows '✓ Chosen' with a Change style button,
    and the other cards carry no pick controls at all (F5)."""
    state = state or {}
    chosen = state.get("style_chosen") or ""
    picks_open = (not chosen) or bool(state.get("style_reopen"))
    change_form = (
        f'<form class="action" method="post" '
        f'action="/project/{quoted}/scope/chat">'
        f'<input type="hidden" name="message" '
        f'value="{interview_module.CHANGE_STYLE_MESSAGE}">'
        '<button class="btn small ghost" type="submit">'
        "Change style</button></form>")
    cards = ""
    for sample_id in turn.get("samples", []):
        sample = samples_by_id.get(sample_id)
        if sample is None:
            continue
        swatches = "".join(
            f'<span class="swatch" title="{_esc(role)} {_esc(hexv)}" '
            f'style="background:{_esc(hexv)}"></span>'
            for role, hexv in (
                ("accent", sample["palette"].get("accent_hex", "")),
                ("target", sample["palette"].get("target_hex", "")),
                ("hazard", sample["palette"].get("hazard_hex", "")),
                ("platform", sample["palette"].get("platform_hex", "")),
                ("crate", sample["palette"].get("crate_hex", ""))))
        if picks_open:
            action = (
                f'<form class="action" method="post" '
                f'action="/project/{quoted}/scope/chat">'
                f'<input type="hidden" name="message" '
                f'value="{_esc(sample_id)}">'
                f'<button class="btn small" type="submit">'
                f'Pick {_esc(sample["name"])}</button></form>')
        elif sample_id == chosen:
            action = ('<span class="chip green">✓ Chosen</span>'
                      + change_form)
        else:
            action = '<span class="hint">Not chosen</span>'
        cards += f"""
<div class="samplecard">
  <img src="/project/{quoted}/style-sample/{_esc(sample_id)}" alt="{_esc(sample['name'])} style sample">
  <div class="samplemeta"><b>{_esc(sample['name'])}</b><span class="hint">{_esc(sample['blurb'])}</span>
  <div class="swatches">{swatches}</div>
  <p class="hint">{_esc(sample['palette_text'])}</p>
  {action}</div>
</div>"""
    if not cards:
        return ""
    note = ""
    if chosen == "custom" and not picks_open:
        note = ('<div style="grid-column:1/-1"><p class="hint">A style '
                "is already chosen (recorded in the scope) and "
                f"locked.</p>{change_form}</div>")
    return f'<div class="samples">{note}{cards}</div>'


def render_scope(server, name: str, notice: str = "",
                 notice_kind: str = "ok",
                 job_id: str | None = None) -> str:
    """The scope interview as a chat: the conversation on the
    left (the interview engine's thread, style samples included),
    the live scope panel on the right — fields filling as the
    conversation proceeds, the approve button enabling exactly
    when every REQUIRED field is filled."""
    project = server.project_by_name(name)
    path = project["path"]
    quoted = urllib.parse.quote(name)
    notice_html = _notice_html(notice, notice_kind)
    job_html = _job_card_for(server, job_id)
    state = interview_module.public_state(path)
    samples_by_id = {s["id"]: s for s in (state.get("samples") or [])}

    thread = ""
    for turn in state["turns"]:
        role = turn.get("role", "assistant")
        text = _esc(turn.get("text", ""))
        cards = ""
        if turn.get("kind") == "samples":
            cards = _sample_cards_html(server, name, quoted,
                                       samples_by_id, turn, state)
        thread += (f'<div class="msg {role}"><div class="bubble">'
                   f'<p class="msgtext">{text}</p>{cards}</div></div>')
    if not state["started"]:
        thread = ('<div class="msg assistant"><div class="bubble">'
                  '<p class="msgtext">No interview yet. Begin below '
                  'and I will ask the questions that scope this '
                  'game — your stored prompt, if you gave one, is '
                  'read first so nothing is asked twice.</p>'
                  '</div></div>')

    sections: dict[str, list] = {}
    order: list[str] = []
    for fld in state["fields"]:
        if fld["section"] not in sections:
            sections[fld["section"]] = []
            order.append(fld["section"])
        sections[fld["section"]].append(fld)
    panel_groups = ""
    for section in order:
        rows = ""
        for fld in sections[section]:
            if fld["filled"]:
                rows += (f'<div class="fieldrow done"><span class="dotok">✓</span>'
                         f'<div><b>{_esc(fld["name"])}</b>'
                         f'<div class="fieldval">{_esc(fld["value"])}</div></div>'
                         f'<span class="chip green specchip">✓ Specified</span></div>')
            else:
                rows += (f'<div class="fieldrow missing"><span class="dotmiss">·</span>'
                         f'<div><b>{_esc(fld["name"])}</b>'
                         f'<div class="fieldval hint">still missing</div></div>'
                         f'<span class="chip warn specchip">Missing</span></div>')
        panel_groups += (f'<h4 class="panelsection">{_esc(section)}</h4>'
                         f'{rows}')
    total = len(state["fields"])
    filled = total - state["missing_count"]
    approve_disabled = "" if state["complete"] else " disabled"
    approve_hint = ("" if state["complete"] else
                    '<span class="hint">Approve unlocks when every REQUIRED field is filled.</span>')
    approval_line = (f'<p class="hint" style="margin-top:8px">{_esc(state["approval"])}</p>'
                     if state["approved"] else "")
    if state["started"]:
        composer = f"""
  <form class="stack action composer" method="post" action="/project/{quoted}/scope/chat">
    <label for="message">Your answer</label>
    <textarea id="message" name="message" rows="3" placeholder="Answer in your own words — one answer per line, or 'Field: value'…"></textarea>
    <div class="actions"><button class="btn" type="submit">Send</button></div>
  </form>"""
    else:
        composer = f"""
  <form class="stack action composer" method="post" action="/project/{quoted}/scope/chat">
    <input type="hidden" name="message" value="">
    <div class="actions"><button class="btn" type="submit">Begin the interview</button></div>
  </form>"""

    body = f"""
<div class="top"><div><h1>Scope interview</h1><div class="sub">{_esc(name)} — a conversation that fills the scope file live. The build stays locked until every REQUIRED field is filled and the scope is approved.</div></div>
<div><a class="btn ghost" href="/project/{quoted}/scope/form">Edit as form</a> <a class="btn ghost" href="/project/{quoted}">Back to project</a></div></div>
{notice_html}
{job_html}
<div class="chatwrap">
  <div class="card chatthread">
    <div class="thread" id="chat-thread">{thread}</div>
    {composer}
  </div>
  <div class="card scopepanel">
    <h3>The scope so far</h3>
    <p class="hint" style="margin:6px 0 4px">Specified {filled} of {total} — {state['missing_count']} missing</p>
    <div class="bar"><i style="width:{(100 * filled // max(1, total))}%"></i></div>
    {panel_groups}
    <h4 class="panelsection">Approve</h4>
    <form class="stack" method="post" action="/project/{quoted}/scope/approve">
      <label for="by">Approved by (your name)</label>
      <input type="text" id="by" name="by" value="">
      <div class="actions"><button class="btn" type="submit"{approve_disabled}>Approve scope</button>{approve_hint}</div>
      {approval_line}
    </form>
  </div>
</div>"""
    return _layout("Scope interview", "projects", body,
                   app_class=" fitview")


SETTINGS_FILENAME = "global-settings.yaml"


def _project_settings_note(server) -> str:
    """One plain line when a project carries its own settings
    file: brain.resolve_settings prefers project files, so the
    maker's project file wins for that project — say so."""
    for project in find_projects(server.workspace):
        if (Path(project["path"]) / SETTINGS_FILENAME).exists():
            return (f"Note: project {project['name']} has its own "
                    "global-settings.yaml — a project file takes "
                    "precedence over this workspace file for that "
                    "project.")
    return ""


def _ladder_fields(data: dict) -> str:
    """The editable model ladder: one block per tier. The key
    field takes the NAME of an environment variable — never a
    key value — and shows whether that variable is set."""
    models = data.get("models", {})
    if not isinstance(models, dict):
        models = {}
    statuses = brain_module.brain_status(data)
    blocks = ""
    for tier in brain_module.TIERS:
        entry = models.get(tier, {})
        if not isinstance(entry, dict):
            entry = {}
        provider = str(entry.get("provider") or "")
        base_url = str(entry.get("base_url") or "")
        display = provider
        if provider == "openai" and brain_module._is_local(base_url):
            display = "local"
        options = ""
        for value, label in (("", "None — tier off"),
                             ("openai", "OpenAI"),
                             ("anthropic", "Anthropic"),
                             ("opencode", "OpenCode (your local server)"),
                             ("local", "Local server (no key needed)")):
            selected = " selected" if display == value else ""
            options += (f'<option value="{value}"{selected}>'
                        f"{label}</option>")
        key_env = str(entry.get("api_key_env") or "")
        st = statuses[tier]
        if provider == "opencode":
            # OpenCode holds its own provider credentials — there is
            # no key field for this provider, by design.
            key_block = ('<label>API key</label>'
                         '<p class="hint">OpenCode holds its own '
                         "provider credentials — Weaverbird never "
                         "asks for, stores, or sends a key for this "
                         "provider. The model is written the OpenCode "
                         "way: provider/model.</p>")
            model_placeholder = "provider/model — e.g. anthropic/claude-sonnet-4-5"
            url_placeholder = "http://127.0.0.1:4096"
        else:
            if key_env:
                key_state = "set" if st["key_present"] else "not set"
                key_hint = (f'<p class="hint">{_esc(key_env)} — {key_state} '
                            "in this app's environment.</p>")
            else:
                key_hint = ('<p class="hint">No key variable named — '
                            "hosted providers need one.</p>")
            key_block = (
                f'<label for="m_{tier}_api_key_env">API key — '
                'environment variable name</label>'
                f'<input type="text" id="m_{tier}_api_key_env" '
                f'name="m_{tier}_api_key_env" value="{_esc(key_env)}" '
                f'placeholder="OPENAI_API_KEY">\n      {key_hint}')
            model_placeholder = "model name"
            url_placeholder = "https://…"
        cost = entry.get("cost_per_1k_tokens")
        blocks += f"""
    <fieldset class="tier"><legend>{tier} tier —
      {'usable' if st['usable'] else 'not usable'}</legend>
      <label for="m_{tier}_provider">Provider</label>
      <select id="m_{tier}_provider" name="m_{tier}_provider">{options}</select>
      <label for="m_{tier}_model">Model</label>
      <input type="text" id="m_{tier}_model" name="m_{tier}_model" value="{_esc(str(entry.get('model') or ''))}" placeholder="{model_placeholder}">
      <label for="m_{tier}_base_url">Base URL <span class="hint">(blank = the provider's default)</span></label>
      <input type="text" id="m_{tier}_base_url" name="m_{tier}_base_url" value="{_esc(base_url)}" placeholder="{url_placeholder}">
      {key_block}
      <label for="m_{tier}_cost">Cost per 1k tokens <span class="hint">(your estimate, for the ledger)</span></label>
      <input type="number" step="0.0001" id="m_{tier}_cost" name="m_{tier}_cost_per_1k_tokens" value="{_esc('' if cost is None else cost)}">
    </fieldset>"""
    return blocks


def render_settings(server, notice: str = "", notice_kind: str = "ok") -> str:
    """The workspace settings page. The app edits the
    workspace-ROOT global-settings.yaml only; when there is no
    root file yet, one click creates it from the shipped
    template (the same file `init --with-settings` copies)."""
    root = Path(server.workspace) / SETTINGS_FILENAME
    notice_html = _notice_html(notice, notice_kind)
    note = _project_settings_note(server)
    note_html = _notice_html(note, "warn")
    if not root.exists():
        body = f"""
<div class="top"><div><h1>Settings</h1><div class="sub">One global settings file serves the whole factory: budgets, and the model ladder the brain climbs.</div></div></div>
{notice_html}
{note_html}
<div class="empty">No <code>global-settings.yaml</code> in the workspace root yet — so no budgets are set and no model tier is configured.
<form method="post" action="/settings/create" style="margin-top:14px"><button class="btn" type="submit">Create settings file</button></form>
<p class="hint" style="margin-top:12px">One click copies the template that ships with Weaverbird — the same file
<code>weaverbird init --with-settings</code> writes — into the workspace root. Then set the budgets and the model ladder here.</p></div>"""
        return _layout("Settings", "settings", body)
    _path, data = workspace_settings(server.workspace)
    if _path != root:
        # Only a project-level file resolved; the root file is the
        # one this page edits, so read it directly.
        try:
            data = yaml.safe_load(root.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError:
            data = {}
    data = data if isinstance(data, dict) else {}
    budgets = data.get("budgets", {}) if isinstance(data.get("budgets"), dict) else {}

    def bfield(name, step="1"):
        value = budgets.get(name)
        return (f'<label for="b_{name}">{name}</label>'
                f'<input type="number" step="{step}" id="b_{name}" name="{name}" '
                f'value="{_esc("" if value is None else value)}">')

    validation = settings_module.validate_settings(data)
    valid_line = ("Global settings are valid."
                  if validation.ok else
                  "Problems: " + "; ".join(
                      [f"missing {m}" for m in validation.missing_required]
                      + validation.errors))
    body = f"""
<div class="top"><div><h1>Settings</h1><div class="sub">{_esc(str(root))} — the workspace settings file. Key values are never typed or shown here — only the NAME of each key's environment variable, and whether it is set.</div></div></div>
{notice_html}
{note_html}
<div class="notice {'ok' if validation.ok else 'warn'}">{_esc(valid_line)}</div>
<form class="stack" method="post" action="/settings">
<div class="row" style="grid-template-columns:1fr 1fr;margin-top:0">
  <div class="q" style="margin-bottom:0"><h4>Budgets (global — never per game)</h4>
      {bfield("tokens_per_run")}
      {bfield("tokens_per_game")}
      {bfield("cost_ceiling_per_month", "0.01")}
      {bfield("max_build_iterations")}
      {bfield("warn_at_percent_of_cap")}
      <p class="hint">Caps are hard stops, in tokens. The factory never raises a cap to keep its own loop alive.</p>
  </div>
  <div class="q" style="margin-bottom:0"><h4>Model ladder (bring your own key)</h4>
    {_ladder_fields(data)}
    <p class="hint">A local server (Ollama, LM Studio) needs no key when its base URL is on this machine.</p>
  </div>
</div>
<div class="actions" style="margin-top:14px"><button class="btn" type="submit">Save settings</button></div>
<p class="hint">Saving rewrites this YAML file (comments in it are not kept). An invalid save is refused and the file is left exactly as it was.</p>
</form>"""
    return _layout("Settings", "settings", body)


def render_result(server, title: str, message: str, back_href: str,
                  kind: str = "ok") -> str:
    # The message is text and is escaped here, like every notice
    # (F2's rule: render escapes, callers pass text).
    body = f"""
<div class="top"><div><h1>{_esc(title)}</h1></div></div>
<div class="notice {kind}">{_esc(message)}</div>
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
    notice_html = _notice_html(notice, notice_kind)
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
    notice_html = _notice_html(notice, notice_kind)
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

# The only hostnames this app serves: it binds 127.0.0.1 and is a
# single-maker local tool, so a request naming any other host is a
# DNS-rebinding attempt, and a state-changing request carrying a
# foreign Origin/Referer (or Sec-Fetch-Site: cross-site) is a
# cross-site form attack (security findings F1/F7, 0.13.0). Local
# tooling that sends no Origin at all — curl, the CLI, scripts —
# is unaffected: the defence targets browser-originated requests.
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}

# The tightest policy that fits how the app is actually built:
# everything is served by the app itself (its own stylesheet and
# app.js — no inline scripts since 0.13.0), pages use inline style
# ATTRIBUTES throughout, and nothing may frame the app.
_CONTENT_SECURITY_POLICY = (
    "default-src 'self'; script-src 'self'; "
    "style-src 'self' 'unsafe-inline'; img-src 'self'; "
    "media-src 'self'; font-src 'self'; connect-src 'self'; "
    "frame-ancestors 'none'; base-uri 'none'; form-action 'self'")


class _Handler(BaseHTTPRequestHandler):
    # The banner names the tool only — never the version (F7).
    server_version = "Weaverbird"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    # -- plumbing ------------------------------------------------------
    def log_message(self, *args):  # keep the terminal quiet
        pass

    @property
    def app(self) -> "WeaverbirdAppServer":
        return self.server  # type: ignore[return-value]

    def _send_security_headers(self) -> None:
        self.send_header("Content-Security-Policy",
                         _CONTENT_SECURITY_POLICY)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")

    def _send_html(self, text: str, status: int = 200) -> None:
        data = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self._send_security_headers()
        self.end_headers()
        self.wfile.write(data)

    def _send_bytes(self, data: bytes, content_type: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self._send_security_headers()
        self.end_headers()
        self.wfile.write(data)

    def _send_json(self, payload, status: int = 200) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self._send_security_headers()
        self.end_headers()
        self.wfile.write(data)

    # -- request defences (F1) --------------------------------------
    def _bound_port(self) -> int:
        return int(self.server.server_address[1])

    def _authority_is_own(self) -> bool:
        """The Host header, when present, must name one of the
        loopback hosts on the port this server is bound to."""
        host = self.headers.get("Host")
        if host is None:
            return True  # non-browser tooling need not send one
        try:
            parsed = urllib.parse.urlparse(f"//{host}")
            port = parsed.port
        except ValueError:
            return False
        if (parsed.hostname or "").lower() not in _LOOPBACK_HOSTS:
            return False
        return port is None or port == self._bound_port()

    def _origin_is_own(self) -> bool:
        """A browser-originated request must come from this app's
        own origin. Requests with no Origin/Referer at all are
        local tooling (curl, the CLI) and pass."""
        if (self.headers.get("Sec-Fetch-Site") or "").strip().lower() \
                == "cross-site":
            return False
        for header in ("Origin", "Referer"):
            value = self.headers.get(header)
            if not value:
                continue
            try:
                parsed = urllib.parse.urlparse(value)
                port = parsed.port
            except ValueError:
                return False
            if (parsed.hostname or "").lower() not in _LOOPBACK_HOSTS:
                return False
            if port is not None and port != self._bound_port():
                return False
        return True

    def _request_allowed(self, *, state_changing: bool) -> bool:
        """Run the F1 defences; on refusal, answer 403 plainly and
        return False — nothing has been read or executed yet."""
        if not self._authority_is_own():
            self._fail_page(
                "This request names a host the Weaverbird app does "
                "not serve — it only answers on its own loopback "
                "address. Nothing was done.", 403)
            return False
        if state_changing and not self._origin_is_own():
            self._fail_page(
                "This request came from a different site, so the "
                "app did not act on it. Weaverbird only acts on "
                "requests from its own pages. Nothing was done.",
                403)
            return False
        return True

    def _form(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8", "replace")
        parsed = urllib.parse.parse_qs(raw, keep_blank_values=True)
        return {key: values[-1] for key, values in parsed.items()}

    def _fail_page(self, message: str, status: int = 400) -> None:
        self._send_html(render_result(self.app, "Something needs attention",
                                      message, "/"), status)

    # -- routing ---------------------------------------------------------
    def do_GET(self):  # noqa: N802 (stdlib handler name)
        if not self._request_allowed(state_changing=False):
            return
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
                deleted = (query.get("deleted") or [""])[0]
                notice = (f"Project {deleted} deleted — "
                          "permanently. Its folder, git history and "
                          "exports are gone."
                          if deleted else "")
                return self._send_html(render_dashboard(
                    self.app, notice=notice))
            if parts == ["new"]:
                return self._send_html(render_new_project(self.app))
            if parts == ["settings"]:
                created = (query.get("created") or [""])[0] == "1"
                return self._send_html(render_settings(
                    self.app,
                    notice=("Settings file created in the workspace "
                            "root — set the budgets and the model "
                            "ladder below, then save.")
                    if created else ""))
            if parts[0] == "project" and len(parts) >= 2:
                name = urllib.parse.unquote(parts[1])
                if len(parts) == 2:
                    return self._send_html(render_project(
                        self.app, name, job_id=job_id))
                if len(parts) == 3 and parts[2] == "scope":
                    return self._send_html(render_scope(
                        self.app, name, job_id=job_id))
                if len(parts) == 4 and parts[2] == "scope" \
                        and parts[3] == "form":
                    return self._send_html(render_scope_form(
                        self.app, name))
                if len(parts) == 4 and parts[2] == "scope" \
                        and parts[3] == "state":
                    project = self.app.project_by_name(name)
                    return self._send_json(interview_module.public_state(
                        project["path"]))
                if len(parts) == 4 and parts[2] == "style-sample":
                    return self._get_style_sample(
                        name, urllib.parse.unquote(parts[3]))
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
                if len(parts) == 4 and parts[2] == "export-file":
                    return self._get_export_file(
                        name, urllib.parse.unquote(parts[3]))
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

    def _get_export_file(self, name: str, filename: str) -> None:
        """Serve one export artifact, read-only, confined to the
        project's exports/ folder — the same traversal
        protection as the asset route; anything outside it (or
        missing) is a 404."""
        project = self.app.project_by_name(name)
        root = (project["path"] / exports_module.EXPORTS_DIR_NAME).resolve()
        candidate = (root / filename).resolve()
        if not candidate.is_relative_to(root) \
                or candidate.parent != root \
                or not candidate.is_file():
            return self._fail_page("That export does not exist here.", 404)
        return self._send_bytes(candidate.read_bytes(),
                                "application/zip")

    def _get_style_sample(self, name: str, sample_id: str) -> None:
        """Serve one generated style sample card, confined to the
        project's .weaverbird/style_samples/ folder and to the
        known sample ids."""
        project = self.app.project_by_name(name)
        known = {s["id"] for s in assets_module.style_samples()}
        if sample_id not in known:
            return self._fail_page("That style sample does not exist.", 404)
        candidate = (project["path"] / interview_module.SAMPLES_DIR_REL
                     / f"{sample_id}.svg").resolve()
        root = (project["path"] / interview_module.SAMPLES_DIR_REL).resolve()
        if not candidate.is_relative_to(root) or not candidate.is_file():
            return self._fail_page("That style sample does not exist.", 404)
        return self._send_bytes(candidate.read_bytes(), "image/svg+xml")

    def do_POST(self):  # noqa: N802
        if not self._request_allowed(state_changing=True):
            return
        parsed = urllib.parse.urlparse(self.path)
        parts = [p for p in parsed.path.split("/") if p]
        try:
            if parts == ["new"]:
                return self._post_new_project()
            if parts == ["demo"]:
                return self._post_demo()
            if parts == ["settings"]:
                return self._post_settings()
            if parts == ["settings", "create"]:
                return self._post_settings_create()
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
                # Redirect-after-POST, like every job action: the
                # job page lives at /jobs/<id>; rendering it here
                # would strand a refresh on this POST-only URL.
                return self._redirect(
                    f"/jobs/{urllib.parse.quote(job_id)}")
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
                if action == "run-exports":
                    return self._post_run_exports(name)
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
                if action == "delete":
                    return self._post_delete_project(name)
                if action == "scope" and len(parts) == 3:
                    return self._post_scope_save(name)
                if action == "scope" and len(parts) == 4 and parts[3] == "chat":
                    return self._post_scope_chat(name)
                if action == "scope" and len(parts) == 4 and parts[3] == "extract":
                    return self._post_scope_extract(name)
                if action == "scope" and len(parts) == 4 and parts[3] == "approve":
                    return self._post_scope_approve(name)
            return self._fail_page("That action does not exist here.", 404)
        except AppError as exc:
            return self._fail_page(str(exc), 404)
        except Exception as exc:
            return self._fail_page(f"{type(exc).__name__}: {exc}", 500)

    def _post_new_project(self) -> None:
        """Create a project from the dashboard's New project form.

        Validation failures re-render the form with the reason in
        plain language (projects.new_project validates before it
        touches the disk, so a refused creation leaves nothing
        behind). Success is a 303: into the interview when a
        starting prompt was given — it is consumed there first —
        and to the project page otherwise."""
        form = self._form()
        values = {key: form.get(key, "")
                  for key in ("name", "folder", "prompt")}
        try:
            created = projects_module.new_project(
                self.app.workspace, values["name"],
                folder=values["folder"], prompt=values["prompt"])
        except projects_module.ProjectError as exc:
            return self._send_html(render_new_project(
                self.app, notice=f"Not created. {exc}",
                notice_kind="bad", values=values))
        except Exception as exc:  # a half-made folder is already removed
            return self._send_html(render_new_project(
                self.app,
                notice=f"Not created. {type(exc).__name__}: {exc}",
                notice_kind="bad", values=values))
        quoted = urllib.parse.quote(created["name"])
        if values["prompt"].strip():
            return self._redirect(f"/project/{quoted}/scope")
        return self._redirect(f"/project/{quoted}")

    def _post_demo(self) -> None:
        """Install the bundled demo project into the workspace
        and land on its project page (303, like every POST that
        leads to a page). An existing folder of the demo's name
        is never overwritten: when it holds a scope — the demo
        already opened, or a project of the maker's own with
        that name — we land on it instead."""
        try:
            installed = demo_module.install_demo(self.app.workspace)
        except demo_module.DemoError as exc:
            existing = self.app.workspace / demo_module.DEMO_NAME
            if (existing / "SCOPE.md").is_file():
                return self._redirect(
                    f"/project/{demo_module.DEMO_NAME}")
            return self._fail_page(str(exc), 400)
        return self._redirect(
            f"/project/{urllib.parse.quote(installed['name'])}")

    def _post_delete_project(self, name: str) -> None:
        """The Danger zone: permanently delete the project folder.

        All guardrails live in projects.delete_project (target
        inside the workspace, a real project, the folder name
        typed exactly, no job running); a refusal re-renders the
        project page with the reason and deletes nothing. Success
        is a 303 to the dashboard, which no longer lists it."""
        project = self.app.project_by_name(name)
        form = self._form()
        try:
            deleted = projects_module.delete_project(
                self.app.workspace, project["name"],
                form.get("confirm_name", ""))
        except projects_module.ProjectError as exc:
            return self._send_html(render_project(
                self.app, name,
                notice=f"Not deleted. {exc}",
                notice_kind="bad"))
        return self._redirect(
            f"/?deleted={urllib.parse.quote(deleted['name'])}")

    # -- job-backed actions ------------------------------------------------
    def _redirect(self, location: str) -> None:
        """A 303 redirect with an empty body (redirect-after-POST)."""
        self.send_response(303)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self._send_security_headers()
        self.end_headers()

    def _post_job_action(self, name: str, action: str,
                         params: dict | None = None,
                         view: str = "project") -> None:
        """Every long project action is a job now: submit it, then
        answer with a 303 redirect to the GET page the button lives
        on, carrying ?job=<id>. The browser must end up at a GET
        URL: the job card's reload re-GETs the current URL when the
        job lands, so rendering the page here instead would strand
        that reload on this POST-only URL, which answers GET with
        a 404."""
        project = self.app.project_by_name(name)
        job = self.app.jobs.submit(project["name"], project["path"],
                                   action, params=params)
        quoted = urllib.parse.quote(name)
        page = {
            "feedback": f"/project/{quoted}/feedback",
            "balance": f"/project/{quoted}/balance",
            "assets": f"/project/{quoted}/assets",
        }.get(view, f"/project/{quoted}")
        return self._redirect(f"{page}?job={job['id']}")

    def _post_run_exports(self, name: str) -> None:
        form = self._form()
        targets = [t for t in exports_module.TARGETS
                   if form.get(f"target_{t}")]
        if not targets:
            return self._fail_page(
                "Pick at least one export target.", 400)
        return self._post_job_action(name, "run-exports",
                                     params={"targets": targets})

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
                notice=f"Palette not saved. {exc}",
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
                notice=(f"{'Added' if created else 'Already on record'}: "
                        f"[{item['id']}] {item['text']}"),
                notice_kind="ok"))
        if len(parts) == 5 and parts[4] in ("approve", "dismiss"):
            try:
                item = feedback_module.set_status(
                    path, parts[3],
                    "approved" if parts[4] == "approve" else "dismissed")
            except feedback_module.FeedbackError as exc:
                return self._send_html(render_feedback(
                    self.app, name, notice=str(exc), notice_kind="bad"))
            return self._send_html(render_feedback(
                self.app, name,
                notice=f"[{item['id']}] is now {item['status']}.",
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
                self.app, name, notice=f"Not advanced. {exc}",
                notice_kind="warn"))
        return self._send_html(render_project(
            self.app, name,
            notice=f"Advanced. The pipeline is now at stage: {state['stage']}.",
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
        return self._send_html(render_scope_form(
            self.app, name, notice=note + " The scope file has been updated.",
            notice_kind="ok"))

    def _post_scope_chat(self, name: str) -> None:
        """One interview turn, as a job (worker: scope): the
        engine may call the maker's brain, and the chat page's
        live job card shows the turn landing. Answered with a
        redirect to the chat page itself (carrying ?job=): the
        browser must sit at the page's own URL, because the job
        card's reload re-GETs the current URL — rendering here
        instead would strand that reload on this POST-only URL,
        which answers GET with a 404."""
        form = self._form()
        message = form.get("message", "")
        project = self.app.project_by_name(name)
        job = self.app.jobs.submit(project["name"], project["path"],
                                   "interview-turn",
                                   params={"message": message})
        quoted = urllib.parse.quote(name)
        return self._redirect(f"/project/{quoted}/scope?job={job['id']}")

    def _post_scope_extract(self, name: str) -> None:
        project = self.app.project_by_name(name)
        form = self._form()
        prompt = form.get("prompt", "")
        if not prompt.strip():
            return self._send_html(render_scope_form(
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
        return self._send_html(render_scope_form(
            self.app, name, notice=" ".join(bits),
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
                self.app, name, notice=str(exc), notice_kind="bad"))
        return self._send_html(render_scope(
            self.app, name,
            notice=f"Scope approved and frozen. {parsed.approval}",
            notice_kind="ok"))

    def _post_settings_create(self) -> None:
        """One click: write the shipped template to the workspace
        root — byte-identical to what `init --with-settings`
        copies, from the same template source — then land on the
        editable form."""
        root = Path(self.app.workspace) / SETTINGS_FILENAME
        if not root.exists():
            root.write_text(template_text(SETTINGS_FILENAME),
                            encoding="utf-8")
        return self._redirect("/settings?created=1")

    def _post_settings(self) -> None:
        """Save budgets AND the model ladder to the workspace-root
        settings file. Only whitelisted field names are read from
        the form — a key VALUE posted under any other name is
        ignored, never stored. Validation runs before any write:
        an invalid save leaves the file byte-identical."""
        root = Path(self.app.workspace) / SETTINGS_FILENAME
        if not root.exists():
            return self._fail_page(
                "No global-settings.yaml exists in the workspace root "
                "yet — create one from the Settings page first.")
        form = self._form()
        try:
            data = yaml.safe_load(root.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError:
            data = {}
        if not isinstance(data, dict):
            data = {}
        data = dict(data)
        budgets = dict(data.get("budgets", {})
                       if isinstance(data.get("budgets"), dict) else {})
        numeric = {"tokens_per_run": int, "tokens_per_game": int,
                   "max_build_iterations": int, "warn_at_percent_of_cap": int,
                   "cost_ceiling_per_month": float}
        errors = []
        for field_name, convert in numeric.items():
            if field_name not in form:
                continue
            raw = form.get(field_name, "")
            if raw is None or str(raw).strip() == "":
                budgets[field_name] = None
                continue
            try:
                budgets[field_name] = convert(str(raw).strip())
            except ValueError:
                errors.append(f"{field_name} must be a number.")
        data["budgets"] = budgets

        models = data.get("models")
        models = dict(models) if isinstance(models, dict) else {}
        for tier in brain_module.TIERS:
            prefix = f"m_{tier}_"
            if not any(key.startswith(prefix) for key in form):
                continue  # this POST did not carry the ladder at all
            entry = models.get(tier)
            entry = dict(entry) if isinstance(entry, dict) else {}
            provider = str(form.get(prefix + "provider", "")).strip()
            model = str(form.get(prefix + "model", "")).strip()
            base_url = str(form.get(prefix + "base_url", "")).strip()
            key_env = str(form.get(prefix + "api_key_env", "")).strip()
            if key_env and not re.fullmatch(
                    r"[A-Za-z_][A-Za-z0-9_]*", key_env):
                errors.append(
                    f"{tier} tier: the key field takes the NAME of an "
                    "environment variable (letters, digits, "
                    "underscores) — never the key itself.")
            if provider == "":
                entry["model"] = ""
            elif provider == "local":
                entry["provider"] = "openai"
                entry["model"] = model
                entry["base_url"] = base_url or "http://localhost:11434/v1"
                entry["api_key_env"] = key_env
            elif provider == "opencode":
                # OpenCode holds its own credentials: any key-env
                # name is dropped on save, never stored for this
                # provider.
                entry["provider"] = "opencode"
                entry["model"] = model
                entry["base_url"] = base_url
                entry["api_key_env"] = ""
            elif provider in brain_module.PROVIDERS:
                entry["provider"] = provider
                entry["model"] = model
                entry["base_url"] = base_url
                entry["api_key_env"] = key_env
            else:
                errors.append(f"{tier} tier: unknown provider "
                              f"{provider!r}.")
            cost_raw = str(form.get(prefix + "cost_per_1k_tokens", "")
                           ).strip()
            if cost_raw:
                try:
                    entry["cost_per_1k_tokens"] = float(cost_raw)
                except ValueError:
                    errors.append(f"{tier} tier: the cost per 1k "
                                  "tokens must be a number.")
            models[tier] = entry
        if models:
            data["models"] = models
        if errors:
            return self._send_html(render_settings(
                self.app, notice="Not saved: " + "; ".join(errors),
                notice_kind="bad"))
        validation = settings_module.validate_settings(data)
        if not validation.ok:
            problems = [f"missing {m}" for m in validation.missing_required] \
                + validation.errors
            return self._send_html(render_settings(
                self.app,
                notice="Not saved — the settings would be invalid: "
                       + "; ".join(problems),
                notice_kind="bad"))
        root.write_text(yaml.safe_dump(data, sort_keys=False),
                        encoding="utf-8")
        return self._send_html(render_settings(
            self.app, notice="Settings saved to the workspace global "
                             "settings file.",
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
