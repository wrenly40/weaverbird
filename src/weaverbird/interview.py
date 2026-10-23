"""The conversational scope interview — the scope worker's front end.

Before 0.8.0 the scope interview was a form (and, from the CLI, a
printed list of questions). The scope file discipline is unchanged
— created live, every REQUIRED field filled before the build, no
silent defaults, one approval at the end — but the filling now
happens as a conversation, driven by one session engine that the
app chat and the CLI line mode share:

- Session state persists per project at
  ``.weaverbird/interview.json``: the turns so far, which question
  group is pending, and the style samples once generated.
- **Deterministic mode** (no brain configured) asks the next group
  of questions in plain language, generated from the missing
  REQUIRED fields, and parses answers conservatively: ``Field:
  value`` lines, keyword matches for the known-choice fields
  (dimension, scope class, telemetry), and plain lines assigned to
  the first pending field whose validation accepts them. Every
  captured field is reflected back ("Got it — Title: …") before
  the conversation moves on.
- **Brain mode** (a usable cheap tier) sends each turn to the
  model with the transcript and the schema of still-empty fields;
  the model answers strict JSON ``{fields, next_message}``. Fields
  are validated against the scope schema before anything is
  written: unknown fields and invalid values are dropped and
  re-asked, never silently written. Any brain failure falls back
  to the deterministic handling for that turn and the conversation
  continues. Token use goes through brain.py as usual (ledger +
  global caps).
- The project's Original prompt, when one is stored, is fed
  through the existing prompt extraction FIRST (brain.py's
  interview_from_prompt), so the conversation never re-asks what
  the prompt already stated. Filled fields are never overwritten
  by parsing or extraction — the one exception is an explicit
  style pick, which is a decision: it writes the chosen sample's
  style lock, palette and shading rules.
- **Style samples**: when the interview reaches the style lock,
  the factory generates sample cards (assets_gen.style_sample_svg
  — small scenes drawn from each candidate palette by the asset
  code, never static images shipped in the package) and caches
  them under ``.weaverbird/style_samples/``. The maker picks one
  by clicking it, typing its name, or typing its number; the pick
  writes Style lock + Palette + proportions into SCOPE.md.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from . import assets_gen as assets_module
from . import brain as brain_module
from . import scope as scope_module

SESSION_REL = ".weaverbird/interview.json"
SAMPLES_DIR_REL = ".weaverbird/style_samples"

STYLE_FIELD = "Style sample chosen (id / link to approved sample)"
PALETTE_FIELD = "Palette"
PROPS_FIELD = "Proportions / outline / shading rules"

_OPENING = (
    "Let's scope your game. I'll ask a few questions at a time, "
    "grouped by topic. Answer in your own words — one answer per "
    "message, several answers on separate lines, or 'Field: value' "
    "when you want to be exact. Nothing is built until every "
    "REQUIRED field is filled and you approve the scope."
)

_ANSWER_HINT = (
    "One answer per line, or 'Field: value'. I will read back "
    "everything I capture."
)


class InterviewError(Exception):
    """Raised when an interview cannot run (no scope file, …)."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Session persistence

def session_path_for(project_dir) -> Path:
    return Path(project_dir) / SESSION_REL


def load_session(project_dir) -> dict | None:
    path = session_path_for(project_dir)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return data if isinstance(data, dict) else None


def _save_session(project_dir, session: dict) -> None:
    path = session_path_for(project_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    session["updated_at"] = _now()
    path.write_text(json.dumps(session, indent=2) + "\n",
                    encoding="utf-8")


def _new_session() -> dict:
    return {
        "version": 1,
        "started_at": _now(),
        "updated_at": _now(),
        "turns": [],
        "pending_fields": [],
        "prompt_extracted": False,
        "samples": None,
    }


def _scope_file(project_dir) -> Path:
    scope_file = scope_module.scope_path_for(project_dir)
    if not scope_file.exists():
        raise InterviewError(f"No scope file found at {scope_file}.")
    return scope_file


def _load_scope(project_dir):
    return scope_module.load_scope(_scope_file(project_dir))


# ---------------------------------------------------------------------------
# Field validation — the schema both modes write through.
# Returns the canonical value to store, or None when the answer is
# not usable for the field (the field is then re-asked).

_DIMENSION_RE = re.compile(r"\b([23])\s*d\b")
_SCOPE_CLASS_RE = re.compile(r"\b(micro|small|medium)\b")
_TELEMETRY_RE = re.compile(r"\b(yes|no)\b")
_HEX_RE = re.compile(r"#[0-9a-fA-F]{6}\b|#[0-9a-fA-F]{3}\b")


def validate_value(field_name: str, raw) -> str | None:
    value = scope_module._clean_value(str(raw if raw is not None else ""))
    if not value:
        return None
    lowered = value.lower()
    if field_name == "Dimension":
        match = _DIMENSION_RE.search(lowered)
        return f"{match.group(1)}D" if match else None
    if field_name == "Scope class":
        match = _SCOPE_CLASS_RE.search(lowered)
        return match.group(1).capitalize() if match else None
    if field_name == "Telemetry":
        match = _TELEMETRY_RE.search(lowered)
        return match.group(1) if match else None
    if field_name == STYLE_FIELD:
        sample = style_pick_from_text(value)
        if sample is not None:
            return style_field_value(sample)
        if lowered.startswith("http://") or lowered.startswith("https://"):
            return value
        return None
    if field_name == PALETTE_FIELD:
        return value if _HEX_RE.search(value) else None
    return value


# ---------------------------------------------------------------------------
# Style samples

def style_field_value(sample: dict) -> str:
    return f"{sample['id']} — {sample['name']} (generated style sample)"


def style_pick_from_text(text: str) -> dict | None:
    """The style sample a message picks, or None: by exact id, by
    number (1–4, as listed), by name, or by a plain keyword."""
    cleaned = str(text or "").strip().lower()
    if not cleaned:
        return None
    samples = assets_module.style_samples()
    for sample in samples:
        if cleaned == sample["id"] or sample["id"] in cleaned:
            return sample
    if cleaned.isdigit():
        index = int(cleaned)
        if 1 <= index <= len(samples):
            return samples[index - 1]
    for sample in samples:
        if sample["name"].lower() in cleaned:
            return sample
    keywords = {
        "bright-chunky": ("bright", "chunky"),
        "soft-pastel": ("pastel", "soft"),
        "bold-neon-night": ("neon", "night"),
        "warm-storybook": ("storybook", "warm"),
    }
    for sample in samples:
        if any(word in cleaned for word in keywords[sample["id"]]):
            return sample
    return None


def ensure_samples(project_dir, session: dict) -> list[dict]:
    """Generate the style sample cards for this project (cached).

    The SVGs are drawn at this moment from the candidate palettes
    by assets_gen and written under .weaverbird/style_samples/;
    the session keeps the samples' data for the chat and the CLI.
    """
    if session.get("samples"):
        return session["samples"]
    project = Path(project_dir)
    samples = []
    for sample in assets_module.style_samples():
        rel = f"{SAMPLES_DIR_REL}/{sample['id']}.svg"
        dest = project / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(assets_module.style_sample_svg(sample),
                        encoding="utf-8")
        samples.append({
            "id": sample["id"],
            "name": sample["name"],
            "blurb": sample["blurb"],
            "palette": dict(sample["palette"]),
            "palette_text": assets_module.sample_palette_text(sample),
            "rules": sample["rules"],
            "svg": rel,
        })
    session["samples"] = samples
    return samples


def apply_style_pick(project_dir, sample: dict) -> dict:
    """Write a style pick into the scope: style lock, palette and
    proportions/shading rules — the sample IS the decision, so
    these three fields take its values even if an earlier answer
    filled them from the prompt."""
    scope_file = _scope_file(project_dir)
    parsed = scope_module.load_scope(scope_file)
    names = {f.name for f in parsed.fields}
    applied = {}
    writes = (
        (STYLE_FIELD, style_field_value(sample)),
        (PALETTE_FIELD, assets_module.sample_palette_text(sample)),
        (PROPS_FIELD, sample["rules"]),
    )
    for name, value in writes:
        if name in names:
            scope_module.set_field_value(scope_file, name, value)
            applied[name] = value
    return applied


# ---------------------------------------------------------------------------
# Question groups

def pending_group(parsed) -> tuple[str, list] | None:
    """The next group of missing fields: (section, [ScopeField])
    in file order, or None when the scope is complete."""
    groups: dict[str, list] = {}
    order: list[str] = []
    for fld in parsed.missing_fields:
        if fld.section not in groups:
            groups[fld.section] = []
            order.append(fld.section)
        groups[fld.section].append(fld)
    if not order:
        return None
    section = order[0]
    return section, groups[section]


def _group_question_text(section: str, fields: list,
                         samples: list[dict] | None) -> str:
    lines = [f"{section}:"]
    for fld in fields:
        lines.append(f"- {scope_module.question_for(fld)}  [{fld.name}]")
    if samples:
        lines.append("")
        lines.append("Style samples — generated just now from four "
                     "candidate palettes. Pick one by clicking it, "
                     "typing its name, or typing its number:")
        for index, sample in enumerate(samples, start=1):
            lines.append(
                f"{index}. {sample['id']} — {sample['name']}: "
                f"{sample['palette_text']}. {sample['blurb']}")
    else:
        lines.append(_ANSWER_HINT)
    return "\n".join(lines)


def _present_group(project_dir, session: dict, parsed) -> dict | None:
    """Build (do not append) the assistant turn asking the current
    pending group — or the completion turn. Style groups carry
    the generated samples."""
    group = pending_group(parsed)
    if group is None:
        return {
            "role": "assistant", "kind": "complete", "at": _now(),
            "text": (
                "Every REQUIRED field is filled — the scope is "
                "complete. Approve it and the factory can start "
                "building: in the app, the Approve button in the "
                "scope panel; from the CLI, 'weaverbird scope "
                "approve <project> --by <your name>'."),
            "fields": [],
        }
    section, fields = group
    names = [f.name for f in fields]
    samples = None
    if STYLE_FIELD in names:
        samples = ensure_samples(project_dir, session)
    turn = {
        "role": "assistant",
        "kind": "samples" if samples else "question",
        "at": _now(),
        "text": _group_question_text(section, fields, samples),
        "fields": names,
    }
    if samples:
        turn["samples"] = [s["id"] for s in samples]
    session["pending_fields"] = names
    return turn


def _append_turn(session: dict, turn: dict) -> None:
    session.setdefault("turns", []).append(turn)


def _maybe_present(session: dict, project_dir, parsed,
                   force: bool = False) -> None:
    """Append the current question turn unless the last assistant
    turn already asks exactly this group."""
    turn = _present_group(project_dir, session, parsed)
    if turn is None:
        return
    if not force:
        for past in reversed(session.get("turns", [])):
            if past.get("role") != "assistant":
                continue
            if past.get("kind") in ("question", "samples", "complete") \
                    and past.get("fields", []) == turn.get("fields", []) \
                    and past.get("kind") == turn.get("kind"):
                return
            break
    _append_turn(session, turn)


# ---------------------------------------------------------------------------
# The two answer engines

def _apply_fields(project_dir, answers: dict) -> tuple[dict, list]:
    """Write validated answers for still-empty fields.
    Returns (applied, dropped field names)."""
    scope_file = _scope_file(project_dir)
    parsed = scope_module.load_scope(scope_file)
    empty = {f.name for f in parsed.missing_fields}
    applied: dict = {}
    dropped: list[str] = []
    for name, raw in answers.items():
        if name not in empty:
            dropped.append(name)
            continue
        canonical = validate_value(name, raw)
        if canonical is None:
            dropped.append(name)
            continue
        if name == STYLE_FIELD:
            sample = style_pick_from_text(str(raw))
            if sample is not None:
                applied.update(apply_style_pick(project_dir, sample))
                continue
        scope_module.set_field_value(scope_file, name, canonical)
        applied[name] = canonical
    return applied, dropped


def _deterministic_apply(project_dir, parsed, text: str) -> tuple[dict, list]:
    """Parse one message without a model: 'Field: value' lines
    first, then plain lines walked across the pending group's
    fields in order — a line lands on the first field whose
    validation accepts it (so prose never becomes a Dimension,
    and a bare '2D' never becomes a Genre)."""
    scope_file = _scope_file(project_dir)
    empty = {f.name: f for f in parsed.missing_fields}
    answers: dict = {}
    dropped: list[str] = []
    plain_lines: list[str] = []
    for raw_line in str(text).splitlines():
        line = raw_line.strip()
        if not line:
            continue
        match = re.match(r"^([^:]{2,80}):\s*(.+)$", line)
        named = None
        if match:
            wanted = match.group(1).strip().lower()
            for name in empty:
                if name.lower() == wanted:
                    named = name
                    break
        if named is not None:
            canonical = validate_value(named, match.group(2))
            if canonical is None:
                dropped.append(named)
            else:
                answers[named] = canonical
        else:
            plain_lines.append(line)
    group = pending_group(parsed)
    group_names = [f.name for f in group[1]] if group else []
    remaining = [n for n in group_names if n not in answers]
    for line in plain_lines:
        placed = False
        for name in list(remaining):
            canonical = validate_value(name, line)
            if canonical is not None:
                answers[name] = canonical
                remaining.remove(name)
                placed = True
                break
        if not placed:
            # The whole message as one answer to the group's first
            # still-open free field is the last resort — handled by
            # the walk above for single lines; extra lines are kept
            # in the transcript and simply not captured.
            pass
    applied: dict = {}
    for name, canonical in answers.items():
        if name == STYLE_FIELD:
            sample = style_pick_from_text(canonical)
            if sample is not None:
                applied.update(apply_style_pick(project_dir, sample))
                continue
        scope_module.set_field_value(scope_file, name, canonical)
        applied[name] = canonical
    return applied, dropped


_BRAIN_SYSTEM = (
    "You are Weaverbird's scope interviewer, talking with the "
    "maker of a game while their scope file is filled in live. "
    "Answer with ONE JSON object only — no prose, no code fences: "
    "{\"fields\": {...}, \"next_message\": \"...\"}. 'fields' maps "
    "exact field names from the schema given to short answers the "
    "maker gave or directly implied in the conversation — never "
    "invent details, and leave a field out rather than guess. "
    "'next_message' is your reply to the maker: briefly reflect "
    "what you captured, then ask about the most important fields "
    "still missing, in plain friendly language, grouped naturally."
)


def _brain_turn(project_dir, session: dict, parsed, text: str,
                settings_path=None) -> tuple[dict, list, str] | None:
    """One brain-mode turn. Returns (applied, dropped,
    next_message), or None when the brain is unavailable or the
    call failed — the caller falls back to the deterministic
    engine for this turn and the conversation continues."""
    _, data = brain_module.resolve_settings(settings_path, project_dir)
    status = brain_module.tier_status(data, "cheap") if data else None
    if status is None or not status["usable"]:
        return None
    missing = parsed.missing_fields
    schema = [{"field": f.name, "section": f.section,
               "question": scope_module.question_for(f)}
              for f in missing]
    transcript = []
    for turn in session.get("turns", [])[-10:]:
        transcript.append(f"{turn.get('role', '?')}: {turn.get('text', '')}")
    content = (
        "Conversation so far:\n" + "\n".join(transcript)
        + "\n\nThe maker's latest message:\n" + text
        + "\n\nFields still empty (name / section / what it asks):\n"
        + "\n".join(f"- {s['field']} [{s['section']}]: {s['question']}"
                    for s in schema)
        + "\n\nReturn the JSON object now.")
    try:
        result = brain_module.complete(
            project_dir, "cheap",
            [{"role": "system", "content": _BRAIN_SYSTEM},
             {"role": "user", "content": content}],
            stage="scope", settings_path=settings_path, max_tokens=800)
    except brain_module.BrainBudgetError:
        return None
    if not result.ok:
        return None
    payload = brain_module.extract_json(result.text)
    if not payload:
        return None
    raw_fields = payload.get("fields")
    if not isinstance(raw_fields, dict):
        raw_fields = {}
    applied, dropped = _apply_fields(project_dir, raw_fields)
    next_message = payload.get("next_message")
    if not isinstance(next_message, str) or not next_message.strip():
        next_message = ""
    return applied, dropped, next_message.strip()


# ---------------------------------------------------------------------------
# The public engine

def start(project_dir, settings_path=None) -> dict:
    """Open the interview for a project: create the session if
    needed, feed the stored Original prompt through extraction
    once, and make sure the current question group is on the
    thread. Returns the public session state."""
    project = Path(project_dir)
    session = load_session(project) or _new_session()
    parsed = _load_scope(project)
    if parsed.original_prompt and not session.get("prompt_extracted"):
        outcome = brain_module.interview_from_prompt(
            project, parsed.original_prompt,
            settings_path=settings_path)
        session["prompt_extracted"] = True
        applied = outcome.get("applied") or {}
        if applied:
            _append_turn(session, {
                "role": "assistant", "kind": "info", "at": _now(),
                "text": "From your prompt I filled in: "
                        + "; ".join(f"{name}: {value}"
                                    for name, value in applied.items())
                        + ". I will not ask about those again.",
                "fields": [],
            })
        parsed = _load_scope(project)
    if not session.get("turns"):
        _append_turn(session, {
            "role": "assistant", "kind": "info", "at": _now(),
            "text": _OPENING, "fields": [],
        })
    _maybe_present(session, project, parsed)
    _save_session(project, session)
    return public_state(project, session)


def respond(project_dir, text: str, settings_path=None,
            reporter=None) -> dict:
    """One conversational turn: record the maker's message, apply
    what it answers (brain mode when usable, deterministic
    otherwise, deterministic again when the brain fails), reflect
    the captures, and ask the next group. An empty message just
    (re)presents the current state — it is how the app's Begin
    button and a bare CLI start work. Returns the public state,
    with this turn's 'applied' and 'dropped' included."""
    project = Path(project_dir)
    start(project, settings_path=settings_path)
    session = load_session(project) or _new_session()
    text = (text or "").strip()
    applied: dict = {}
    dropped: list[str] = []
    next_message = ""
    brain_used = False
    if text:
        _append_turn(session, {
            "role": "user", "kind": "answer", "at": _now(),
            "text": text, "fields": [],
        })
        parsed = _load_scope(project)
        # An explicit style pick is a decision, not an extraction —
        # honoured in both modes before anything else runs, but
        # only while the style lock is the pending question (so an
        # answer like "warm chiptune" for the audio mood can never
        # be mistaken for a re-pick).
        sample = None
        group = pending_group(parsed)
        if group and STYLE_FIELD in [f.name for f in group[1]]:
            sample = style_pick_from_text(text)
        if sample is not None:
            applied = apply_style_pick(project, sample)
        else:
            outcome = _brain_turn(project, session, parsed, text,
                                  settings_path=settings_path)
            if outcome is not None:
                applied, dropped, next_message = outcome
                brain_used = True
            else:
                parsed = _load_scope(project)
                applied, dropped = _deterministic_apply(
                    project, parsed, text)
        if reporter is not None:
            reporter(f"Interview turn recorded "
                     f"({len(applied)} field(s) captured)")
        bits = []
        if applied:
            bits.append("Got it — " + "; ".join(
                f"{name}: {value}" for name, value in applied.items())
                + ".")
        if dropped:
            bits.append(
                "I could not use the answer for: "
                + ", ".join(sorted(set(dropped)))
                + " — I will ask about those again below.")
        if bits or next_message:
            _append_turn(session, {
                "role": "assistant", "kind": "reflection",
                "at": _now(),
                "text": "\n".join(
                    bit for bit in (next_message, "\n".join(bits))
                    if bit),
                "fields": sorted(applied),
            })
        parsed = _load_scope(project)
        _maybe_present(session, project, parsed, force=bool(applied))
        _save_session(project, session)
    out = public_state(project, load_session(project) or session)
    out["applied"] = applied
    out["dropped"] = dropped
    out["brain_used"] = brain_used
    return out


def summary_text(state: dict) -> str:
    """A one-line summary of a turn's outcome, for job results."""
    applied = state.get("applied") or {}
    bits = []
    if applied:
        bits.append("Captured: " + ", ".join(sorted(applied)) + ".")
    if state.get("complete"):
        bits.append("The scope is complete — ready to approve.")
    else:
        bits.append(f"{state.get('missing_count', '?')} required "
                    "field(s) still to go.")
    return " ".join(bits)


def public_state(project_dir, session: dict | None = None) -> dict:
    """Everything a surface needs: the thread, the live scope
    panel (every field, filled or not), progress, and the
    samples once generated."""
    project = Path(project_dir)
    if session is None:
        session = load_session(project) or _new_session()
    parsed = _load_scope(project)
    fields = [{
        "name": f.name, "section": f.section, "value": f.value,
        "filled": f.filled,
    } for f in parsed.fields]
    return {
        "turns": list(session.get("turns", [])),
        "fields": fields,
        "missing": [f.name for f in parsed.missing_fields],
        "missing_count": len(parsed.missing_fields),
        "complete": parsed.is_complete,
        "approved": parsed.is_approved,
        "approval": parsed.approval,
        "samples": session.get("samples"),
        "pending_fields": list(session.get("pending_fields", [])),
        "started": bool(session.get("turns")),
    }
