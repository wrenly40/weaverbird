"""The Weaverbird command-line front end.

This module is deliberately thin: it parses arguments, calls the
library in this package, and prints plain results. All of the logic
lives in the library modules (settings, scope, connectors, ledger,
provenance, state, vcs, godot), so a future desktop or web platform
and a background job runner can call the same core.
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
from . import connectors as connectors_module
from . import godot as godot_module
from . import identity as identity_module
from . import ledger as ledger_module
from . import provenance as provenance_module
from . import scope as scope_module
from . import settings as settings_module
from . import skeleton as skeleton_module
from . import state as state_module
from . import vcs as vcs_module


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


def cmd_init(args) -> int:
    target = Path(args.dir)
    if target.exists() and any(target.iterdir()) and not args.force:
        return _fail(
            f"Refusing to initialise in {target}: the directory is not empty. "
            "Use --force to scaffold anyway."
        )
    target.mkdir(parents=True, exist_ok=True)
    created = []

    scope_dest = target / "SCOPE.md"
    scope_dest.write_text(template_text("SCOPE.md"), encoding="utf-8")
    created.append("SCOPE.md (scope interview file — fill every REQUIRED field)")

    connectors_dest = target / "connectors.yaml"
    connectors_dest.write_text(template_text("connectors.yaml"), encoding="utf-8")
    created.append("connectors.yaml (connector registry for this project)")

    if args.with_settings:
        settings_dest = target / "global-settings.yaml"
        settings_dest.write_text(template_text("global-settings.yaml"), encoding="utf-8")
        created.append("global-settings.yaml (global budgets — fill in the REQUIRED budget fields)")

    state_dir = target / ".weaverbird"
    state_dir.mkdir(parents=True, exist_ok=True)
    state_module.init_state(target)
    created.append(".weaverbird/state.json (pipeline state, at stage: scope)")
    provenance_file = provenance_module.provenance_path_for(target)
    if not provenance_file.exists():
        provenance_file.write_text(json.dumps({"records": []}, indent=2) + "\n", encoding="utf-8")
    created.append(".weaverbird/provenance.json (asset provenance log, empty)")
    ledger_file = ledger_module.ledger_path_for(target)
    ledger_file.touch(exist_ok=True)
    created.append(".weaverbird/ledger.jsonl (token/cost ledger, empty)")

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
    print(f"Definition of done: {len(done)} of {len(dod)} items true")
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


def cmd_pipeline_advance(args) -> int:
    # Stage handlers live in the library; registering them here means
    # advancing out of the scope stage runs the skeleton builder
    # through the normal gate machinery.
    skeleton_module.register_stage_handlers()
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
        description="Weaverbird — a game factory on top of Godot (infrastructure).",
    )
    parser.add_argument("--version", action="version", version=f"weaverbird {__version__}")
    sub = parser.add_subparsers(dest="command")

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

    return parser


_HANDLERS = {
    ("doctor", None): cmd_doctor,
    ("identity", None): cmd_identity,
    ("init", None): cmd_init,
    ("scope", "check"): cmd_scope_check,
    ("scope", "questions"): cmd_scope_questions,
    ("scope", "approve"): cmd_scope_approve,
    ("settings", "check"): cmd_settings_check,
    ("connectors", "list"): cmd_connectors_list,
    ("connectors", "check"): cmd_connectors_check,
    ("ledger", "add"): cmd_ledger_add,
    ("ledger", "show"): cmd_ledger_show,
    ("ledger", "check"): cmd_ledger_check,
    ("provenance", "add"): cmd_provenance_add,
    ("provenance", "list"): cmd_provenance_list,
    ("provenance", "disclosure"): cmd_provenance_disclosure,
    ("pipeline", "status"): cmd_pipeline_status,
    ("pipeline", "advance"): cmd_pipeline_advance,
    ("skeleton", "build"): cmd_skeleton_build,
}

_SUBCOMMAND_ATTRS = {
    "scope": "scope_command",
    "settings": "settings_command",
    "connectors": "connectors_command",
    "ledger": "ledger_command",
    "provenance": "provenance_command",
    "pipeline": "pipeline_command",
    "skeleton": "skeleton_command",
}


def overview_text() -> str:
    """The bare `weaverbird` overview: the workflow and the main commands."""
    return f"""Weaverbird {__version__} — a game factory on top of Godot

A prompt becomes a game, one stage at a time:

  scope → skeleton → assets → build → headless test → playtest →
  finished → approval → publish → live

Start here:
  weaverbird doctor                    check your setup
  weaverbird init <dir>                start a new game project
  weaverbird scope questions <dir>     the next scope interview round
  weaverbird scope check <dir>         what the scope still needs

Working with a project:
  weaverbird scope approve <dir> --by <name>
  weaverbird skeleton build <dir>      build the grey-box Godot skeleton
  weaverbird pipeline status <dir>     where the game is, and its gates
  weaverbird pipeline advance <dir>    move one stage, if the gate passes
  weaverbird ledger show <dir>         what the game has cost so far
  weaverbird provenance list <dir>     how its assets were made

The factory itself:
  weaverbird settings check <file>     validate the global budgets
  weaverbird connectors list           what outside services may do
  weaverbird identity                  the Weaverbird look, in short

Run 'weaverbird <command> --help' for the details of any command.
"""


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        print(overview_text(), end="")
        return 0
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
