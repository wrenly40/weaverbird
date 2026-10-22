"""Global settings: one file for the whole factory.

Token and cost caps live here and only here — never in a per-game scope
file, and the factory never raises a cap to keep its own loop alive.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

# Budget fields that must be set before any build can start.
REQUIRED_BUDGET_FIELDS = ("tokens_per_run", "tokens_per_game")


class SettingsError(Exception):
    """Raised when a settings file cannot be read or is malformed."""


@dataclass
class SettingsValidation:
    """Result of checking a settings mapping."""

    missing_required: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    budgets: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        """True when every required budget field is set and valid."""
        return not self.missing_required and not self.errors


def load_settings(path) -> dict:
    """Load a global-settings YAML file into a plain dict."""
    path = Path(path)
    if not path.exists():
        raise SettingsError(f"Settings file not found: {path}")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise SettingsError(f"Settings file is not valid YAML: {exc}") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise SettingsError("Settings file must be a YAML mapping at the top level.")
    return data


def validate_settings(data: dict) -> SettingsValidation:
    """Check a settings mapping for the required budget fields."""
    result = SettingsValidation()
    if not isinstance(data, dict):
        result.errors.append("Settings must be a mapping.")
        return result

    budgets = data.get("budgets")
    if not isinstance(budgets, dict):
        result.errors.append("Missing 'budgets' section.")
        budgets = {}
    result.budgets = budgets

    for name in REQUIRED_BUDGET_FIELDS:
        value = budgets.get(name)
        if value is None:
            result.missing_required.append(f"budgets.{name}")
        elif not isinstance(value, (int, float)) or isinstance(value, bool):
            result.errors.append(f"budgets.{name} must be a number, got {value!r}.")
        elif value <= 0:
            result.errors.append(f"budgets.{name} must be greater than zero.")

    warn = budgets.get("warn_at_percent_of_cap")
    if warn is not None and (
        not isinstance(warn, (int, float)) or not 0 < warn < 100
    ):
        result.errors.append(
            "budgets.warn_at_percent_of_cap must be a number between 1 and 99."
        )
    return result


def warn_percent(budgets: dict) -> float:
    """The warn threshold as a fraction (defaults to 80%)."""
    value = budgets.get("warn_at_percent_of_cap", 80)
    if not isinstance(value, (int, float)) or not 0 < value < 100:
        return 0.8
    return float(value) / 100.0
