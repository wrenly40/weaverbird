"""Player feedback — pipeline stage 10's front half: the inbox,
the triage queue, and the loop's memory.

Feedback arrives from the places a published game actually hears
from and lands in one store, ``.weaverbird/feedback.json``:

- GitHub Issues of the project's linked repository (via the ``gh``
  CLI, when the project is a checkout with a GitHub remote),
- itch.io comments, best-effort, when ``ITCH_API_KEY`` is set and
  the game's itch page URL is on record,
- the manual inbox — files in ``feedback-inbox/`` and
  ``weaverbird feedback add`` — which is also where the model
  playtester files its confusion notes.

Triage is deterministic first: a keyword classifier assigns the
category, items are clustered by a shared label, and the queue is
ranked by mentions × severity. When a brain is configured, the
cheap tier may refine categories and clusters from the item texts
(strict JSON, validated against the enum) — any failure and the
deterministic answer stands.

What happens to an approved item is patch.py's job; what the maker
decides (requests, scope, price) is never auto-anything here.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from . import brain as brain_module
from . import state as state_module

STORE_NAME = "feedback.json"
CATEGORIES = ("bug", "balance", "confusion", "request", "praise", "unknown")
STATUSES = ("new", "triaged", "approved", "dismissed", "patched", "planned")
INBOX_DIR_NAME = "feedback-inbox"


class FeedbackError(Exception):
    """Raised when a feedback operation cannot run at all."""


def store_path_for(project_dir) -> Path:
    return Path(project_dir) / ".weaverbird" / STORE_NAME


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_store(project_dir) -> dict:
    path = store_path_for(project_dir)
    if not path.exists():
        return {"items": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"items": []}
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        return {"items": []}
    return data


def save_store(project_dir, store: dict) -> None:
    path = store_path_for(project_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(store, indent=2) + "\n", encoding="utf-8")


def get_item(project_dir, item_id: str) -> dict | None:
    for item in load_store(project_dir).get("items", []):
        if item.get("id") == item_id:
            return item
    return None


def _hash_of(source: str, external: str) -> str:
    return hashlib.sha256(f"{source}\n{external}".encode("utf-8")).hexdigest()


def add_item(project_dir, *, source: str, text: str, author: str = "",
             external_id: str = "", at: str = "") -> tuple[dict, bool]:
    """Add one item (deduped by source + external id/text hash).

    Returns (item, created). The item starts life as status "new",
    category "unknown" — triage fills those in.
    """
    project = Path(project_dir)
    store = load_store(project)
    digest = _hash_of(source, external_id or text.strip())
    for item in store["items"]:
        if item.get("dedupe") == digest:
            return item, False
    item = {
        "id": f"fb-{len(store['items']) + 1:03d}",
        "source": source,
        "author": author or "",
        "text": " ".join(str(text).split()),
        "at": at or _now(),
        "category": "unknown",
        "severity": 2,
        "status": "new",
        "cluster": "",
        "patch_version": "",
        "note": "",
        "external_id": external_id,
        "dedupe": digest,
    }
    store["items"].append(item)
    save_store(project, store)
    return item, True


def update_item(project_dir, item_id: str, **changes) -> dict:
    project = Path(project_dir)
    store = load_store(project)
    for item in store["items"]:
        if item.get("id") == item_id:
            item.update(changes)
            save_store(project, store)
            return item
    raise FeedbackError(f"No feedback item with id {item_id!r}.")


# ---------------------------------------------------------------------------
# Fetching

def _github_remote(project: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"], cwd=str(project),
            capture_output=True, text=True, timeout=15)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    match = re.search(r"github\.com[:/]([^/]+/[^/.]+)", result.stdout or "")
    return match.group(1) if match else None


def _fetch_github(project: Path) -> tuple[list[dict], str]:
    """Open issues (title + body + comment count) via the gh CLI."""
    repo = _github_remote(project)
    if not repo:
        return [], "no GitHub remote on this project — skipped"
    try:
        result = subprocess.run(
            ["gh", "issue", "list", "--repo", repo, "--state", "open",
             "--limit", "50", "--json", "number,title,body,author,comments"],
            capture_output=True, text=True, timeout=60)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return [], "the gh CLI is not available — skipped"
    if result.returncode != 0:
        return [], f"gh issue list failed — skipped ({(result.stderr or '').strip()[:120]})"
    try:
        issues = json.loads(result.stdout or "[]")
    except json.JSONDecodeError:
        return [], "gh answered with something unreadable — skipped"
    fetched = []
    for issue in issues:
        comments = issue.get("comments") or []
        summary = (issue.get("body") or "").strip()
        if comments:
            last = comments[-1].get("body", "")
            summary = (summary + "\nLatest comment: " + last).strip()
        fetched.append({
            "external_id": f"issue-{issue.get('number')}",
            "author": (issue.get("author") or {}).get("login", ""),
            "text": f"{issue.get('title', '')}. {summary}".strip(),
        })
    return fetched, f"{len(fetched)} open issue(s) from {repo}"


def _itch_page_url(project: Path) -> str:
    try:
        state = state_module.load_state(project)
    except state_module.PipelineError:
        state = {}
    url = state.get("itch_url") or state.get("itch_page")
    if isinstance(url, str) and url:
        return url
    from . import scope as scope_module
    scope_file = scope_module.scope_path_for(project)
    if scope_file.exists():
        parsed = scope_module.load_scope(scope_file)
        for field in parsed.fields:
            if "itch" in field.name.lower() and "http" in field.value:
                return field.value.strip()
    return ""


def _fetch_itch(project: Path) -> tuple[list[dict], str]:
    """Best-effort itch.io comments. Any failure is reported, never
    raised — itch has no stable public comments API, so this reads
    the game's page JSON feed when a key and a page URL exist."""
    key = os.environ.get("ITCH_API_KEY")
    if not key:
        return [], "ITCH_API_KEY is not set — skipped"
    url = _itch_page_url(project)
    if not url:
        return [], "no itch page URL on record for this game — skipped"
    match = re.search(r"itch\.io/([A-Za-z0-9_-]+)", url)
    if not match:
        return [], f"could not read a game id from {url!r} — skipped"
    api = f"https://itch.io/api/1/{key}/game/{match.group(1)}/comments"
    request = urllib.request.Request(api, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8", "replace"))
    except Exception as exc:  # best-effort by design
        return [], f"itch fetch failed ({type(exc).__name__}) — continuing without it"
    fetched = []
    for comment in payload.get("comments", []) if isinstance(payload, dict) else []:
        fetched.append({
            "external_id": f"comment-{comment.get('id')}",
            "author": (comment.get("user") or {}).get("username", ""),
            "text": str(comment.get("body", "")).strip(),
        })
    return fetched, f"{len(fetched)} comment(s) from itch"


def _fetch_inbox(project: Path) -> tuple[list[dict], str]:
    inbox = project / INBOX_DIR_NAME
    if not inbox.is_dir():
        return [], "no feedback-inbox/ folder — skipped"
    fetched = []
    for path in sorted(inbox.iterdir()):
        if path.suffix.lower() not in (".txt", ".md") or not path.is_file():
            continue
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        if not lines:
            continue
        title = lines[0].strip().lstrip("# ").strip()
        body = " ".join(line.strip() for line in lines[1:])
        fetched.append({
            "external_id": path.name,
            "author": "",
            "text": (title + (". " + body if body else "")).strip(),
        })
    return fetched, f"{len(fetched)} file(s) in feedback-inbox/"


_FETCHERS = (("github", _fetch_github), ("itch", _fetch_itch),
             ("inbox", _fetch_inbox))


def fetch_feedback(project_dir) -> dict:
    """Pull every source into the store. Returns a fetch report:
    {source: {status, detail, added}} — failures are recorded, never
    raised, and never stop the other sources."""
    project = Path(project_dir)
    report: dict = {}
    for name, fetcher in _FETCHERS:
        try:
            fetched, detail = fetcher(project)
        except Exception as exc:  # a source must never crash the fetch
            report[name] = {"status": "failed",
                            "detail": f"{type(exc).__name__}: {exc}",
                            "added": 0}
            continue
        added = 0
        for entry in fetched:
            if not entry.get("text"):
                continue
            _item, created = add_item(
                project, source=name, text=entry["text"],
                author=entry.get("author", ""),
                external_id=entry.get("external_id", ""))
            added += int(created)
        report[name] = {"status": "ok", "detail": detail, "added": added}
    return report


# ---------------------------------------------------------------------------
# Triage

_RULES: list[tuple[str, tuple[str, ...], int]] = [
    # (category, keywords, severity when matched)
    ("bug", ("crash", "error", "broken", "can't", "cannot", "won't",
             "doesn't work", "bug", "freeze", "stuck on", "black screen",
             "not working", "fails", "not responding",
             "stopped working", "soft lock"), 3),
    ("balance", ("too hard", "too easy", "too slow", "too fast",
                 "unfair", "impossible", "frustrating", "grind",
                 "overpowered", "weak", "difficult", "difficulty",
                 "brutal"), 2),
    ("confusion", ("how do", "how to", "confusing", "confused",
                   "where do", "where is", "don't understand",
                   "not clear", "what do i", "stuck", "lost"), 2),
    ("request", ("please add", "wish", "would love", "could you add",
                 "add a", "needs a", "should have", "more levels",
                 "multiplayer"), 1),
    ("praise", ("love", "great", "fun", "awesome", "amazing",
                "brilliant", "enjoyed", "nice", "well done"), 1),
]

# Natural balance language (dogfood finding D7): players rarely
# write "too hard" — they write that a round "is a wall", that it
# "feels much harder", that the "timer ran out". These signals
# are checked at the balance position in the rule order (after
# bug, so a crash report mentioning a spike trap stays a bug),
# and the comparative signal needs complaint context around the
# word — an intensifier before it or a "than" after — so praise
# ("the harder levels were great fun") and requests ("add an
# easier mode") are not swallowed.
# Past-tense / irregular bug verbs (dogfood D2-2): players write
# "the game froze", not "freeze", and the substring keyword list
# missed them, so the report landed in unknown. These match on
# word boundaries, NOT as substrings: "froze" must catch the verb
# without claiming the adjective in "the frozen lake level" (which
# would flip praise and requests about one), and "hung" must not
# claim "hunger".
_BUG_VERB_RE = re.compile(
    r"\b(froze|freezing|hung|hangs|unresponsive|glitch|glitched|"
    r"glitching|glitches|softlock|softlocked)\b")

_COMPARATIVE_RE = re.compile(r"\b(harder|easier|tougher)\b")
_COMPARATIVE_CONTEXT = {
    "much", "way", "so", "far", "too", "feels", "feel", "felt",
    "gets", "get", "got", "became", "becomes", "is", "was",
    "seems", "seem", "really", "keeps", "keep",
}
_RAN_OUT_RE = re.compile(r"\b(ran out|runs out|run out|running out)\b")
_TIME_WORD_RE = re.compile(r"\b(timer|time|clock)\b")
_WALL_RE = re.compile(
    r"\b(difficulty spike|spike in difficulty|hit a wall|"
    r"brick wall|is a wall|a real wall)\b")


def _balance_signals(lowered: str) -> bool:
    """True when the text carries a natural balance signal (D7)."""
    if _WALL_RE.search(lowered):
        return True
    ran = _RAN_OUT_RE.search(lowered)
    if ran:
        for match in _TIME_WORD_RE.finditer(lowered):
            if abs(match.start() - ran.start()) <= 80:
                return True
    for match in _COMPARATIVE_RE.finditer(lowered):
        before = [w.strip(".,!—-")
                  for w in lowered[:match.start()].split()[-3:]]
        after = [w.strip(".,!—-")
                 for w in lowered[match.end():].split()[:3]]
        if any(w in _COMPARATIVE_CONTEXT for w in before):
            return True
        if "than" in after:
            return True
    return False

_CLUSTER_WORDS = (
    "hazard", "timer", "level", "crate", "jump", "speed",
    "menu", "save", "sound", "music", "control", "door", "spike",
    "star", "target", "hard", "easy", "slow", "fast", "crash",
)


def classify_text(text: str) -> tuple[str, int]:
    """(category, severity) from keywords — the deterministic floor."""
    lowered = f" {text.lower()} "
    for category, keywords, severity in _RULES:
        if any(keyword in lowered for keyword in keywords):
            return category, severity
        if category == "bug" and _BUG_VERB_RE.search(lowered):
            return category, severity
        if category == "balance" and _balance_signals(lowered):
            return category, severity
    return "unknown", 2


def cluster_label(text: str, category: str) -> str:
    """One shared keyword names the cluster — items about the same
    thing must land together for the mentions count to mean anything."""
    lowered = text.lower()
    for word in _CLUSTER_WORDS:
        if word in lowered:
            return word
    return category


def deterministic_triage(store: dict) -> dict:
    """Classify + cluster every un-triaged item, in place."""
    for item in store["items"]:
        if item.get("status") in ("patched", "dismissed", "planned"):
            continue
        category, severity = classify_text(item.get("text", ""))
        item["category"] = category
        item["severity"] = severity
        item["cluster"] = cluster_label(item.get("text", ""), category)
        if item.get("status") in ("new", "triaged"):
            item["status"] = "triaged"
    return store


def _brain_refine(project, store: dict, settings_path=None) -> str:
    """Let the cheap tier refine categories/clusters. Returns a note.

    Strict JSON in/out, validated against the category enum; any
    problem at all and the deterministic result stands untouched.
    """
    _, data = brain_module.resolve_settings(settings_path, project)
    status = brain_module.tier_status(data, "cheap") if data else None
    if not status or not status.get("usable"):
        return "deterministic triage only (no usable cheap-tier model)"
    open_items = [i for i in store["items"]
                  if i.get("status") == "triaged"]
    if not open_items:
        return "nothing for the model to refine"
    listing = "\n".join(
        f"{i['id']}: {i['text'][:200]}" for i in open_items[:30])
    messages = [
        {"role": "system", "content": (
            "You refine triage for game feedback. Answer with one JSON "
            "object only: {\"items\": {\"<id>\": {\"category\": one of "
            + ", ".join(CATEGORIES) + ", \"cluster\": short label}}. "
            "Only include ids you are confident about.")},
        {"role": "user", "content": "Feedback items:\n" + listing},
    ]
    try:
        result = brain_module.complete(project, "cheap", messages,
                                       stage="feedback-triage",
                                       settings_path=settings_path,
                                       max_tokens=600)
    except brain_module.BrainBudgetError as exc:
        return f"deterministic triage kept (brain refused by budget: {exc})"
    if not result.ok:
        return "deterministic triage kept (the model call did not work)"
    parsed = brain_module.extract_json(result.text)
    refined = 0
    raw = parsed.get("items")
    if isinstance(raw, dict):
        by_id = {i["id"]: i for i in open_items}
        for item_id, change in raw.items():
            if item_id not in by_id or not isinstance(change, dict):
                continue
            category = str(change.get("category", "")).lower()
            if category in CATEGORIES:
                by_id[item_id]["category"] = category
                refined += 1
            cluster = str(change.get("cluster", "")).strip()
            if cluster and len(cluster) <= 40:
                by_id[item_id]["cluster"] = cluster
    note = f"cheap tier refined {refined} item(s)"
    if result.warning:
        note += " — " + result.warning
    return note


def triage(project_dir, settings_path=None, use_brain: bool = True) -> dict:
    """Classify, cluster and rank the whole queue. Returns
    {items (ranked), note}. Ranked score: cluster mentions × the
    item's severity — a loud, severe cluster outranks a quiet one."""
    project = Path(project_dir)
    store = load_store(project)
    deterministic_triage(store)
    note = "deterministic triage"
    if use_brain:
        note = _brain_refine(project, store, settings_path=settings_path)
    save_store(project, store)
    return {"items": ranked_queue(store), "note": note}


def ranked_queue(store: dict) -> list[dict]:
    """Items sorted by cluster score (mentions × severity), then
    severity, patched/dismissed last."""
    counts: dict[str, int] = {}
    for item in store["items"]:
        counts[item.get("cluster", "")] = counts.get(item.get("cluster", ""), 0) + 1
    for item in store["items"]:
        mentions = counts.get(item.get("cluster", ""), 1)
        item["score"] = mentions * int(item.get("severity", 2))

    def key(item):
        done = item.get("status") in ("patched", "dismissed")
        return (done, -item.get("score", 0), -int(item.get("severity", 2)))

    return sorted(store["items"], key=key)


def queue_lines(items: list[dict]) -> list[str]:
    lines = []
    for item in items:
        lines.append(
            f"[{item['id']}] {item.get('category', 'unknown')} · "
            f"sev {item.get('severity', '?')} · score {item.get('score', 0)} · "
            f"{item.get('status', 'new')} · cluster "
            f"{item.get('cluster', '')!r} — {item.get('text', '')[:110]}"
        )
    return lines


def set_status(project_dir, item_id: str, status: str) -> dict:
    if status not in STATUSES:
        raise FeedbackError(
            f"Unknown status {status!r}. Known: {', '.join(STATUSES)}.")
    return update_item(project_dir, item_id, status=status)
