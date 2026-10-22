"""The connector registry.

Everything external to the factory is a connector with a declared
contract: what it can do on its own, what needs the user's approval, and
what only the user can do. Unknown actions fail closed — they are
treated as needing approval, never as allowed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from . import template_text

ALLOWED_ALONE = "allowed_alone"
NEEDS_APPROVAL = "needs_approval"
USER_ONLY = "user_only"
UNKNOWN = "unknown"


class ConnectorError(Exception):
    """Raised when the registry cannot be read."""


@dataclass
class Connector:
    id: str
    kind: str = ""
    auth: str = ""
    can_do_alone: list[str] = field(default_factory=list)
    needs_approval: list[str] = field(default_factory=list)
    user_only: list[str] = field(default_factory=list)
    fallback_without: str = ""
    notes: str = ""


@dataclass
class Registry:
    connectors: list[Connector] = field(default_factory=list)

    def get(self, connector_id: str) -> Connector | None:
        for connector in self.connectors:
            if connector.id == connector_id:
                return connector
        return None


def default_registry_path() -> Path | None:
    """The current project's own registry file, if there is one.

    A project created by ``weaverbird init`` carries its own
    connectors.yaml at its root; when the CLI runs inside such a project
    that copy wins. Otherwise None is returned and the caller falls back
    to the registry packaged inside the app.
    """
    local = Path.cwd() / "connectors.yaml"
    if local.exists():
        return local
    return None


def load_registry_from_text(text: str) -> Registry:
    """Parse a connector registry from YAML text."""
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConnectorError(f"Connector registry is not valid YAML: {exc}") from exc
    return _registry_from_data(data)


def load_registry(path=None) -> Registry:
    """Load a connector registry.

    With no path: the current project's connectors.yaml when present,
    otherwise the registry packaged inside the app (read as a package
    resource, so it works inside the single-file executable too).
    """
    if path is None:
        local = default_registry_path()
        if local is not None:
            path = local
        else:
            return load_registry_from_text(template_text("connectors.yaml"))
    path = Path(path)
    if not path.exists():
        raise ConnectorError(f"Connector registry not found: {path}")
    return load_registry_from_text(path.read_text(encoding="utf-8"))


def _registry_from_data(data) -> Registry:
    if not isinstance(data, list):
        raise ConnectorError("Connector registry must be a YAML list.")
    registry = Registry()
    for item in data:
        if not isinstance(item, dict) or "id" not in item:
            continue
        registry.connectors.append(
            Connector(
                id=str(item["id"]),
                kind=str(item.get("kind", "")),
                auth=str(item.get("auth", "")),
                can_do_alone=list(item.get("can_do_alone") or []),
                needs_approval=list(item.get("needs_approval") or []),
                user_only=list(item.get("user_only") or []),
                fallback_without=str(item.get("fallback_without", "")),
                notes=str(item.get("notes", "")),
            )
        )
    return registry


def classify_capability(registry: Registry, connector_id: str, action: str) -> str:
    """Classify an action: allowed_alone / needs_approval / user_only /
    unknown (connector or action not in the registry)."""
    connector = registry.get(connector_id)
    if connector is None:
        return UNKNOWN
    if action in connector.can_do_alone:
        return ALLOWED_ALONE
    if action in connector.needs_approval:
        return NEEDS_APPROVAL
    if action in connector.user_only:
        return USER_ONLY
    return UNKNOWN


def check_capability(registry: Registry, connector_id: str, action: str) -> str:
    """The safe capability answer: unknown fails closed to needs_approval."""
    result = classify_capability(registry, connector_id, action)
    if result == UNKNOWN:
        return NEEDS_APPROVAL
    return result


def is_allowed_alone(registry: Registry, connector_id: str, action: str) -> bool:
    return classify_capability(registry, connector_id, action) == ALLOWED_ALONE
