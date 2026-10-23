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
- The project's Original prompt, when one is stored, is
  consumed ONCE, before any question is asked: brain extraction
  first (brain.py's interview_from_prompt), then — for fields
  still empty — explicit 'Field: value' lines parsed
  deterministically (no other guessing without a brain). The
  interview opens with a recap of what the prompt filled; the
  first question group covers only the gaps, and when the
  prompt covered everything no questions are asked at all.
  Filled fields are never overwritten
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
        "style_reopen": False,
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
_TELEMETRY_YES_RE = re.compile(r"\byes\b")
# "None" is a complete, natural answer to "is telemetry allowed?"
# (dogfood finding D1): it means no telemetry. Before 0.10.1 the
# validator only accepted yes/no, so an explicit "None" was
# dropped and the field re-asked forever, silently.
_TELEMETRY_NO_RE = re.compile(r"\b(?:no|none|off|never)\b")
_HEX_RE = re.compile(r"#[0-9a-fA-F]{6}\b|#[0-9a-fA-F]{3}\b")


def validate_value(field_name: str, raw, authoritative: bool = False) -> str | None:
    """The canonical value to store for a field, or None when the
    answer is not usable for it (the field is then re-asked).

    ``authoritative`` marks a value the maker submitted as an
    answer in an interview turn: it is taken as written (only
    whitespace is tidied) and the template-placeholder filtering
    in scope._clean_value does NOT apply — a maker's "None" is a
    real answer, not an unspecified field. Non-authoritative
    values (template defaults, prompt-extraction and brain
    guesses) keep the placeholder filtering, where hint-shaped
    text genuinely means "not specified"."""
    text = str(raw if raw is not None else "")
    if authoritative:
        value = text.strip().rstrip(":").strip()
    else:
        value = scope_module._clean_value(text)
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
        yes = _TELEMETRY_YES_RE.search(lowered)
        no = _TELEMETRY_NO_RE.search(lowered)
        if yes and no:
            return "yes" if yes.start() < no.start() else "no"
        if yes:
            return "yes"
        if no:
            return "no"
        return None
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


CHANGE_STYLE_MESSAGE = "change-style"


def style_choice(project_dir) -> str:
    """The style decision currently standing in the scope: the
    chosen sample's id, "custom" when the style field is filled
    by anything else (a link the maker pasted, a hand edit), or
    "" when no choice stands. Derived from the scope itself —
    the choice is a scope decision, so it survives session
    resets and app restarts."""
    try:
        parsed = _load_scope(project_dir)
    except InterviewError:
        return ""
    for fld in parsed.fields:
        if fld.name == STYLE_FIELD:
            if not fld.filled:
                return ""
            candidate = fld.value.split(" — ")[0].strip()
            known = {s["id"] for s in assets_module.style_samples()}
            return candidate if candidate in known else "custom"
    return ""


def _sentence(text: str) -> str:
    """Close a sentence: append '.' only when the text does not
    already end in sentence punctuation (a captured value that
    ends in '.' must not double it in a recap)."""
    stripped = text.rstrip()
    if stripped.endswith((".", "!", "?")):
        return text
    return text + "."


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


def _present_group(project_dir, session: dict, parsed,
                   only_field=None) -> dict | None:
    """Build (do not append) the assistant turn asking the current
    pending group — or the completion turn. Style groups carry
    the generated samples. With only_field, present exactly that
    one field's group (the Change style re-open: the style lock
    is already filled, so no missing-group logic would ask it)."""
    if only_field is not None:
        group = (only_field.section, [only_field])
    else:
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


def _why_hint(field_name: str) -> str:
    """What a field needs, in one clause — used when an answer
    could not be taken, so the maker hears why, not silence."""
    hints = {
        "Dimension": "it needs '2D' or '3D'",
        "Scope class": "it needs Micro, Small or Medium",
        "Telemetry": "it needs yes or no ('none' counts as no)",
        PALETTE_FIELD: "it needs at least one hex colour, "
                       "like #6C4CF1",
        STYLE_FIELD: "it needs a style sample's name or number, "
                     "or a link to an approved sample",
    }
    return hints.get(field_name, "")


def _field_named(typed: str, names) -> str | None:
    """The scope field a 'Name: value' line addresses, or None:
    the full field name, its head (the part before any " (…)"
    qualifier — "Title" for "Title (working)"), or an
    unambiguous prefix of either ("AI disclosure stance" for
    "AI disclosure stance for stores (follows the allowed
    sources)" — the name a maker naturally shortens it to)."""
    wanted = str(typed or "").strip().lower()
    if not wanted:
        return None
    for name in names:
        full = name.lower()
        head = name.split(" (")[0].lower()
        if wanted in (full, head):
            return name
        if len(wanted) >= 8 and (full.startswith(wanted)
                                 or head.startswith(wanted)):
            return name
    return None


def _explicit_corrections(project_dir, parsed, text: str):
    """'Field: value' lines that CORRECT an already-filled field
    (dogfood finding D3): the composer advertises the form, but
    a correction naming a filled field used to be ignored with
    no reply at all.

    Returns (updated, dropped, unknown_names, confirmed,
    rest_text): filled-field lines are consumed here — validated
    authoritatively, written, and, when the scope is already
    approved, recorded as a dated amendment on the scope file.
    Lines naming missing fields, and everything else, pass
    through in rest_text for the answer engines, unchanged. A
    line naming NO field is reported as unknown only when it
    cannot be a plain answer instead: the scope is complete
    (nothing else could take the line), or the name is a close
    miss for a real field."""
    import difflib

    scope_file = _scope_file(project_dir)
    all_names = [f.name for f in parsed.fields]
    filled = {f.name: f.value for f in parsed.fields if f.filled}
    updated: dict = {}
    dropped: list[str] = []
    unknown: list[str] = []
    confirmed: list[str] = []
    rest: list[str] = []
    for raw_line in str(text).splitlines():
        line = raw_line.strip()
        if not line:
            continue
        match = re.match(r"^([^:]{2,80}):\s*(.+)$", line)
        named = _field_named(match.group(1), all_names) \
            if match else None
        if match and named is None:
            typed = match.group(1).strip()
            close = difflib.get_close_matches(
                typed.lower(), [n.lower() for n in all_names],
                n=1, cutoff=0.7)
            if parsed.is_complete or close:
                unknown.append(typed)
                continue
        if named is None or named not in filled:
            rest.append(line)
            continue
        canonical = validate_value(named, match.group(2),
                                   authoritative=True)
        if canonical is None:
            dropped.append(named)
            continue
        if canonical == filled[named]:
            confirmed.append(named)
            continue
        scope_module.set_field_value(scope_file, named, canonical)
        updated[named] = canonical
        if parsed.is_approved:
            scope_module.append_amendment(
                scope_file,
                f"Interview correction: {named} set to "
                f"“{canonical}” (was “{filled[named]}”).")
    return updated, dropped, unknown, confirmed, "\n".join(rest)


def _deterministic_apply(project_dir, parsed,
                         text: str) -> tuple[dict, list, list, list]:
    """Parse one message without a model: 'Field: value' lines
    first, then plain lines for the pending group. Returns
    (applied, dropped, ignored lines, uncaptured lines).

    Every value here was submitted by the maker as an answer, so
    validation is authoritative (see validate_value): an answer
    that reads like a placeholder word — "None" for Telemetry —
    still fills its field.

    With no more lines than the group has open fields, plain
    lines are walked across the fields in order — a line lands
    on the first field whose validation accepts it (so prose
    never becomes a Dimension, and a bare '2D' never becomes a
    Genre). With MORE lines than open fields, the walk could
    silently shift every answer onto the wrong field, so the
    first lines are applied positionally instead (line i to
    field i, validation still applied, an unusable line is
    dropped rather than walked onward) and the surplus lines
    are returned as ignored, for the interviewer to quote back.
    Lines no open field accepts at all come back as uncaptured,
    so the interviewer can say so instead of silently re-asking
    (D1: a maker answering "None" was looped with no explanation).
    """
    scope_file = _scope_file(project_dir)
    empty = {f.name: f for f in parsed.missing_fields}
    answers: dict = {}
    dropped: list[str] = []
    ignored: list[str] = []
    uncaptured: list[str] = []
    plain_lines: list[str] = []
    for raw_line in str(text).splitlines():
        line = raw_line.strip()
        if not line:
            continue
        match = re.match(r"^([^:]{2,80}):\s*(.+)$", line)
        named = _field_named(match.group(1), list(empty)) \
            if match else None
        if named is not None:
            canonical = validate_value(named, match.group(2),
                                       authoritative=True)
            if canonical is None:
                dropped.append(named)
            else:
                answers[named] = canonical
        else:
            plain_lines.append(line)
    group = pending_group(parsed)
    group_names = [f.name for f in group[1]] if group else []
    remaining = [n for n in group_names if n not in answers]
    if len(plain_lines) > len(remaining):
        ignored.extend(plain_lines[len(remaining):])
        for line, name in zip(plain_lines[:len(remaining)], remaining):
            canonical = validate_value(name, line, authoritative=True)
            if canonical is None:
                dropped.append(name)
            else:
                answers[name] = canonical
    else:
        for line in plain_lines:
            captured = False
            for name in list(remaining):
                canonical = validate_value(name, line,
                                           authoritative=True)
                if canonical is not None:
                    answers[name] = canonical
                    remaining.remove(name)
                    captured = True
                    break
            if not captured:
                # Kept in the transcript AND reported back as
                # uncaptured — never a silent no-op.
                uncaptured.append(line)
    applied: dict = {}
    for name, canonical in answers.items():
        if name == STYLE_FIELD:
            sample = style_pick_from_text(canonical)
            if sample is not None:
                applied.update(apply_style_pick(project_dir, sample))
                continue
        scope_module.set_field_value(scope_file, name, canonical)
        applied[name] = canonical
    return applied, dropped, ignored, uncaptured


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

_ABBREV_END_RE = re.compile(r"(?:^|\s)[A-Z]\.$")


def _tidy_marker_segment(raw: str) -> str:
    """Tidy a prompt value segment that was cut short by the NEXT
    field marker: prose like 'Title (working): Star Shepherd.
    Genre / archetype: …' leaves the sentence's full stop on the
    captured value. Strip one trailing '.' or ',' — only here,
    only for marker-terminated segments: never for a segment
    running to the end of the prompt, never for chat answers,
    never after '..' and never when the value ends in an
    abbreviation-like single capital plus stop ('Option A.')."""
    if raw.endswith("..") or _ABBREV_END_RE.search(raw):
        return raw
    if raw.endswith((".", ",")):
        return raw[:-1].strip()
    return raw


def _prompt_field_lines(parsed, prompt_text: str) -> dict:
    """Deterministic prompt consumption: explicit 'Field: value'
    statements in the starting prompt. The stored prompt is one
    whitespace-collapsed line, so this scans for exact known
    field names followed by ':' — each value runs to the next
    field marker or the end of the prompt — and validates every
    value. Nothing else is taken from the prompt without a
    brain: prose is never guessed into fields."""
    text = str(prompt_text)
    lowered = text.lower()
    empty = {f.name.lower(): f.name for f in parsed.missing_fields}
    markers: list[tuple[int, int, str]] = []
    for lowered_name, name in empty.items():
        needle = lowered_name + ":"
        index = lowered.find(needle)
        while index != -1:
            markers.append((index, index + len(needle), name))
            index = lowered.find(needle, index + 1)
    markers.sort()
    answers: dict = {}
    for i, (_start, end, name) in enumerate(markers):
        if name in answers:
            continue
        terminated = i + 1 < len(markers)
        stop = markers[i + 1][0] if terminated else len(text)
        raw = text[end:stop].strip().rstrip(";").strip()
        if terminated:
            raw = _tidy_marker_segment(raw)
        canonical = validate_value(name, raw)
        if canonical is not None:
            answers[name] = canonical
    return answers


def start(project_dir, settings_path=None) -> dict:
    """Open the interview for a project: create the session if
    needed, consume the stored Original prompt ONCE before any
    question is asked (brain extraction first, then explicit
    'Field: value' lines for fields still empty), open with a
    recap of what the prompt filled, and present the first
    question group — which covers only the gaps, or nothing at
    all when the prompt covered everything. Returns the public
    session state."""
    project = Path(project_dir)
    session = load_session(project) or _new_session()
    parsed = _load_scope(project)
    if parsed.original_prompt and not session.get("prompt_extracted"):
        outcome = brain_module.interview_from_prompt(
            project, parsed.original_prompt,
            settings_path=settings_path)
        applied = dict(outcome.get("applied") or {})
        parsed = _load_scope(project)
        explicit = _prompt_field_lines(parsed, parsed.original_prompt)
        if explicit:
            more, _dropped = _apply_fields(project, explicit)
            applied.update(more)
        session["prompt_extracted"] = True
        parsed = _load_scope(project)
        if applied:
            recap = _sentence(
                "From your prompt I filled in: "
                + "; ".join(f"{name}: {value}"
                            for name, value in applied.items()))
            if parsed.is_complete:
                recap += (" That covers everything the scope "
                          "needs, so there are no questions to "
                          "ask — check the scope panel and approve "
                          "it when you are ready.")
            else:
                recap += (" I will not ask about those again — "
                          "only about what is still missing.")
            _append_turn(session, {
                "role": "assistant", "kind": "info", "at": _now(),
                "text": recap,
                "fields": sorted(applied),
            })
    if not session.get("turns"):
        _append_turn(session, {
            "role": "assistant", "kind": "info", "at": _now(),
            "text": _OPENING, "fields": [],
        })
    _maybe_present(session, project, parsed)
    _save_session(project, session)
    return public_state(project, session)


def _is_exact_pick(text: str) -> bool:
    """True when the message is exactly a style sample's id or
    name — what a stale Pick button posts. Deliberately narrow:
    an ordinary answer that merely mentions a style word ('warm
    chiptune' for the audio mood) is a normal answer, never a
    refused pick."""
    cleaned = str(text or "").strip().lower()
    if not cleaned:
        return False
    for sample in assets_module.style_samples():
        if cleaned == sample["id"] \
                or cleaned == sample["name"].lower():
            return True
    return False


def respond(project_dir, text: str, settings_path=None,
            reporter=None) -> dict:
    """One conversational turn: record the maker's message, apply
    what it answers (brain mode when usable, deterministic
    otherwise, deterministic again when the brain fails), reflect
    the captures, and ask the next group. An empty message just
    (re)presents the current state — it is how the app's Begin
    button and a bare CLI start work. Returns the public state,
    with this turn's 'applied', 'dropped' and 'ignored' (surplus
    answer lines, set aside unshifted) included.

    The style decision locks once made: a pick is honoured only
    while the style question is the pending turn (or the choice
    has been re-opened with change-style); an exact pick arriving
    after the choice stands is refused politely and changes
    nothing."""
    project = Path(project_dir)
    start(project, settings_path=settings_path)
    session = load_session(project) or _new_session()
    text = (text or "").strip()
    applied: dict = {}
    dropped: list[str] = []
    ignored: list[str] = []
    uncaptured: list[str] = []
    updated: dict = {}
    unknown_names: list[str] = []
    confirmed: list[str] = []
    next_message = ""
    brain_used = False
    if text:
        _append_turn(session, {
            "role": "user", "kind": "answer", "at": _now(),
            "text": text, "fields": [],
        })
        parsed = _load_scope(project)
        choice = style_choice(project)

        def finish(extra: dict | None = None) -> dict:
            out = public_state(project,
                               load_session(project) or session)
            out["applied"] = applied
            out["dropped"] = dropped
            out["ignored"] = ignored
            out["uncaptured"] = uncaptured
            out["updated"] = updated
            out["unknown_fields"] = unknown_names
            out["brain_used"] = brain_used
            if extra:
                out.update(extra)
            return out

        if text == CHANGE_STYLE_MESSAGE:
            if choice:
                # Re-open the style decision as the current turn:
                # present the samples for the (filled) style field
                # itself, and let the next pick land.
                session["style_reopen"] = True
                _append_turn(session, {
                    "role": "assistant", "kind": "info", "at": _now(),
                    "text": (
                        "Of course — the style choice is open "
                        "again. Pick the sample that should stand; "
                        "picking replaces the current style lock, "
                        "palette and shading rules."),
                    "fields": [],
                })
                style_field = next(
                    (f for f in parsed.fields
                     if f.name == STYLE_FIELD), None)
                if style_field is not None:
                    turn = _present_group(
                        project, session, parsed,
                        only_field=style_field)
                    if turn is not None:
                        _append_turn(session, turn)
                _save_session(project, session)
                return finish({"style_reopened": True})
            _append_turn(session, {
                "role": "assistant", "kind": "info", "at": _now(),
                "text": (
                    "No style has been chosen yet, so there is "
                    "nothing to change — the samples arrive with "
                    "the style questions below."),
                "fields": [],
            })
            _maybe_present(session, project, parsed)
            _save_session(project, session)
            return finish()

        # 'Field: value' corrections to already-filled fields
        # run first (D3): they are explicit and addressed, so
        # both engines honour them, and the engines then see
        # only the lines that are left. Corrections may have
        # written the scope, so the parsed state and the standing
        # style choice are reloaded afterwards.
        updated, corr_dropped, unknown_names, confirmed, text = \
            _explicit_corrections(project, parsed, text)
        dropped.extend(corr_dropped)
        if updated or confirmed:
            parsed = _load_scope(project)
            choice = style_choice(project)

        # An explicit style pick is a decision, not an extraction —
        # honoured in both modes before anything else runs, but
        # only while the style lock is the pending question (so an
        # answer like "warm chiptune" for the audio mood can never
        # be mistaken for a re-pick).
        pending_names = list(session.get("pending_fields") or [])
        if not pending_names:
            group = pending_group(parsed)
            pending_names = ([f.name for f in group[1]]
                             if group else [])
        sample = None
        if STYLE_FIELD in pending_names or session.get("style_reopen"):
            sample = style_pick_from_text(text)
        if sample is not None:
            applied = apply_style_pick(project, sample)
            session["style_reopen"] = False
        elif choice and _is_exact_pick(text):
            # A pick for a decision that already stands — a stale
            # page or a double submit. Refused: nothing changes.
            _append_turn(session, {
                "role": "assistant", "kind": "info", "at": _now(),
                "text": (
                    "The style is already chosen and locked, so I "
                    "have not changed it. To pick a different one, "
                    "use Change style on the chosen sample card "
                    "(or send 'change-style')."),
                "fields": [],
            })
            _maybe_present(session, project, parsed)
            _save_session(project, session)
            return finish({"style_refused": True})
        else:
            outcome = None
            if text.strip():
                outcome = _brain_turn(project, session, parsed, text,
                                      settings_path=settings_path)
            if outcome is not None:
                eng_applied, eng_dropped, next_message = outcome
                applied.update(eng_applied)
                dropped.extend(eng_dropped)
                brain_used = True
            elif text.strip():
                parsed = _load_scope(project)
                eng_applied, eng_dropped, ignored, uncaptured = \
                    _deterministic_apply(project, parsed, text)
                applied.update(eng_applied)
                dropped.extend(eng_dropped)
            # An empty remainder means the message was entirely
            # corrections (handled above) — no engine runs.
        if reporter is not None:
            reporter(f"Interview turn recorded "
                     f"({len(applied)} field(s) captured)")
        bits = []
        if applied:
            bits.append(_sentence(
                "Got it — " + "; ".join(
                    f"{name}: {value}"
                    for name, value in applied.items())))
        if updated:
            bits.append(_sentence(
                "Updated " + "; ".join(
                    f"{name}: {value}"
                    for name, value in updated.items())))
        if confirmed:
            bits.append(
                "Already as you say: "
                + ", ".join(sorted(set(confirmed)))
                + " — nothing to change there.")
        if unknown_names:
            bits.append(
                "I don't have a field called "
                + "; ".join(f'"{name}"' for name in unknown_names)
                + " — the scope's fields are the ones listed in "
                "the panel, so nothing was changed for "
                + ("that line." if len(unknown_names) == 1
                   else "those lines."))
        if dropped:
            whys = "; ".join(
                f"{name} — {_why_hint(name)}" if _why_hint(name)
                else name
                for name in sorted(set(dropped)))
            bits.append(
                "I could not use the answer for: " + whys
                + ". I will ask about those again below.")
        if uncaptured and not applied and not updated:
            # The turn changed nothing at all. Say so plainly,
            # with the reason — a silent re-ask here is what
            # looped makers forever on answers like "None" (D1).
            asking = ", ".join(
                f.name for f in parsed.missing_fields) or "nothing"
            bits.append(
                "I could not take "
                + "; ".join(f'"{line}"' for line in uncaptured)
                + " as an answer to what I am asking just now ("
                + asking + "), so nothing was captured from that "
                "message. One answer per line works, or "
                "'Field: value' to answer a field by name.")
        elif uncaptured:
            bits.append(
                "I could not place "
                + "; ".join(f'"{line}"' for line in uncaptured)
                + " — no question I am asking takes it, so I set "
                "it aside uncaptured.")
        if ignored:
            bits.append(
                "I had more answers than questions in that group, "
                "so I set "
                + ("this extra line" if len(ignored) == 1
                   else "these extra lines")
                + " aside, unused: "
                + "; ".join(f'"{line}"' for line in ignored)
                + ". Nothing was shifted — each answer above went "
                "to its own question.")
        if bits or next_message:
            _append_turn(session, {
                "role": "assistant", "kind": "reflection",
                "at": _now(),
                "text": "\n".join(
                    bit for bit in (next_message, "\n".join(bits))
                    if bit),
                "fields": sorted(set(applied) | set(updated)),
            })
        parsed = _load_scope(project)
        _maybe_present(session, project, parsed,
                       force=bool(applied or dropped or uncaptured
                                  or ignored or updated
                                  or unknown_names))
        _save_session(project, session)
    out = public_state(project, load_session(project) or session)
    out["applied"] = applied
    out["dropped"] = dropped
    out["ignored"] = ignored
    out["uncaptured"] = uncaptured
    out["updated"] = updated
    out["unknown_fields"] = unknown_names
    out["brain_used"] = brain_used
    return out


def summary_text(state: dict) -> str:
    """A one-line summary of a turn's outcome, for job results."""
    applied = state.get("applied") or {}
    bits = []
    if applied:
        bits.append("Captured: " + ", ".join(sorted(applied)) + ".")
    updated = state.get("updated") or {}
    if updated:
        bits.append("Updated: " + ", ".join(sorted(updated)) + ".")
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
        "style_chosen": style_choice(project),
        "style_reopen": bool(session.get("style_reopen")),
    }
