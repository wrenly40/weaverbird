"""The pipeline state machine.

A game moves through the factory one stage at a time, in order:
scope → skeleton → assets → build → headless_test → playtest →
finished → approval → publish → live.

State is persisted as JSON in the project's .weaverbird/ directory.
Advancing is only ever to the next stage, and only when the current
stage's gate passes. Where infrastructure can check a gate for real, it
does: leaving scope needs a complete, approved scope file, and reaching
or leaving finished needs the whole definition-of-done checklist true.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from . import scope as scope_module

STAGES = [
    "scope",
    "skeleton",
    "assets",
    "build",
    "headless_test",
    "playtest",
    "finished",
    "approval",
    "publish",
    "live",
]

# The definition of done (design stage 6). Every item must be true
# before a game can be called finished.
DEFINITION_OF_DONE = {
    "scenario_tests_pass": False,
    "no_crashes_in_full_playthroughs": False,
    "performance_targets_met": False,
    "menus_save_settings_complete": False,
    "game_winnable_and_losable": False,
    "content_counts_delivered": False,
    "icon_and_polish_gate_passed": False,
}


class PipelineError(Exception):
    """Raised when a pipeline advance is refused."""


def state_path_for(project_dir) -> Path:
    path = Path(project_dir)
    if path.is_file():
        return path
    return path / ".weaverbird" / "state.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_state() -> dict:
    return {
        "stage": STAGES[0],
        "history": [{"stage": STAGES[0], "at": _now(), "note": "Pipeline created."}],
        "definition_of_done": dict(DEFINITION_OF_DONE),
    }


def init_state(project_dir) -> dict:
    """Create the pipeline state for a project (keeping any existing one)."""
    path = state_path_for(project_dir)
    if path.exists():
        return load_state(project_dir)
    state = new_state()
    save_state(project_dir, state)
    return state


def load_state(project_dir) -> dict:
    path = state_path_for(project_dir)
    if not path.exists():
        raise PipelineError(
            f"No pipeline state found at {path}. Run 'weaverbird init' first."
        )
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise PipelineError(f"Pipeline state is not valid JSON: {path}") from exc
    if state.get("stage") not in STAGES:
        raise PipelineError(f"Pipeline state has an unknown stage: {state.get('stage')!r}")
    state.setdefault("definition_of_done", dict(DEFINITION_OF_DONE))
    state.setdefault("history", [])
    return state


def save_state(project_dir, state: dict) -> None:
    path = state_path_for(project_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")


# --- Stage-handler registry -------------------------------------------------
# Future platform modules (skeleton builder, asset factory, test runners,
# publishers) plug into the pipeline by registering a handler for their
# stage. A handler is called as handler(project_dir, state) when the
# pipeline tries to leave that stage, after the built-in gate passes.
# Returning False — or raising PipelineError — blocks the advance.
# Handlers are optional: with none registered, the built-in gates are
# the only checks, which is how the v0.1 infrastructure runs.

_HANDLERS: dict[str, object] = {}


def register_handler(stage: str, fn) -> None:
    """Register a stage handler callable for a pipeline stage."""
    if stage not in STAGES:
        raise PipelineError(
            f"Unknown stage {stage!r}. Known stages: {', '.join(STAGES)}"
        )
    if not callable(fn):
        raise PipelineError("A stage handler must be callable.")
    _HANDLERS[stage] = fn


def get_handler(stage: str):
    """The handler registered for a stage, or None."""
    return _HANDLERS.get(stage)


def clear_handlers() -> None:
    """Remove all registered handlers (mainly for tests)."""
    _HANDLERS.clear()


def current_stage(project_dir) -> str:
    return load_state(project_dir)["stage"]


def next_stage(stage: str) -> str | None:
    index = STAGES.index(stage)
    if index + 1 >= len(STAGES):
        return None
    return STAGES[index + 1]


def set_definition_of_done(project_dir, key: str, value: bool = True) -> dict:
    """Set one definition-of-done item in a project's state."""
    if key not in DEFINITION_OF_DONE:
        raise PipelineError(
            f"Unknown definition-of-done item {key!r}. "
            f"Known items: {', '.join(DEFINITION_OF_DONE)}"
        )
    state = load_state(project_dir)
    state["definition_of_done"][key] = bool(value)
    save_state(project_dir, state)
    return state


def _gate(project_dir, state: dict, from_stage: str, to_stage: str) -> list[str]:
    """Reasons the move from_stage -> to_stage is blocked ([] = allowed)."""
    reasons: list[str] = []
    if from_stage == "scope":
        scope_file = scope_module.scope_path_for(project_dir)
        if not scope_file.exists():
            reasons.append(f"No scope file found at {scope_file}.")
        else:
            parsed = scope_module.load_scope(scope_file)
            if not parsed.is_complete:
                missing = ", ".join(f.name for f in parsed.missing_fields)
                reasons.append(f"Scope is incomplete — still missing: {missing}.")
            elif not parsed.is_approved:
                reasons.append("Scope is complete but not approved yet.")
    if to_stage == "finished" or from_stage == "finished":
        dod = state.get("definition_of_done", {})
        undone = [key for key, done in DEFINITION_OF_DONE.items() if not dod.get(key)]
        if to_stage == "finished" and undone:
            reasons.append(
                "Definition of done is not fully met — still open: "
                + ", ".join(undone)
                + "."
            )
        elif from_stage == "finished" and undone:
            reasons.append(
                "Definition of done has items no longer true: "
                + ", ".join(undone)
                + "."
            )
    return reasons


def gate_reasons(project_dir, state: dict | None = None) -> list[str]:
    """Why the next advance is blocked, for the project's current stage."""
    state = state or load_state(project_dir)
    following = next_stage(state["stage"])
    if following is None:
        return ["This is the final stage."]
    return _gate(project_dir, state, state["stage"], following)


def advance(project_dir) -> dict:
    """Move the pipeline exactly one stage forward, if its gate passes."""
    state = load_state(project_dir)
    current = state["stage"]
    following = next_stage(current)
    if following is None:
        raise PipelineError(f"Already at the final stage ({current}); cannot advance.")
    reasons = _gate(project_dir, state, current, following)
    if reasons:
        raise PipelineError(
            f"Cannot advance from {current} to {following}: " + " ".join(reasons)
        )
    handler = _HANDLERS.get(current)
    if handler is not None:
        try:
            outcome = handler(project_dir, state)
        except PipelineError:
            raise
        except Exception as exc:  # a failing stage blocks, never crashes past
            raise PipelineError(
                f"Stage handler for {current} failed: {exc}"
            ) from exc
        if outcome is False:
            raise PipelineError(
                f"Cannot advance from {current} to {following}: "
                f"the {current} stage handler did not pass."
            )
    state["stage"] = following
    state["history"].append(
        {"stage": following, "at": _now(), "note": f"Advanced from {current}."}
    )
    save_state(project_dir, state)
    return state


def advance_to(project_dir, target: str) -> dict:
    """Move to a named stage — refused unless it is the very next stage."""
    state = load_state(project_dir)
    following = next_stage(state["stage"])
    if target != following:
        raise PipelineError(
            f"Out-of-order advance refused: the pipeline is at "
            f"{state['stage']} and can only move to {following}, not {target}. "
            "Stages are completed one at a time, in order."
        )
    return advance(project_dir)
