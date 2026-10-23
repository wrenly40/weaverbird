"""The Weaverbird command-line front end.

This module is deliberately thin: it parses arguments, calls the
library in this package, and prints plain results. All of the logic
lives in the library modules (settings, scope, connectors, ledger,
provenance, state, vcs, godot, skeleton, brain, finish, update), so
the app (bare ``weaverbird``) and any future job runner call the
same core. The everyday CLI surface is deliberately minimal — bare
launch, ``update``, ``version``, ``help``; the project commands stay
underneath for scripting.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import sys
from pathlib import Path

from . import __version__, template_text
from . import app as app_module
from . import assets_gen as assets_module
from . import balance as balance_module
from . import brain as brain_module
from . import generate as generate_module
from . import jobs as jobs_module
from . import testing as testing_module
from . import update as update_module
from . import connectors as connectors_module
from . import feedback as feedback_module
from . import finish as finish_module
from . import godot as godot_module
from . import identity as identity_module
from . import interview as interview_module
from . import ledger as ledger_module
from . import modelplayer as modelplayer_module
from . import patch as patch_module
from . import provenance as provenance_module
from . import repair as repair_module
from . import scope as scope_module
from . import settings as settings_module
from . import orchestrator as orchestrator_module
from . import projects as projects_module
from . import skeleton as skeleton_module
from . import state as state_module
from . import vcs as vcs_module
from . import workers as workers_module


# --------------------------------------------------------------------------
# helpers

def _ok(message: str) -> int:
    print(message)
    return 0


def _fail(message: str) -> int:
    print(message)
    return 1


def _scope_file(project) -> Path:
    return scope_module.scope_path_for(project)


def _settings_candidates() -> list[Path]:
    candidates = []
    env_value = os.environ.get("WEAVERBIRD_SETTINGS")
    if env_value:
        candidates.append(Path(env_value))
    candidates.append(Path.cwd() / "global-settings.yaml")
    candidates.append(Path.home() / ".weaverbird" / "global-settings.yaml")
    return candidates


# --------------------------------------------------------------------------
# commands

def cmd_doctor(_args) -> int:
    print(f"Weaverbird {__version__} — checks")
    print(f"✓ Python          {platform.python_version()}")
    git = shutil.which("git")
    if git:
        print(f"✓ Git             {git}")
    else:
        print("✗ Git             not found — needed for versioning and rollback")
    status = vcs_module.github_status()
    if status["available"] and status["logged_in"]:
        print(f"✓ GitHub CLI      logged in as {status['account']}")
    elif status["available"]:
        print("– GitHub CLI      installed but not logged in (optional — local git still works)")
    else:
        print("– GitHub CLI      not found (optional — local git still works)")
    godot_path, godot_version = godot_module.find_godot()
    if godot_path:
        version = f" ({godot_version})" if godot_version else ""
        print(f"✓ Godot           {godot_path}{version}")
    else:
        print("✗ Godot           not found — needed to build and test games; "
              "set WEAVERBIRD_GODOT to its path")
    found_settings = [p for p in _settings_candidates() if p.exists()]
    if found_settings:
        print(f"✓ Global settings {found_settings[0]}")
    else:
        print("✗ Global settings no file found yet — start one with "
              "'weaverbird init <dir> --with-settings' and fill in the budgets")
    return 0


def cmd_identity(args) -> int:
    if getattr(args, "export", None):
        written = identity_module.export_assets(args.export)
        print(f"Exported the Weaverbird identity to {args.export}:")
        for path in written:
            print(f"- {path.name}")
        return 0
    print(identity_module.summary_text(), end="")
    return 0


def cmd_demo(args) -> int:
    from . import demo as demo_module
    workspace = Path(args.workspace) if args.workspace else Path.cwd()
    try:
        installed = demo_module.install_demo(workspace, name=args.name)
    except demo_module.DemoError as exc:
        return _fail(str(exc))
    print(f"Installed the demo project: {installed['files']} files "
          f"copied to {installed['path']} — a finished game (arena "
          "collector, 3 rounds) with its scope, test report and "
          "finish report, at the finished stage.")
    print("Look around in the app, re-run its tests, or export it "
          f"with 'weaverbird export {installed['path']}'.")
    return 0


def cmd_export(args) -> int:
    from . import exports as exports_module
    from . import workers as workers_module
    targets = None
    if args.targets:
        targets = [t.strip() for t in args.targets.split(",")
                   if t.strip()]
        unknown = [t for t in targets if t not in exports_module.TARGETS]
        if unknown:
            return _fail(f"Unknown target(s): {', '.join(unknown)}. "
                         f"Known: {', '.join(exports_module.TARGETS)}.")
    try:
        outcome = workers_module.run_worker(
            "export", Path(args.dir), params={"targets": targets},
            reporter=lambda text, done=None, total=None: print(text))
    except workers_module.WorkerError as exc:
        return _fail(str(exc))
    if not outcome["ok"]:
        return _fail(outcome["summary"])
    report = exports_module.load_record(args.dir) or {}
    for entry in report.get("targets", []):
        if entry.get("status") == "exported":
            print(f"✓ {entry['target']}: {entry['artifact']} "
                  f"({entry['size']} bytes)")
        else:
            print(f"– {entry['target']}: blocked — {entry['reason']}")
    print(f"Report: {Path(args.dir) / exports_module.REPORT_NAME}")
    exported = [e for e in report.get("targets", [])
                if e.get("status") == "exported"]
    return 0 if exported else 1


def cmd_init(args) -> int:
    target = Path(args.dir)
    try:
        created = projects_module.create_project(
            target, with_settings=args.with_settings, force=args.force)
    except projects_module.ProjectError as exc:
        return _fail(str(exc))

    print(f"Created a new Weaverbird project in {target}:")
    for item in created:
        print(f"- {item}")
    print("\nNext step: fill in the REQUIRED fields in SCOPE.md, then run "
          f"'weaverbird scope check {target}'.")
    return 0


def cmd_scope_check(args) -> int:
    try:
        parsed = scope_module.load_scope(_scope_file(args.dir))
    except scope_module.ScopeError as exc:
        return _fail(str(exc))
    print(f"Scope file: {_scope_file(args.dir)}")
    print(f"Required fields: {len(parsed.fields)} — "
          f"filled {len(parsed.filled_fields)}, missing {len(parsed.missing_fields)}")
    if parsed.filled_fields:
        print("Filled:")
        for fld in parsed.filled_fields:
            print(f"- {fld.name}: {fld.value}")
    if parsed.missing_fields:
        print("Still missing (REQUIRED):")
        for fld in parsed.missing_fields:
            print(f"- {fld.name} [{fld.section}]")
        print("\nThe build cannot start until every REQUIRED field is filled.")
        print(f"Next step: weaverbird scope questions {args.dir}")
        return 1
    print("Approved: " + ("yes" if parsed.is_approved else "no — the scope is complete and ready to approve"))
    return _ok("The scope is complete.")


def cmd_scope_questions(args) -> int:
    try:
        parsed = scope_module.load_scope(_scope_file(args.dir))
    except scope_module.ScopeError as exc:
        return _fail(str(exc))
    groups = scope_module.interview_questions(parsed)
    if not groups:
        return _ok("No questions — every REQUIRED field in the scope is filled.")
    print("Next interview round — please answer these, grouped by section:")
    for section, questions in groups:
        print(f"\n{section}:")
        for question in questions:
            print(f"- {question}")
    return 0


def cmd_scope_approve(args) -> int:
    try:
        parsed = scope_module.approve_scope(_scope_file(args.dir), by=args.by)
    except scope_module.ScopeIncompleteError as exc:
        print(str(exc))
        print("Run 'weaverbird scope questions' for the next interview round.")
        return 1
    except scope_module.ScopeError as exc:
        return _fail(str(exc))
    return _ok(f"Scope approved by {args.by} and frozen. Approval line: {parsed.approval}")


def cmd_scope_interview(args) -> int:
    try:
        outcome = brain_module.interview_from_prompt(
            args.dir, args.prompt, settings_path=args.settings)
    except scope_module.ScopeError as exc:
        return _fail(str(exc))
    print("Prompt stored in the scope file's Original prompt field.")
    if outcome["applied"]:
        print("Extracted from the prompt (only empty fields were filled):")
        for name, value in outcome["applied"].items():
            print(f"- {name}: {value}")
    result = outcome.get("brain_result")
    if result is not None and result.ok:
        print(f"Model call logged to the ledger: stage scope, tier cheap, "
              f"{result.total_tokens} tokens ({result.prompt_tokens} in, "
              f"{result.completion_tokens} out), cost {result.cost:.4f}.")
    if outcome.get("note"):
        print(outcome["note"])
    questions = outcome.get("questions") or []
    if questions:
        print(f"\nStill missing {outcome['missing_count']} required field(s). "
              "Next interview round:")
        for section, group in questions:
            print(f"\n{section}:")
            for question in group:
                print(f"- {question}")
        return 0
    return _ok("The scope is now complete — ready to approve.")


def _print_interview_turn(turn) -> None:
    text = turn.get("text", "")
    if text:
        print(text)


def cmd_interview(args) -> int:
    """`weaverbird interview <dir>` — the scope interview as a
    conversation, line by line. The same engine the app chat
    uses: answers are read from stdin (one message per line;
    piped input works, which is how the tests drive it), each
    captured field is read back, and the interview ends when the
    scope is complete or the input does."""
    try:
        state = interview_module.start(args.dir,
                                       settings_path=args.settings)
    except interview_module.InterviewError as exc:
        return _fail(str(exc))
    printed = 0
    for turn in state["turns"]:
        if turn.get("role") == "assistant":
            _print_interview_turn(turn)
            printed += 1
    while not state["complete"]:
        try:
            line = input()
        except EOFError:
            print(f"\nInterview paused — {state['missing_count']} "
                  "required field(s) still missing. Run the command "
                  "again (or open the app) to continue; nothing "
                  "already answered is asked twice.")
            return 0
        except KeyboardInterrupt:
            print()
            return 0
        try:
            state = interview_module.respond(
                args.dir, line, settings_path=args.settings)
        except interview_module.InterviewError as exc:
            return _fail(str(exc))
        turns = state["turns"]
        seen = 0
        for turn in turns:
            if turn.get("role") != "assistant":
                continue
            seen += 1
            if seen > printed:
                _print_interview_turn(turn)
        printed = seen
    print("\nThe scope is complete — approve it with "
          f"'weaverbird scope approve {args.dir} --by <your name>'.")
    return 0


def cmd_brain_check(args) -> int:
    settings_file, data = brain_module.resolve_settings(args.settings,
                                                        args.project)
    if settings_file:
        print(f"Settings: {settings_file}")
    else:
        print("Settings: no global settings file found — no model is "
              "configured. Weaverbird is bring-your-own-key: create a "
              "global-settings.yaml and fill in the models ladder.")
    statuses = brain_module.brain_status(data)
    for tier in brain_module.TIERS:
        st = statuses[tier]
        if st["configured"]:
            line = (f"- {tier}: provider {st['provider']}, model {st['model']}, "
                    f"key ({st['api_key_env']}): "
                    f"{'present' if st['key_present'] else 'not set'}")
            if st["base_url"]:
                line += f", base url {st['base_url']}"
            print(line)
        else:
            print(f"- {tier}: not configured — {st['reason']}")
    if not args.live:
        print("Add --live to make one tiny test call per usable tier.")
        return 0
    project = args.project
    scratch = None
    if not project:
        import tempfile
        scratch = tempfile.mkdtemp(prefix="weaverbird-brain-check-")
        project = scratch
        print(f"No --project given: live calls are logged to a scratch "
              f"ledger in {scratch}.")
    for tier in brain_module.TIERS:
        st = statuses[tier]
        if not st["usable"]:
            print(f"- {tier}: skipped — {st['reason']}")
            continue
        try:
            result = brain_module.complete(
                project, tier,
                [{"role": "user", "content": "Reply with the word ok"}],
                stage="brain-check", settings_path=settings_file,
                max_tokens=8)
        except brain_module.BrainBudgetError as exc:
            print(f"- {tier}: refused — {exc}")
            continue
        if result.ok:
            print(f"- {tier}: live call ok — reply {result.text.strip()!r}, "
                  f"{result.total_tokens} tokens logged "
                  f"({result.prompt_tokens} in, {result.completion_tokens} out).")
            if result.warning:
                print(f"  {result.warning}")
        else:
            print(f"- {tier}: live call failed — {result.error}")
    return 0


def cmd_finish_check(args) -> int:
    try:
        result = finish_module.evaluate_finish(args.dir)
    except finish_module.FinishError as exc:
        return _fail(str(exc))
    print("Finish check — the factory marks only what it can evidence:")
    if result["marked_now"]:
        print("Marked now (evidence found):")
        for key, note in result["marked_now"].items():
            print(f"- ✓ {finish_module.DOD_LABELS.get(key, key)} — {note}")
    if result["already_marked"]:
        print("Already marked:")
        for key, note in result["already_marked"].items():
            print(f"- ✓ {finish_module.DOD_LABELS.get(key, key)} — {note}")
    if result["open"]:
        print("Still open:")
        for key, reason in result["open"].items():
            print(f"- – {finish_module.DOD_LABELS.get(key, key)} — {reason}")
        return _fail(
            f"Not finished yet: {len(result['open'])} of "
            f"{len(state_module.DEFINITION_OF_DONE)} definition-of-done "
            "items are still open.")
    return _ok("Finished: every definition-of-done item is marked, each "
               "with its evidence. The finished gate now passes.")


def cmd_finish_report(args) -> int:
    try:
        path = finish_module.build_finish_report(
            args.dir, settings_path=args.settings)
    except finish_module.FinishError as exc:
        return _fail(str(exc))
    return _ok(f"Finish report written: {path}\n"
               "It is the document the approval stage reads: what was "
               "built, the definition of done with its evidence, what "
               "everything cost, and the decisions left for the human.")


def cmd_launch(args) -> int:
    """Bare `weaverbird`: the app is the product surface — launch it."""
    workspace = Path(args.workspace) if args.workspace else Path.cwd()
    return app_module.run_app(workspace, port=args.port or 8765,
                              open_browser=not args.no_open)


def cmd_version(_args) -> int:
    return _ok(f"weaverbird {__version__}")


def cmd_update(args) -> int:
    return update_module.run_update(check_only=args.check)


def cmd_settings_check(args) -> int:
    try:
        data = settings_module.load_settings(args.file)
    except settings_module.SettingsError as exc:
        return _fail(str(exc))
    result = settings_module.validate_settings(data)
    print(f"Settings file: {args.file}")
    if result.missing_required:
        print("Missing REQUIRED budget fields (must be set before a build):")
        for name in result.missing_required:
            print(f"- {name}")
    for error in result.errors:
        print(f"Problem: {error}")
    if result.ok:
        budgets = result.budgets
        return _ok(
            "Global settings are valid. Budgets: "
            f"tokens per run = {budgets.get('tokens_per_run')}, "
            f"tokens per game = {budgets.get('tokens_per_game')}."
        )
    print("Fill in the missing fields — token and cost caps live here, in the "
          "global settings, never in a single game's scope file.")
    return 1


def cmd_connectors_list(args) -> int:
    try:
        registry = connectors_module.load_registry(args.registry)
    except connectors_module.ConnectorError as exc:
        return _fail(str(exc))
    if not registry.connectors:
        return _fail("No connectors found in the registry.")
    print("Connectors in the registry:")
    for connector in registry.connectors:
        print(f"- {connector.id} ({connector.kind or 'connector'})")
        if connector.can_do_alone:
            print(f"    can do alone: {', '.join(connector.can_do_alone)}")
        if connector.needs_approval:
            print(f"    needs approval: {', '.join(connector.needs_approval)}")
        if connector.user_only:
            print(f"    user only: {', '.join(connector.user_only)}")
    return 0


_CAPABILITY_TEXT = {
    connectors_module.ALLOWED_ALONE: "allowed — the factory may do this on its own",
    connectors_module.NEEDS_APPROVAL: "needs approval — the factory must ask the user first",
    connectors_module.USER_ONLY: "user only — only the user can do this",
}


def cmd_connectors_check(args) -> int:
    try:
        registry = connectors_module.load_registry(args.registry)
    except connectors_module.ConnectorError as exc:
        return _fail(str(exc))
    connector = registry.get(args.connector)
    if connector is None:
        print(f"Unknown connector '{args.connector}' — treated as needs approval (fail closed).")
        return 1
    classified = connectors_module.classify_capability(registry, args.connector, args.action)
    decision = connectors_module.check_capability(registry, args.connector, args.action)
    if classified == connectors_module.UNKNOWN:
        print(f"{args.connector} / {args.action}: this action is not in the registry, "
              "so it fails closed — needs approval.")
        return 0
    print(f"{args.connector} / {args.action}: {_CAPABILITY_TEXT[decision]}")
    return 0


def cmd_ledger_add(args) -> int:
    try:
        entry = ledger_module.add_entry(args.dir, args.stage, args.tier, args.tokens, args.cost)
    except ledger_module.LedgerError as exc:
        return _fail(str(exc))
    summary = ledger_module.summarise(args.dir)
    return _ok(
        f"Added to the ledger: stage {entry['stage']}, tier {entry['tier']}, "
        f"{entry['tokens']} tokens, cost {entry['cost']:.4f}. "
        f"Project total so far: {summary['tokens']} tokens, cost {summary['cost']:.4f}."
    )


def _print_summary(summary: dict) -> None:
    print(f"Entries: {summary['entries']}")
    print(f"Total tokens: {summary['tokens']}")
    print(f"Total cost: {summary['cost']:.4f}")
    if summary["by_stage"]:
        print("By stage:")
        for name, slot in sorted(summary["by_stage"].items()):
            print(f"- {name}: {slot['tokens']} tokens, cost {slot['cost']:.4f}")
    if summary["by_tier"]:
        print("By tier:")
        for name, slot in sorted(summary["by_tier"].items()):
            print(f"- {name}: {slot['tokens']} tokens, cost {slot['cost']:.4f}")


def cmd_ledger_show(args) -> int:
    try:
        summary = ledger_module.summarise(args.dir)
    except ledger_module.LedgerError as exc:
        return _fail(str(exc))
    if summary["entries"] == 0:
        return _ok("The ledger is empty — nothing has been spent on this project yet.")
    _print_summary(summary)
    return 0


def cmd_ledger_check(args) -> int:
    try:
        data = settings_module.load_settings(args.settings)
        result = ledger_module.check_budgets(args.dir, data)
    except (settings_module.SettingsError, ledger_module.LedgerError) as exc:
        return _fail(str(exc))
    status = result["status"]
    if result["tokens_cap"] is None:
        print("No per-game token cap is set in the global settings, so the budget "
              "cannot be checked. Set budgets.tokens_per_game first.")
        return 1
    print(f"Tokens used: {result['tokens']} of {result['tokens_cap']} "
          f"({result['tokens_percent']:.1f}% of the per-game cap; "
          f"warning at {result['warn_at_percent']}%)")
    if result["cost_cap"] is not None:
        print(f"Cost so far: {result['cost']:.4f} of {result['cost_cap']} "
              f"({result['cost_percent']:.1f}% of the monthly ceiling)")
    if status == "over":
        print("Status: OVER budget — the factory must stop and escalate to the user.")
        return 1
    if status == "warn":
        return _ok("Status: WARNING — spend is close to the cap.")
    return _ok("Status: OK — spend is within budget.")


def cmd_provenance_add(args) -> int:
    try:
        record = provenance_module.add_record(
            args.dir, args.asset, args.source, note=args.note, provider=args.provider
        )
    except provenance_module.ProvenanceError as exc:
        return _fail(str(exc))
    return _ok(f"Recorded provenance for {record['asset']}: source = {record['source']}"
               + (f", provider = {record['provider']}" if record["provider"] else "")
               + ".")


def cmd_provenance_list(args) -> int:
    records = provenance_module.list_records(args.dir)
    if not records:
        return _ok("No provenance records yet. An asset with no record is not used.")
    print(f"Provenance records ({len(records)}):")
    for record in records:
        line = f"- {record.get('asset')}: {record.get('source')}"
        if record.get("provider"):
            line += f" via {record['provider']}"
        if record.get("note"):
            line += f" — {record['note']}"
        print(line)
    return 0


def cmd_provenance_disclosure(args) -> int:
    print(provenance_module.disclosure_text(args.dir), end="")
    return 0


def cmd_pipeline_status(args) -> int:
    try:
        state = state_module.load_state(args.dir)
    except state_module.PipelineError as exc:
        return _fail(str(exc))
    print(f"Current stage: {state['stage']}")
    following = state_module.next_stage(state["stage"])
    print(f"Next stage: {following or '(none — this is the final stage)'}")
    dod = state.get("definition_of_done", {})
    done = [k for k, v in dod.items() if v]
    open_items = [k for k, v in dod.items() if not v]
    evidence = state.get("dod_evidence", {})
    print(f"Definition of done: {len(done)} of {len(dod)} items true")
    for key in done:
        note = evidence.get(key, {})
        if note:
            print(f"- done: {key} — {note.get('note', '')} "
                  f"(marked by {note.get('by', 'weaverbird')})")
        else:
            print(f"- done: {key}")
    for key in open_items:
        print(f"- still open: {key}")
    reasons = state_module.gate_reasons(args.dir, state)
    if reasons:
        print("Blocked from advancing:")
        for reason in reasons:
            print(f"- {reason}")
    elif following:
        print("Ready to advance.")
    print("History:")
    for item in state.get("history", []):
        print(f"- {item.get('stage')} at {item.get('at')}")
    return 0


def cmd_skeleton_build(args) -> int:
    try:
        report = skeleton_module.build_skeleton(args.dir)
    except skeleton_module.SkeletonError as exc:
        return _fail(str(exc))
    game_dir = Path(args.dir) / skeleton_module.GAME_DIR_NAME
    print(f"Built the grey-box Godot skeleton in {game_dir}:")
    for rel in report["files"]:
        print(f"- {rel}")
    validation = report.get("validation", {})
    if validation.get("passed"):
        runs = ", ".join(run["name"] for run in validation.get("runs", []))
        print(f"Godot validation passed ({validation.get('godot_version')}): "
              f"{runs} — all clean.")
        print(f"Known-good tag: {report.get('known_good_tag')}")
    print("Cost: one ledger entry for the skeleton stage, 0 tokens, 0 cost — "
          "this stage is deterministic and calls no model.")
    print(f"Next step: open {game_dir} in Godot and press play, or check "
          f"'weaverbird pipeline status {args.dir}'.")
    return 0


def cmd_assets_generate(args) -> int:
    try:
        report = assets_module.generate_assets(args.dir)
    except assets_module.AssetsError as exc:
        return _fail(str(exc))
    print(f"Generated {len(report['files'])} asset files for the "
          f"{report['archetype']} game (palette "
          f"{report['palette']['accent_hex']} / "
          f"{report['palette']['target_hex']} from the style lock):")
    for rel in report["files"]:
        print(f"- {rel}")
    print("Every file is recorded in the provenance log (source: code, "
          "provider: weaverbird-assets). Cost: 0 tokens — deterministic.")
    return 0


def cmd_assets_list(args) -> int:
    try:
        listing = assets_module.list_assets(args.dir)
    except assets_module.AssetsError as exc:
        return _fail(str(exc))
    print(f"Generated assets for {args.dir} (provenance in brackets):")
    for entry in listing:
        if entry["missing"]:
            size = "MISSING — the file is gone"
        else:
            size = f"{entry['size']} bytes"
        who = entry["provider"] or entry["source"] or "no record"
        print(f"- {entry['name']}: {entry['path']} — {size} "
              f"[{who}]")
        if entry["note"]:
            print(f"    {entry['note']}")
    print("Redraw one with 'weaverbird assets regenerate <dir> <name>'.")
    return 0


def cmd_assets_regenerate(args) -> int:
    try:
        result = assets_module.regenerate_asset(args.dir, args.name)
    except assets_module.AssetsError as exc:
        return _fail(str(exc))
    print(f"Regenerated {result['path']} ({result['size']} bytes) "
          "from the current palette and recorded it in the "
          "provenance log. Cost: 0 tokens.")
    return 0


def cmd_build_game(args) -> int:
    try:
        report = generate_module.build_game(
            args.dir, archetype=args.archetype, settings_path=args.settings)
    except generate_module.GenerationError as exc:
        return _fail(str(exc))
    print(f"Generated the real game: archetype {report['archetype']} "
          f"({report['archetype_reason']}).")
    print(f"Content: {report['units_delivered']} units delivered "
          f"(scope asked for {report['units_requested']}).")
    print(f"Brain: {'used — ' if report['brain_used'] else 'not used — '}"
          f"{report['brain_note']}")
    validation = report.get("validation", {})
    if validation.get("passed"):
        runs = ", ".join(run["name"] for run in validation.get("runs", []))
        print(f"Godot validation passed ({validation.get('godot_version')}): "
              f"{runs} — all clean.")
        print(f"Known-good tag: {report.get('known_good_tag')}")
    print("The game reads its tuning from game/game_config.json.")
    return 0


def cmd_test_run(args) -> int:
    try:
        report = testing_module.run_tests(args.dir)
    except testing_module.TestingError as exc:
        return _fail(str(exc))
    print("Headless test suite — base runs plus the scripted players "
          "(competent, breaker, first-time):")
    for line in testing_module.digest_lines(report):
        print(line)
    return 0 if report.get("ok") else 1


def cmd_feedback_fetch(args) -> int:
    report = feedback_module.fetch_feedback(args.dir)
    total = 0
    for source, entry in report.items():
        total += entry["added"]
        print(f"{source}: {entry['status']} — {entry['detail']} "
              f"(+{entry['added']} new)")
    print(f"Fetched. {total} new item(s) in the feedback store "
          f"(.weaverbird/feedback.json). Triage with "
          f"'weaverbird feedback triage {args.dir}'.")
    return 0


def cmd_feedback_add(args) -> int:
    item, created = feedback_module.add_item(
        args.dir, source="manual", text=args.text, author=args.author or "")
    print(f"{'Added' if created else 'Already on record'}: "
          f"[{item['id']}] {item['text']}")
    return 0


def cmd_feedback_list(args) -> int:
    store = feedback_module.load_store(args.dir)
    if not store["items"]:
        print("No feedback yet. Fetch it with "
              f"'weaverbird feedback fetch {args.dir}' or add some with "
              "'weaverbird feedback add'.")
        return 0
    print("Feedback queue (run triage to rank it):")
    for line in feedback_module.queue_lines(
            feedback_module.ranked_queue(store)):
        print("- " + line)
    return 0


def cmd_feedback_triage(args) -> int:
    try:
        result = feedback_module.triage(args.dir, settings_path=args.settings)
    except feedback_module.FeedbackError as exc:
        return _fail(str(exc))
    print(f"Triage: {result['note']}.")
    if not result["items"]:
        print("The feedback queue is empty.")
        return 0
    print("Ranked queue (cluster mentions × severity):")
    for line in feedback_module.queue_lines(result["items"]):
        print("- " + line)
    return 0


def cmd_feedback_approve(args) -> int:
    try:
        item = feedback_module.set_status(args.dir, args.item, "approved")
    except feedback_module.FeedbackError as exc:
        return _fail(str(exc))
    return _ok(f"[{item['id']}] approved — it can enter the patch loop: "
               f"'weaverbird patch start {args.dir} --item {item['id']}'.")


def cmd_feedback_dismiss(args) -> int:
    try:
        item = feedback_module.set_status(args.dir, args.item, "dismissed")
    except feedback_module.FeedbackError as exc:
        return _fail(str(exc))
    return _ok(f"[{item['id']}] dismissed.")


def cmd_patch_start(args) -> int:
    try:
        result = patch_module.start_patch(
            args.dir, args.item, settings_path=args.settings)
    except patch_module.PatchError as exc:
        return _fail(str(exc))
    if result["outcome"] == "patched":
        print(f"Patch shipped: version {result['version']} "
              f"(tagged known-good/patch-{result['version']}).")
        print(f"Change: {result['change']}")
        print("Gates passed: the full test suite, and save "
              "compatibility (a pre-patch save loads with stats intact).")
        print("CHANGELOG.md and .weaverbird/patch_log.json updated; "
              "the feedback item is marked patched.")
        return 0
    if result["outcome"] == "planned":
        print("No automatic patch for this item — a patch plan was "
              f"written instead: {result['plan']}")
        print(f"Why: {result['reason']}")
        return 0
    print(f"Patch rejected: {result['reason']}")
    print("The working tree was restored to the pre-patch checkpoint; "
          "the item is back to approved with the failure recorded.")
    return 1


def cmd_repair_run(args) -> int:
    try:
        result = repair_module.run_repair(
            args.dir, settings_path=args.settings,
            max_attempts=args.max_attempts)
    except repair_module.RepairError as exc:
        return _fail(str(exc))
    print(f"Self-repair: {result['outcome']} — {result['note']}")
    for attempt in result["attempts"]:
        print(f"- attempt {attempt['attempt']}: {attempt['outcome']}")
        for strategy in attempt.get("strategies", []):
            print(f"    {strategy}")
    print("Full record: REPAIR_REPORT.md in the project.")
    return 0 if result["outcome"] in ("repaired", "already_passing") else 1


def cmd_playtest_model(args) -> int:
    try:
        record = modelplayer_module.run_model_playtest(
            args.dir, settings_path=args.settings,
            max_decisions=args.max_decisions)
    except modelplayer_module.ModelPlayerError as exc:
        return _fail(f"Model playtest did not run: {exc}")
    print(f"Model playtest finished: {record['outcome']} — "
          f"{record['decisions']} decisions, {record['deaths']} deaths, "
          f"{record['time_s']}s.")
    if record["finished"]:
        print(f"The model {record['result']} the game.")
    for note in record["confusion_notes"]:
        print(f"Confusion (filed as feedback): {note}")
    print("Transcript: .weaverbird/model_playtest.json; screenshots "
          "in .weaverbird/model_playtest/shots/.")
    return 0 if record["finished"] else 1


# ---------------------------------------------------------------------------
# Balance lab

def _print_balance_table(project) -> list:
    """Print the per-unit table + flags. Returns the flags."""
    report = testing_module.load_report(project)
    if report is None:
        print("No test report yet — run 'weaverbird test run "
              f"{project}' first; the lab reads its per-unit records.")
        return []
    units = balance_module.load_units(project)
    if not units:
        print("The latest test report has no per-unit records — "
              "it was written by an older build. Re-run "
              "'weaverbird test run' with the current build and "
              "the scripted players will record per-unit stats.")
        return []
    rows = balance_module.unit_rows(units)

    def cell(record) -> str:
        # Formatted from the fields the record itself carries —
        # a metric a game does not record is simply absent, never
        # guessed from the game's kind.
        if record is None:
            return "—"
        if not record.get("completed"):
            state = "NOT completed"
        else:
            state = "completed"
        bits = [state]
        if record.get("deaths"):
            bits.append(f"{record['deaths']} deaths")
        if record.get("time_s") is not None:
            bits.append(f"{record['time_s']}s")
        if record.get("moves_used") is not None:
            bits.append(f"{record['moves_used']}/"
                        f"{record.get('move_budget', '?')} moves")
        return ", ".join(bits)

    print("Per-unit records from the latest test run:")
    for row in rows:
        print(f"- Unit {row['unit']}: competent — "
              f"{cell(row['competent'])} · first-time — "
              f"{cell(row['first_time'])}")
    model = balance_module.model_summary(project)
    if model:
        print(f"Model playtest on record: {model['outcome']} — "
              f"{model['decisions']} decisions, {model['deaths']} "
              f"deaths, finished: {model['finished']}.")
    flags = balance_module.analyse(units)
    if flags:
        print("Flags:")
        for flag in flags:
            print(f"- [{flag['kind']}] {flag['text']}")
    else:
        print("No flags — no spikes, walls, stalls or breeze in "
              "the recorded units.")
    return flags


def cmd_balance_show(args) -> int:
    _print_balance_table(args.dir)
    return 0


def cmd_balance_suggest(args) -> int:
    _print_balance_table(args.dir)
    try:
        found = balance_module.suggestions(args.dir)
    except balance_module.BalanceError as exc:
        return _fail(str(exc))
    if not found:
        print("No suggestions — nothing flagged, or the flagged "
              "fixes are already at their bounds.")
        return 0
    print("Suggestions (bounded knobs only — the same clamps the "
          "patch loop and repair use):")
    for index, suggestion in enumerate(found, start=1):
        print(f"{index}. [{suggestion['kind']}] {suggestion['rationale']}")
        for line in suggestion["changes"]:
            print(f"     {line}")
    print("Apply one with 'weaverbird balance apply <dir> "
          "--suggestion N' (it ships through the patch gates), or "
          "preview it with 'weaverbird balance compare'.")
    return 0


def cmd_balance_apply(args) -> int:
    try:
        result = balance_module.apply_suggestion(
            args.dir, args.suggestion, settings_path=args.settings)
    except (balance_module.BalanceError, patch_module.PatchError) as exc:
        return _fail(str(exc))
    item = result["item"]
    if result["outcome"] == "patched":
        print(f"Balance patch shipped: version {result['version']} "
              f"(tagged known-good/patch-{result['version']}).")
        print(f"Change: {result['change']}")
        print("Gates passed: the full test suite, and save "
              "compatibility. CHANGELOG.md cites the lab numbers; "
              f"the synthetic item is [{item}] (source balance-lab).")
        return 0
    if result["outcome"] == "planned":
        print("No automatic patch — a patch plan was written "
              f"instead: {result['plan']}")
        print(f"Why: {result['reason']}")
        return 0
    print(f"Balance patch rejected: {result['reason']}")
    print("The working tree was restored to the pre-patch "
          "checkpoint — nothing shipped.")
    return 1


def cmd_balance_compare(args) -> int:
    if args.suggestion is not None and args.config:
        return _fail("Give either --suggestion or --config, not both.")
    overrides = None
    if args.config:
        try:
            overrides = json.loads(
                Path(args.config).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return _fail(f"Could not read the overrides file: {exc}")
        if not isinstance(overrides, dict):
            return _fail("The overrides file must hold a JSON object "
                         "of config fields.")
    try:
        report = balance_module.compare(
            args.dir, suggestion_index=args.suggestion,
            config_overrides=overrides, settings_path=args.settings)
    except balance_module.BalanceError as exc:
        return _fail(str(exc))
    base, cand = report["baseline"], report["candidate_metrics"]
    print(f"Compare: {report['candidate']} "
          f"({'; '.join(report['changes'])})")
    print(f"- baseline: suite {'PASS' if base['ok'] else 'FAIL'}, "
          f"unit deaths {base['total_deaths']}, unit time "
          f"{base['total_time_s']}s, {base['avg_fps']} avg FPS"
          + (" (from the existing test report)"
             if report["baseline_from_existing_report"] else ""))
    print(f"- candidate: suite {'PASS' if cand['ok'] else 'FAIL'}, "
          f"unit deaths {cand['total_deaths']}, unit time "
          f"{cand['total_time_s']}s, {cand['avg_fps']} avg FPS")
    print(f"Verdict: {report['verdict']}")
    print("The project was restored to exactly its starting state. "
          "Full side-by-side: .weaverbird/balance_report.json")
    return 0


# ---------------------------------------------------------------------------
# Jobs

def _jobs_usage() -> int:
    print("Usage: weaverbird jobs <workspace> — recent jobs and "
          "their statuses")
    print("       weaverbird jobs show <workspace> <job-id> — one "
          "job's event log")
    return 1


def cmd_jobs(argv) -> int:
    """`weaverbird jobs <workspace>` / `jobs show <workspace> <id>`.

    Parsed by hand: the jobs command takes a workspace path where
    other commands take a subcommand, so it does not fit the
    subparser table. Read-only — the records live under
    <workspace>/.weaverbird-app/jobs/ and the app writes them.
    """
    if not argv:
        return _jobs_usage()
    if argv[0] in ("-h", "--help", "help"):
        _jobs_usage()
        return 0
    if argv[0] == "show":
        if len(argv) != 3:
            return _jobs_usage()
        store = jobs_module.JobStore(argv[1])
        job = store.load(argv[2])
        if job is None:
            return _fail(f"No job with id {argv[2]!r} in workspace "
                         f"{argv[1]}.")
        print(f"Job {job['id']} — {job.get('label', job.get('action'))} "
              f"for {job.get('project')}: {job.get('status')}")
        print(f"Created {job.get('created')} · started "
              f"{job.get('started') or '—'} · finished "
              f"{job.get('finished') or '—'}")
        for event in job.get("events", []):
            print(f"  {event.get('at', '')}  {event.get('text', '')}")
        if job.get("result"):
            print(f"Result: {job['result']}")
        if job.get("error"):
            print(f"Error: {job['error']}")
        return 0
    if len(argv) != 1:
        return _jobs_usage()
    store = jobs_module.JobStore(argv[0])
    jobs = store.list(limit=15)
    if not jobs:
        print("No jobs recorded for this workspace yet — jobs are "
              "started from the app (every project action is one).")
        return 0
    print(f"Recent jobs in {argv[0]} (newest first):")
    for job in jobs:
        line = (f"- {job['id']}  {job.get('status', '?'):11s} "
                f"{job.get('label', job.get('action', ''))} "
                f"({job.get('project', '')})")
        print(line)
        detail = job.get("error") or job.get("result")
        if detail:
            print(f"    {detail}")
    return 0


def cmd_workers_list(args) -> int:
    lines = ["Workers — every factory step, independently runnable:"]
    for name, spec in workers_module.WORKERS.items():
        deps = ", ".join(spec.depends_on) or "—"
        lines.append(f"  {name:<15} {spec.title}")
        lines.append(f"  {'':<15} inputs: {', '.join(spec.inputs) or '—'}")
        lines.append(f"  {'':<15} outputs: {', '.join(spec.outputs) or '—'}")
        lines.append(f"  {'':<15} depends on: {deps}")
    return _ok("\n".join(lines))


def cmd_orchestrate(args) -> int:
    try:
        if args.plan_only:
            data = orchestrator_module.plan(
                args.dir, target=args.to, reactive=args.reactive)
            lines = [f"Plan for {args.dir} → {data['target']}:"]
            for step in data["steps"]:
                lines.append(f"  {step['worker']:<15} {step['status']:<8} "
                             f"{step['reason']}")
            return _ok("\n".join(lines))
        data = orchestrator_module.run(
            args.dir, target=args.to, reactive=args.reactive,
            force=args.force,
            reporter=jobs_module.print_reporter)
    except orchestrator_module.OrchestratorError as exc:
        return _fail(str(exc))
    for step in data["steps"]:
        mark = {"done": "✓", "skipped": "–", "failed": "✗",
                "blocked": "!"}.get(step["status"], "?")
        note = f" ({step['note']})" if step.get("note") else ""
        lines = f"{mark} {step['worker']}{note}"
        print(lines)
    if data.get("outcome") == "done":
        return _ok(f"Plan complete — target {data['target']} reached. "
                   f"Plan: {orchestrator_module.plan_path_for(args.dir)}")
    escalation = data.get("escalation") or {}
    details = "; ".join(i.get("detail", "")
                        for i in escalation.get("issues", []))
    return _fail(f"Plan stopped ({data.get('outcome')}): owner "
                 f"{escalation.get('worker', '?')} — {details}")


def register_all_stage_handlers() -> None:
    """Register every built stage handler with the pipeline.

    Used by `pipeline advance` and the app. Since 0.7.0 the
    handlers delegate to the worker adapters (workers.py), so
    advancing a stage runs exactly the worker the orchestrator
    would run — the surfaces cannot diverge.
    """
    workers_module.register_stage_handlers()


def cmd_pipeline_advance(args) -> int:
    # Stage handlers live in the library; registering them here means
    # advancing out of a stage runs that stage's work through the
    # normal gate machinery.
    register_all_stage_handlers()
    try:
        state = state_module.advance(args.dir)
    except state_module.PipelineError as exc:
        return _fail(str(exc))
    return _ok(f"Advanced. The pipeline is now at stage: {state['stage']}")


# --------------------------------------------------------------------------
# argument parsing

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="weaverbird",
        description="Weaverbird — a game factory on top of Godot. "
                    "Run it with no command to open the app.",
    )
    parser.add_argument("--version", action="version", version=f"weaverbird {__version__}")
    parser.add_argument("--workspace", default=None, metavar="DIR",
                        help="Workspace for the app launch (default: the "
                             "current folder).")
    parser.add_argument("--port", type=int, default=None,
                        help="Port for the app launch (default: 8765).")
    parser.add_argument("--no-open", action="store_true",
                        help="Serve the app without opening a browser.")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("version", help="Print the Weaverbird version.")
    update_cmd = sub.add_parser(
        "update", help="Update Weaverbird from its GitHub releases.")
    update_cmd.add_argument("--check", action="store_true",
                            help="Only report whether an update exists.")

    sub.add_parser("doctor", help="Check python, git, GitHub login, Godot and settings.")

    identity_cmd = sub.add_parser("identity", help="Show the Weaverbird visual identity.")
    identity_cmd.add_argument("--export", default=None, metavar="DIR",
                              help="Also write the design tokens and the mark into DIR.")

    init = sub.add_parser("init", help="Scaffold a new factory project.")
    init.add_argument("dir")
    init.add_argument("--with-settings", action="store_true",
                      help="Also copy the global-settings template into the project.")
    init.add_argument("--force", action="store_true",
                      help="Scaffold even if the directory is not empty.")

    demo_cmd = sub.add_parser(
        "demo", help="Install the bundled demo project (a finished "
                     "game) into the workspace.")
    demo_cmd.add_argument("--name", default="demo-arena",
                          help="Folder name in the workspace "
                               "(default: demo-arena). An existing "
                               "folder is never overwritten.")

    export_cmd = sub.add_parser(
        "export", help="Export a finished game to local zip files "
                       "(linux, windows, macos, source). Local "
                       "files only — nothing is published.")
    export_cmd.add_argument("dir", help="The finished project folder.")
    export_cmd.add_argument(
        "--targets", default=None,
        help="Comma-separated subset of linux,windows,macos,source "
             "(default: all four).")

    scope = sub.add_parser("scope", help="Work with a project's scope file.")
    scope_sub = scope.add_subparsers(dest="scope_command")
    for name, help_text in (
        ("check", "Print filled and missing REQUIRED fields."),
        ("questions", "Print the next interview questions for missing fields."),
    ):
        p = scope_sub.add_parser(name, help=help_text)
        p.add_argument("dir")
    approve = scope_sub.add_parser("approve", help="Approve a complete scope.")
    approve.add_argument("dir")
    approve.add_argument("--by", required=True, help="Name of the person approving.")
    interview = scope_sub.add_parser(
        "interview",
        help="Store a prompt in the scope and extract answers from it "
             "(bring-your-own-key brain; degrades gracefully without one).")
    interview.add_argument("dir")
    interview.add_argument("--prompt", required=True,
                           help="The maker's description of the game.")
    interview.add_argument("--settings", default=None,
                           help="Global settings file (default: search the "
                                "usual places).")

    chat = sub.add_parser(
        "interview",
        help="The scope interview as a conversation (line mode; "
             "piped stdin works). The same engine as the app chat.")
    chat.add_argument("dir")
    chat.add_argument("--settings", default=None,
                      help="Global settings file (default: search the "
                           "usual places).")

    brain_cmd = sub.add_parser(
        "brain", help="Check the bring-your-own-key model ladder.")
    brain_sub = brain_cmd.add_subparsers(dest="brain_command")
    brain_check = brain_sub.add_parser(
        "check", help="Show per-tier provider, model and whether the key "
                      "environment variable is set (never its value).")
    brain_check.add_argument("--settings", default=None,
                             help="Global settings file (default: search the usual places).")
    brain_check.add_argument("--project", default=None,
                             help="Project directory (helps find the settings file; "
                                  "its ledger receives --live call entries).")
    brain_check.add_argument("--live", action="store_true",
                             help="Also make one tiny call per usable tier and "
                                  "show the token counts it logged.")

    finish_cmd = sub.add_parser(
        "finish", help="Evidence-based definition-of-done check and report.")
    finish_sub = finish_cmd.add_subparsers(dest="finish_command")
    finish_check = finish_sub.add_parser(
        "check", help="Mark definition-of-done items the evidence supports.")
    finish_check.add_argument("dir")
    finish_report = finish_sub.add_parser(
        "report", help="Run the check and write FINISH_REPORT.md.")
    finish_report.add_argument("dir")
    finish_report.add_argument("--settings", default=None,
                               help="Global settings file for the optional "
                                    "frontier-written summary.")


    settings_cmd = sub.add_parser("settings", help="Work with the global settings file.")
    settings_sub = settings_cmd.add_subparsers(dest="settings_command")
    check = settings_sub.add_parser("check", help="Validate a global-settings file.")
    check.add_argument("file")

    connectors_cmd = sub.add_parser("connectors", help="Inspect the connector registry.")
    connectors_sub = connectors_cmd.add_subparsers(dest="connectors_command")
    list_cmd = connectors_sub.add_parser("list", help="List connectors and their capabilities.")
    list_cmd.add_argument("--registry", default=None,
                          help="Registry file (default: project copy, else packaged template).")
    check_cmd = connectors_sub.add_parser("check", help="Check one connector action.")
    check_cmd.add_argument("connector")
    check_cmd.add_argument("action")
    check_cmd.add_argument("--registry", default=None)

    ledger_cmd = sub.add_parser("ledger", help="Work with a project's token/cost ledger.")
    ledger_sub = ledger_cmd.add_subparsers(dest="ledger_command")
    add = ledger_sub.add_parser("add", help="Add a spend entry.")
    add.add_argument("dir")
    add.add_argument("--stage", required=True)
    add.add_argument("--tier", required=True, choices=list(ledger_module.TIERS))
    add.add_argument("--tokens", required=True, type=int)
    add.add_argument("--cost", required=True, type=float)
    show = ledger_sub.add_parser("show", help="Show ledger totals.")
    show.add_argument("dir")
    check_ledger = ledger_sub.add_parser("check", help="Check spend against the global budgets.")
    check_ledger.add_argument("dir")
    check_ledger.add_argument("--settings", required=True)

    prov = sub.add_parser("provenance", help="Work with a project's asset provenance log.")
    prov_sub = prov.add_subparsers(dest="provenance_command")
    padd = prov_sub.add_parser("add", help="Record how an asset was made.")
    padd.add_argument("dir")
    padd.add_argument("--asset", required=True)
    padd.add_argument("--source", required=True, choices=list(provenance_module.SOURCES))
    padd.add_argument("--note", default="")
    padd.add_argument("--provider", default=None)
    plist = prov_sub.add_parser("list", help="List provenance records.")
    plist.add_argument("dir")
    pdis = prov_sub.add_parser("disclosure", help="Print an AI-content disclosure draft.")
    pdis.add_argument("dir")

    workers_cmd = sub.add_parser(
        "workers", help="The independent workers and their contracts.")
    workers_sub = workers_cmd.add_subparsers(dest="workers_command")
    workers_sub.add_parser(
        "list", help="Every registered worker with its inputs, "
                     "outputs and dependencies.")

    orch = sub.add_parser(
        "orchestrate", help="Run the orchestrator's plan for a project: "
                            "the workers in order, skipping any whose "
                            "inputs are unchanged.")
    orch.add_argument("dir")
    orch.add_argument("--to", default="finished",
                      help="Target stage (default: finished).")
    orch.add_argument("--force", action="store_true",
                      help="Run every step even if its inputs are unchanged.")
    orch.add_argument("--reactive", default=None,
                      help="A reactive plan instead: feedback, patch, "
                           "balance, repair, model-playtest.")
    orch.add_argument("--plan-only", action="store_true",
                      help="Print the plan without running it.")

    pipeline = sub.add_parser("pipeline", help="Inspect or advance a project's pipeline.")
    pipeline_sub = pipeline.add_subparsers(dest="pipeline_command")
    pstatus = pipeline_sub.add_parser("status", help="Show the current stage and gates.")
    pstatus.add_argument("dir")
    padvance = pipeline_sub.add_parser("advance", help="Advance one stage, if the gate passes.")
    padvance.add_argument("dir")

    skeleton_cmd = sub.add_parser(
        "skeleton", help="Build the grey-box Godot skeleton from an approved scope."
    )
    skeleton_sub = skeleton_cmd.add_subparsers(dest="skeleton_command")
    sbuild = skeleton_sub.add_parser(
        "build", help="Generate the Godot project and validate it with Godot."
    )
    sbuild.add_argument("dir")

    assets_cmd = sub.add_parser(
        "assets", help="Generate the game's assets (sprites, icon, audio)."
    )
    assets_sub = assets_cmd.add_subparsers(dest="assets_command")
    agen = assets_sub.add_parser(
        "generate", help="Draw sprites and the icon from the style-lock "
                         "palette and synthesise the audio, deterministically."
    )
    agen.add_argument("dir")
    alist = assets_sub.add_parser(
        "list", help="List the generated assets with their provenance "
                     "and file sizes; missing files are flagged."
    )
    alist.add_argument("dir")
    aregen = assets_sub.add_parser(
        "regenerate", help="Redraw exactly one generated asset from "
                           "the current palette (audio is re-synthesised)."
    )
    aregen.add_argument("dir")
    aregen.add_argument("name", help="Asset name, e.g. player, icon, music "
                                     "(see 'assets list').")

    build_cmd = sub.add_parser(
        "build", help="Generate the real game from the approved scope."
    )
    build_sub = build_cmd.add_subparsers(dest="build_command")
    bgame = build_sub.add_parser(
        "game", help="Pick the archetype from the scope, generate the "
                     "game, validate it in Godot, tag it known-good."
    )
    bgame.add_argument("dir")
    bgame.add_argument("--archetype", default=None,
                       choices=list(generate_module.ARCHETYPES),
                       help="Override archetype detection.")
    bgame.add_argument("--settings", default=None,
                       help="Global settings file for the optional "
                            "mid-tier tuning proposal.")

    test_cmd = sub.add_parser(
        "test", help="Run the headless test suite with scripted players."
    )
    test_sub = test_cmd.add_subparsers(dest="test_command")
    trun = test_sub.add_parser(
        "run", help="Base runs + competent (win and lose), breaker and "
                    "first-time scripted players; writes the test report."
    )
    trun.add_argument("dir")

    feedback_cmd = sub.add_parser(
        "feedback", help="Player feedback: fetch, triage, approve.")
    feedback_sub = feedback_cmd.add_subparsers(dest="feedback_command")
    ffetch = feedback_sub.add_parser(
        "fetch", help="Pull GitHub issues, itch comments (best-effort) "
                      "and feedback-inbox/ files into the store.")
    ffetch.add_argument("dir")
    fadd = feedback_sub.add_parser(
        "add", help="Add one feedback item by hand.")
    fadd.add_argument("dir")
    fadd.add_argument("--text", required=True)
    fadd.add_argument("--author", default="")
    flist = feedback_sub.add_parser(
        "list", help="Show the feedback queue, ranked.")
    flist.add_argument("dir")
    ftriage = feedback_sub.add_parser(
        "triage", help="Classify, cluster and rank the queue.")
    ftriage.add_argument("dir")
    ftriage.add_argument("--settings", default=None)
    for name, help_text in (("approve", "Approve an item for the patch loop."),
                            ("dismiss", "Dismiss an item.")):
        p = feedback_sub.add_parser(name, help=help_text)
        p.add_argument("dir")
        p.add_argument("--item", required=True)

    patch_cmd = sub.add_parser(
        "patch", help="Ship an approved feedback item as a verified patch.")
    patch_sub = patch_cmd.add_subparsers(dest="patch_command")
    pstart = patch_sub.add_parser(
        "start", help="Apply the item's fix, run the test suite and the "
                      "save-compatibility gate, then version + changelog "
                      "+ tag — or restore and record the rejection.")
    pstart.add_argument("dir")
    pstart.add_argument("--item", required=True)
    pstart.add_argument("--settings", default=None)

    repair_cmd = sub.add_parser(
        "repair", help="Self-repair: bounded automatic fixes for a "
                       "failing test suite, with git checkpoints.")
    repair_sub = repair_cmd.add_subparsers(dest="repair_command")
    rrun = repair_sub.add_parser(
        "run", help="Classify the failures and try strategies until "
                    "the suite passes or attempts run out.")
    rrun.add_argument("dir")
    rrun.add_argument("--settings", default=None)
    rrun.add_argument("--max-attempts", type=int, default=None,
                      help="Override repair.max_attempts from settings.")

    playtest_cmd = sub.add_parser(
        "playtest", help="Playtesters beyond the scripted three.")
    playtest_sub = playtest_cmd.add_subparsers(dest="playtest_command")
    pmodel = playtest_sub.add_parser(
        "model", help="A vision model plays the game from screenshots "
                      "through the in-game bridge (BYOK).")
    pmodel.add_argument("dir")
    pmodel.add_argument("--settings", default=None)
    pmodel.add_argument("--max-decisions", type=int, default=None,
                        help="Decision cap (default: playtest."
                             "model_max_decisions, else 40).")

    balance_cmd = sub.add_parser(
        "balance", help="The balance lab: per-unit difficulty "
                        "evidence, flags, suggestions, A/B compares.")
    balance_sub = balance_cmd.add_subparsers(dest="balance_command")
    bshow = balance_sub.add_parser(
        "show", help="Per-unit table from the latest test report, "
                     "with the deterministic flags.")
    bshow.add_argument("dir")
    bsuggest = balance_sub.add_parser(
        "suggest", help="Numbered suggestions from the flags, using "
                        "only the patch loop's bounded knobs, with "
                        "exact config diffs.")
    bsuggest.add_argument("dir")
    bapply = balance_sub.add_parser(
        "apply", help="Ship one suggestion through the patch loop "
                      "(test stage + save-compatibility gates, "
                      "version bump, changelog).")
    bapply.add_argument("dir")
    bapply.add_argument("--suggestion", type=int, required=True)
    bapply.add_argument("--settings", default=None)
    bcompare = balance_sub.add_parser(
        "compare", help="A/B a suggestion (or a JSON file of config "
                        "overrides) in a checkpointed run; the "
                        "project ends exactly as it started.")
    bcompare.add_argument("dir")
    bcompare.add_argument("--suggestion", type=int, default=None)
    bcompare.add_argument("--config", default=None,
                          help="Path to a JSON object of config "
                               "overrides (instead of --suggestion).")
    bcompare.add_argument("--settings", default=None)

    return parser


_HANDLERS = {
    ("doctor", None): cmd_doctor,
    ("identity", None): cmd_identity,
    ("init", None): cmd_init,
    ("demo", None): cmd_demo,
    ("export", None): cmd_export,
    ("scope", "check"): cmd_scope_check,
    ("scope", "questions"): cmd_scope_questions,
    ("scope", "approve"): cmd_scope_approve,
    ("scope", "interview"): cmd_scope_interview,
    ("interview", None): cmd_interview,
    ("brain", "check"): cmd_brain_check,
    ("finish", "check"): cmd_finish_check,
    ("finish", "report"): cmd_finish_report,
    ("version", None): cmd_version,
    ("update", None): cmd_update,
    ("settings", "check"): cmd_settings_check,
    ("connectors", "list"): cmd_connectors_list,
    ("connectors", "check"): cmd_connectors_check,
    ("ledger", "add"): cmd_ledger_add,
    ("ledger", "show"): cmd_ledger_show,
    ("ledger", "check"): cmd_ledger_check,
    ("provenance", "add"): cmd_provenance_add,
    ("provenance", "list"): cmd_provenance_list,
    ("provenance", "disclosure"): cmd_provenance_disclosure,
    ("workers", "list"): cmd_workers_list,
    ("orchestrate", None): cmd_orchestrate,
    ("pipeline", "status"): cmd_pipeline_status,
    ("pipeline", "advance"): cmd_pipeline_advance,
    ("skeleton", "build"): cmd_skeleton_build,
    ("assets", "generate"): cmd_assets_generate,
    ("assets", "list"): cmd_assets_list,
    ("assets", "regenerate"): cmd_assets_regenerate,
    ("build", "game"): cmd_build_game,
    ("test", "run"): cmd_test_run,
    ("feedback", "fetch"): cmd_feedback_fetch,
    ("feedback", "add"): cmd_feedback_add,
    ("feedback", "list"): cmd_feedback_list,
    ("feedback", "triage"): cmd_feedback_triage,
    ("feedback", "approve"): cmd_feedback_approve,
    ("feedback", "dismiss"): cmd_feedback_dismiss,
    ("patch", "start"): cmd_patch_start,
    ("repair", "run"): cmd_repair_run,
    ("playtest", "model"): cmd_playtest_model,
    ("balance", "show"): cmd_balance_show,
    ("balance", "suggest"): cmd_balance_suggest,
    ("balance", "apply"): cmd_balance_apply,
    ("balance", "compare"): cmd_balance_compare,
}

_SUBCOMMAND_ATTRS = {
    "scope": "scope_command",
    "brain": "brain_command",
    "finish": "finish_command",
    "settings": "settings_command",
    "connectors": "connectors_command",
    "ledger": "ledger_command",
    "provenance": "provenance_command",
    "workers": "workers_command",
    "pipeline": "pipeline_command",
    "skeleton": "skeleton_command",
    "assets": "assets_command",
    "build": "build_command",
    "test": "test_command",
    "feedback": "feedback_command",
    "patch": "patch_command",
    "repair": "repair_command",
    "playtest": "playtest_command",
    "balance": "balance_command",
}


def help_text() -> str:
    """`weaverbird help`: the app first, the scripting commands quiet."""
    return f"""Weaverbird {__version__} — a game factory on top of Godot

The app is the product. Run Weaverbird and the app opens in your
browser — a prompt becomes a game there, one stage at a time:
scope → skeleton → assets → build → headless test → playtest →
finished → approval → publish → live.

  weaverbird                      open the app (workspace = this folder)
  weaverbird --workspace <dir>    open the app for another workspace
  weaverbird --port <n>           serve on another port (default 8765)
  weaverbird --no-open            serve without opening a browser

Keep it current:
  weaverbird update               update to the latest release
  weaverbird update --check       just say whether an update exists
  weaverbird version              print the version

Project commands (scripting) — the same work the app does, as
commands, for scripts and terminals:
  weaverbird doctor                     check the setup
  weaverbird init <dir>                 start a new game project
  weaverbird demo                       install the bundled demo project (a finished game)
  weaverbird export <dir>               export a finished game to local zips (no publishing)
  weaverbird scope ...                  the scope file and the interview
  weaverbird brain check                the bring-your-own-key ladder
  weaverbird workers list               every independent worker + its contract
  weaverbird orchestrate <dir>          run the plan: workers in order, unchanged ones skipped
  weaverbird skeleton build <dir>       build the grey-box skeleton
  weaverbird assets generate <dir>      sprites, icon and audio
  weaverbird assets list|regenerate     the asset studio, as commands
  weaverbird build game <dir>           generate the real game
  weaverbird test run <dir>             headless tests + scripted players
  weaverbird repair run <dir>           self-repair a failing suite
  weaverbird balance ...                per-unit difficulty lab: show, suggest, apply, compare
  weaverbird jobs <workspace>           recent background jobs and their statuses
  weaverbird playtest model <dir>       a vision model plays the game
  weaverbird feedback ...               fetch, triage, approve player feedback
  weaverbird patch start <dir>          ship an approved item as a patch
  weaverbird pipeline ...               stage, gates, advance
  weaverbird finish check|report <dir>  the evidence-based finish
  weaverbird ledger ...                 what a game has cost
  weaverbird provenance ...             how its assets were made
  weaverbird connectors ...             what outside services may do
  weaverbird settings check <file>      validate the global budgets
  weaverbird identity                   the Weaverbird look, in short

Run 'weaverbird <command> --help' for the details of any command.
"""


# Kept for compatibility: the v0.1/v0.2 overview is now the help text.
overview_text = help_text


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in ("help", "-h", "--help"):
        print(help_text(), end="")
        return 0
    if argv and argv[0] == "jobs":
        # `weaverbird jobs <workspace>` takes a path, not a
        # subcommand — handled by its own small parser.
        return cmd_jobs(argv[1:])
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        return cmd_launch(args)
    sub_name = None
    attr = _SUBCOMMAND_ATTRS.get(args.command)
    if attr:
        sub_name = getattr(args, attr, None)
        if not sub_name:
            parser.parse_args([args.command, "--help"])
            return 0
    handler = _HANDLERS.get((args.command, sub_name))
    if handler is None:
        parser.print_help()
        return 0
    return handler(args)


if __name__ == "__main__":
    sys.exit(main())
