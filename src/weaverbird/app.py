"""The Weaverbird app — a local web front end over the same core.

Bare ``weaverbird`` starts this app (workspace = the current folder;
``--workspace``, ``--port`` and ``--no-open`` adjust the launch). It
is bound to 127.0.0.1 only. It has no authentication because it never leaves the
machine it runs on: it is a local tool, like the CLI, drawn as
screens. Everything it shows comes from the library (scope, state,
ledger, provenance, brain, finish, skeleton) — the app holds no
logic of its own, so the CLI and the app can never drift apart.

Screens: a dashboard of the workspace's projects, a project view
(pipeline stepper, definition of done with its evidence, ledger,
provenance, and the actions: build skeleton, finish check, advance),
the scope interview as a form (with prompt extraction when a brain
is configured), and a settings view (budgets editable, the rest —
including which key environment variables are set, never their
values — read-only).

All HTML/CSS/JS is package data in ``weaverbird/web/``, styled from
the design tokens to match docs/visual-prototype/.
"""

from __future__ import annotations

import html
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path

import yaml

from . import __version__
from . import assets_gen as assets_module
from . import brain as brain_module
from . import finish as finish_module
from . import generate as generate_module
from . import testing as testing_module
from . import godot as godot_module
from . import ledger as ledger_module
from . import provenance as provenance_module
from . import scope as scope_module
from . import settings as settings_module
from . import skeleton as skeleton_module
from . import state as state_module
from . import vcs as vcs_module

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


def render_project(server, name: str, notice: str = "",
                   notice_kind: str = "ok") -> str:
    project = server.project_by_name(name)
    facts = _project_facts(project)
    state = facts["state"]
    path = project["path"]
    quoted = urllib.parse.quote(name)

    notice_html = (f'<div class="notice {notice_kind}">{notice}</div>'
                   if notice else "")

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
        test_card = f"""
<div class="card" style="margin-top:20px"><h3>Test summary</h3>
<p class="hint" style="margin:6px 0 10px">Verdict: <b>{'PASS' if test_report.get('ok') else 'FAIL'}</b> ·
Win proven: {'yes' if test_report.get('win_proven') else 'no'} · Lose proven: {'yes' if test_report.get('lose_proven') else 'no'} ·
Performance: {_esc(perf.get('avg_fps', '?'))} avg FPS (target {_esc(perf.get('target_fps', '?'))}), scene load {_esc(perf.get('load_time_s', '?'))}s ·
Scripted players (deterministic bots — model playtesting is future work)</p>
{comp_line}{player_line("Breaker", players.get("breaker", {}))}{player_line("First-time player", players.get("first_time", {}))}</div>"""

    reasons = state_module.gate_reasons(path, state) if state else []
    gate_html = ""
    if reasons:
        gate_html = ('<p class="hint" style="margin-top:10px">Advance is '
                     'blocked: ' + _esc(" ".join(reasons)) + "</p>")

    body = f"""
<div class="top"><div><h1>{_esc(facts['title'])}</h1><div class="sub">{_esc(facts['pitch'] or 'No pitch in the scope yet.')}</div></div>
<div><a class="btn ghost" href="/project/{quoted}/scope">Scope interview</a></div></div>
{notice_html}
{_stepper(facts['stage'])}
<div class="actions">
  {skeleton_button}
  {stage_buttons}
  <form class="action" method="post" action="/project/{quoted}/finish-check"><button class="btn ghost" type="submit">Run finish check</button></form>
  <form class="action" method="post" action="/project/{quoted}/advance"><button class="btn ghost" type="submit">Advance pipeline</button></form>
  <form method="post" action="/project/{quoted}/finish-report"><button class="btn ghost" type="submit">Write finish report</button></form>
</div>
{gate_html}
{generation_card}
{test_card}
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
        try:
            if parsed.path == "/style.css":
                return self._send_bytes(_web_text("style.css").encode(), "text/css")
            if parsed.path == "/app.js":
                return self._send_bytes(_web_text("app.js").encode(),
                                        "text/javascript")
            if parsed.path == "/mark.svg":
                return self._send_bytes(_asset_bytes("weaverbird.svg"),
                                        "image/svg+xml")
            if not parts:
                return self._send_html(render_dashboard(self.app))
            if parts == ["settings"]:
                return self._send_html(render_settings(self.app))
            if parts[0] == "project" and len(parts) >= 2:
                name = urllib.parse.unquote(parts[1])
                if len(parts) == 2:
                    return self._send_html(render_project(self.app, name))
                if len(parts) == 3 and parts[2] == "scope":
                    return self._send_html(render_scope(self.app, name))
            return self._fail_page("That page does not exist here.", 404)
        except AppError as exc:
            return self._fail_page(str(exc), 404)
        except Exception as exc:  # a page must fail plainly, never hang
            return self._fail_page(f"{type(exc).__name__}: {exc}", 500)

    def do_POST(self):  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        parts = [p for p in parsed.path.split("/") if p]
        try:
            if parts == ["settings"]:
                return self._post_settings()
            if parts[0] == "project" and len(parts) >= 3:
                name = urllib.parse.unquote(parts[1])
                action = parts[2]
                if action == "build-skeleton":
                    return self._post_build_skeleton(name)
                if action == "generate-assets":
                    return self._post_generate_assets(name)
                if action == "build-game":
                    return self._post_build_game(name)
                if action == "run-tests":
                    return self._post_run_tests(name)
                if action == "finish-check":
                    return self._post_finish_check(name)
                if action == "finish-report":
                    return self._post_finish_report(name)
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

    # -- actions ---------------------------------------------------------
    def _post_build_skeleton(self, name: str) -> None:
        project = self.app.project_by_name(name)
        back = f"/project/{urllib.parse.quote(name)}"
        try:
            report = skeleton_module.build_skeleton(project["path"])
        except skeleton_module.SkeletonError as exc:
            return self._send_html(render_project(
                self.app, name, notice=_esc(f"Skeleton not built. {exc}"),
                notice_kind="bad"))
        validation = report.get("validation", {})
        message = (f"Skeleton built and validated in Godot "
                   f"({validation.get('godot_version', '')}). "
                   f"Known-good tag: {_esc(report.get('known_good_tag'))}.")
        return self._send_html(render_project(
            self.app, name, notice=message, notice_kind="ok"))

    def _post_generate_assets(self, name: str) -> None:
        project = self.app.project_by_name(name)
        try:
            report = assets_module.generate_assets(project["path"])
        except assets_module.AssetsError as exc:
            return self._send_html(render_project(
                self.app, name, notice=_esc(f"Assets not generated. {exc}"),
                notice_kind="bad"))
        return self._send_html(render_project(
            self.app, name,
            notice=f"Assets generated: {len(report['files'])} files for the "
                   f"{_esc(report['archetype'])} game, every one recorded "
                   "in the provenance log. Cost: 0 tokens.",
            notice_kind="ok"))

    def _post_build_game(self, name: str) -> None:
        project = self.app.project_by_name(name)
        settings_path, _ = workspace_settings(self.app.workspace)
        try:
            report = generate_module.build_game(
                project["path"], settings_path=settings_path)
        except generate_module.GenerationError as exc:
            return self._send_html(render_project(
                self.app, name, notice=_esc(f"Game not built. {exc}"),
                notice_kind="bad"))
        return self._send_html(render_project(
            self.app, name,
            notice=f"Game built: archetype {_esc(report['archetype'])}, "
                   f"{report['units_delivered']} content units, Godot "
                   "validation passed, tagged known-good/build.",
            notice_kind="ok"))

    def _post_run_tests(self, name: str) -> None:
        project = self.app.project_by_name(name)
        try:
            report = testing_module.run_tests(project["path"])
        except testing_module.TestingError as exc:
            return self._send_html(render_project(
                self.app, name, notice=_esc(f"Tests not run. {exc}"),
                notice_kind="bad"))
        digest = " ".join(testing_module.digest_lines(report)[-3:])
        kind = "ok" if report.get("ok") else "bad"
        return self._send_html(render_project(
            self.app, name,
            notice=f"Test suite finished. {_esc(digest)}",
            notice_kind=kind))

    def _post_finish_check(self, name: str) -> None:
        project = self.app.project_by_name(name)
        try:
            result = finish_module.evaluate_finish(project["path"])
        except finish_module.FinishError as exc:
            return self._send_html(render_project(
                self.app, name, notice=_esc(str(exc)), notice_kind="bad"))
        bits = []
        if result["marked_now"]:
            bits.append("Marked now: " + ", ".join(
                finish_module.DOD_LABELS.get(k, k) for k in result["marked_now"]))
        if result["already_marked"]:
            bits.append("Already marked: " + ", ".join(
                finish_module.DOD_LABELS.get(k, k) for k in result["already_marked"]))
        if result["open"]:
            bits.append("Still open: " + "; ".join(
                f"{finish_module.DOD_LABELS.get(k, k)} — {reason}"
                for k, reason in result["open"].items()))
        message = "Finish check done. " + (" ".join(bits) if bits else "")
        kind = "ok" if not result["open"] else "warn"
        return self._send_html(render_project(
            self.app, name, notice=_esc(message), notice_kind=kind))

    def _post_finish_report(self, name: str) -> None:
        project = self.app.project_by_name(name)
        settings_path, _ = workspace_settings(self.app.workspace)
        try:
            path = finish_module.build_finish_report(
                project["path"], settings_path=settings_path)
        except finish_module.FinishError as exc:
            return self._send_html(render_project(
                self.app, name, notice=_esc(str(exc)), notice_kind="bad"))
        return self._send_html(render_project(
            self.app, name,
            notice=f"Finish report written to {_esc(path.name)} in the project folder.",
            notice_kind="ok"))

    def _post_advance(self, name: str) -> None:
        project = self.app.project_by_name(name)
        skeleton_module.register_stage_handlers()
        assets_module.register_stage_handlers()
        generate_module.register_stage_handlers()
        testing_module.register_stage_handlers()
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
        super().__init__(("127.0.0.1", port), _Handler)

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
