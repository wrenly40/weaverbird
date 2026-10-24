"""The per-project token/cost ledger (JSONL).

Every model call lands here, so a game's spend can be checked against
the global budgets in the global settings file. The ledger reports
against the global budget; it never sets its own limits.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from .settings import warn_percent

TIERS = ("cheap", "mid", "frontier")


class LedgerError(Exception):
    """Raised for bad ledger entries or unreadable ledger files."""


def ledger_path_for(project_dir) -> Path:
    """The ledger file for a project directory (or the file itself)."""
    path = Path(project_dir)
    if path.is_file():
        return path
    return path / ".weaverbird" / "ledger.jsonl"


def add_entry(project_dir, stage: str, tier: str, tokens: int, cost: float,
              source: str | None = None) -> dict:
    """Append one entry to the project's ledger and return it.

    ``source`` names where the figures came from when a provider
    reports them itself (e.g. "opencode" — the maker's OpenCode
    server reports its own token counts and cost); it is recorded
    on the entry and never changes the numbers."""
    if tier not in TIERS:
        raise LedgerError(f"Tier must be one of {', '.join(TIERS)}, got {tier!r}.")
    if not stage:
        raise LedgerError("A stage is required.")
    try:
        tokens = int(tokens)
        cost = float(cost)
    except (TypeError, ValueError) as exc:
        raise LedgerError("Tokens must be a whole number and cost a number.") from exc
    if tokens < 0 or cost < 0:
        raise LedgerError("Tokens and cost cannot be negative.")
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "stage": str(stage),
        "tier": tier,
        "tokens": tokens,
        "cost": round(cost, 6),
    }
    if source:
        entry["source"] = str(source)
    path = ledger_path_for(project_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry) + "\n")
    return entry


def read_entries(project_dir) -> list[dict]:
    """Read all ledger entries (empty list when there is no ledger yet)."""
    path = ledger_path_for(project_dir)
    if not path.exists():
        return []
    entries = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise LedgerError(f"Bad line in ledger {path}: {line!r}") from exc
    return entries


def summarise(project_dir) -> dict:
    """Totals for a project's ledger: overall, by stage and by tier."""
    entries = read_entries(project_dir)
    summary = {
        "entries": len(entries),
        "tokens": 0,
        "cost": 0.0,
        "by_stage": {},
        "by_tier": {},
    }
    for entry in entries:
        tokens = int(entry.get("tokens", 0))
        cost = float(entry.get("cost", 0.0))
        summary["tokens"] += tokens
        summary["cost"] += cost
        for key, bucket in (("stage", "by_stage"), ("tier", "by_tier")):
            name = str(entry.get(key, "unknown"))
            slot = summary[bucket].setdefault(name, {"tokens": 0, "cost": 0.0})
            slot["tokens"] += tokens
            slot["cost"] += cost
    summary["cost"] = round(summary["cost"], 6)
    return summary


def _status_for(used: float, cap, fraction: float) -> tuple[str, float | None]:
    if cap is None:
        return "unknown", None
    percent = (used / cap * 100.0) if cap else 0.0
    if used >= cap:
        return "over", percent
    if used >= cap * fraction:
        return "warn", percent
    return "ok", percent


_ORDER = {"ok": 0, "warn": 1, "over": 2, "unknown": -1}


def check_budgets(project_dir, settings_data: dict) -> dict:
    """Check the ledger against the global budgets.

    Returns a dict with an overall status of ok / warn / over (unknown
    when no cap is set), plus the per-cap detail.
    """
    summary = summarise(project_dir)
    budgets = settings_data.get("budgets", {}) if isinstance(settings_data, dict) else {}
    fraction = warn_percent(budgets)

    token_status, token_percent = _status_for(
        summary["tokens"], budgets.get("tokens_per_game"), fraction
    )
    cost_status, cost_percent = _status_for(
        summary["cost"], budgets.get("cost_ceiling_per_month"), fraction
    )

    overall = token_status
    for candidate in (cost_status,):
        if _ORDER.get(candidate, -1) > _ORDER.get(overall, -1):
            overall = candidate

    return {
        "status": overall,
        "tokens": summary["tokens"],
        "tokens_cap": budgets.get("tokens_per_game"),
        "tokens_percent": token_percent,
        "cost": summary["cost"],
        "cost_cap": budgets.get("cost_ceiling_per_month"),
        "cost_percent": cost_percent,
        "warn_at_percent": int(fraction * 100),
    }
