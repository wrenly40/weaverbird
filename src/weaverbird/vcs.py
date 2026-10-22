"""Local git helpers, plus a read-only GitHub login check.

Rollback in the factory is local git: a commit per iteration and a tag
on every known-good state, so a failed iteration costs a checkout, not
a rebuild. The GitHub connector adds backup and sharing on top of this;
nothing here needs GitHub to work.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path


class VcsError(Exception):
    """Raised when a git command fails."""


def _git(path, *args, check=True) -> subprocess.CompletedProcess:
    result = subprocess.run(
        ["git", *args], cwd=str(path), capture_output=True, text=True
    )
    if check and result.returncode != 0:
        message = (result.stderr or result.stdout).strip()
        raise VcsError(f"git {' '.join(args)} failed: {message}")
    return result


def is_repo(path) -> bool:
    path = Path(path)
    if not path.exists():
        return False
    result = _git(path, "rev-parse", "--is-inside-work-tree", check=False)
    return result.returncode == 0 and result.stdout.strip() == "true"


def init_repo(path) -> Path:
    """Initialise a git repository in path (idempotent)."""
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    if not is_repo(path):
        _git(path, "init", "--quiet")
    return path


def commit_all(path, message: str) -> str:
    """Stage everything and commit it. Returns the commit hash."""
    _git(path, "add", "-A")
    staged = _git(path, "diff", "--cached", "--quiet", check=False)
    if staged.returncode == 0:
        raise VcsError("Nothing to commit — the working tree matches the last commit.")
    _git(path, "commit", "--quiet", "-m", message)
    return _git(path, "rev-parse", "HEAD").stdout.strip()


def tag_known_good(path, name: str) -> str:
    """Tag the current commit as a known-good checkpoint."""
    _git(path, "tag", name)
    return name


def list_tags(path) -> list[str]:
    if not is_repo(path):
        return []
    out = _git(path, "tag", "--list").stdout
    return [line.strip() for line in out.splitlines() if line.strip()]


# Backwards-friendly alias: the tags currently in the repo.
current_tags = list_tags


def rollback_to_tag(path, name: str) -> None:
    """Roll the working tree back to a known-good tag (hard reset).

    This discards uncommitted changes and any commits after the tag —
    that is the point of a rollback, and why every good state is tagged.
    """
    if name not in list_tags(path):
        raise VcsError(f"No tag named {name!r} in this repository.")
    _git(path, "reset", "--hard", "--quiet", name)


_ACCOUNT_RE = re.compile(r"[Ll]ogged in to github\.com(?: account| as) ([A-Za-z0-9-]+)")


def github_status() -> dict:
    """Check `gh auth status` and return the account name, if logged in.

    Only the account name is parsed out. No token is ever requested
    (--show-token is never used) or returned.
    """
    try:
        result = subprocess.run(
            ["gh", "auth", "status"], capture_output=True, text=True, timeout=15
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return {"available": False, "logged_in": False, "account": None}
    combined = (result.stdout or "") + "\n" + (result.stderr or "")
    match = _ACCOUNT_RE.search(combined)
    account = match.group(1) if match else None
    return {
        "available": True,
        "logged_in": account is not None,
        "account": account,
    }
