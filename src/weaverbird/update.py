"""Self-update — ``weaverbird update [--check]``.

Weaverbird keeps itself current from its GitHub releases. The latest
release of wrenly40/weaverbird is read from the GitHub Releases API;
its tag (leading "v" stripped) is compared with the installed
version. What an update does depends on how Weaverbird is installed:

- The standalone executable (the zipapp file): the release asset
  named ``weaverbird`` is downloaded next to the installed file,
  run once to prove it works and reports the new version, and only
  then swapped in atomically. The previous version is kept as
  ``weaverbird.bak``. A download that fails verification changes
  nothing. A release with no ``weaverbird`` asset is reported
  plainly — it exists, but carries no executable yet.
- A source checkout (a .git directory above the package): refused
  while the working tree is dirty; otherwise ``git pull --ff-only``
  and ``./build.sh``, verified by running the rebuilt executable.
- Anything else (pip and friends): check-only — the exact upgrade
  command is printed and nothing is mutated.

Standard library only. Network and subprocess calls sit behind three
small seams (``_fetch_json``, ``_download``, ``_run``) so tests can
replace them: no test performs a real network call or replaces a
real executable.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

from . import __version__

GITHUB_REPO = "wrenly40/weaverbird"
API_URL = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
ASSET_NAME = "weaverbird"
PIP_UPGRADE = ("python3 -m pip install --upgrade "
               f"git+https://github.com/{GITHUB_REPO}")


class UpdateError(Exception):
    """Raised when the latest release cannot be read."""


# ---------------------------------------------------------------------------
# Seams (tests replace these)

def _fetch_json(url: str) -> dict:
    request = urllib.request.Request(
        url, headers={"User-Agent": f"weaverbird/{__version__}",
                      "Accept": "application/vnd.github+json"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        raise UpdateError(
            f"GitHub answered HTTP {exc.code} — the latest release "
            "could not be read.") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise UpdateError(
            f"GitHub could not be reached ({type(exc).__name__}) — "
            "check the network connection.") from exc
    except json.JSONDecodeError as exc:
        raise UpdateError("GitHub's answer was not readable JSON.") from exc


def _download(url: str, dest: Path) -> None:
    request = urllib.request.Request(
        url, headers={"User-Agent": f"weaverbird/{__version__}"})
    try:
        with urllib.request.urlopen(request, timeout=120) as response, \
                open(dest, "wb") as handle:
            shutil.copyfileobj(response, handle)
    except (urllib.error.URLError, OSError) as exc:
        raise UpdateError(
            f"The download failed ({type(exc).__name__}).") from exc


def _run(cmd: list, cwd=None) -> tuple:
    """Run a command, returning (exit code, combined output)."""
    try:
        done = subprocess.run(cmd, cwd=cwd, capture_output=True,
                              text=True, timeout=600)
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, f"{type(exc).__name__}: {exc}"
    return done.returncode, (done.stdout or "") + (done.stderr or "")


# ---------------------------------------------------------------------------
# Version and release facts

def parse_version(text: str) -> tuple:
    """'v0.3.0' / '0.3.0' -> (0, 3, 0). Non-numeric parts count as 0."""
    cleaned = str(text).strip().lstrip("vV")
    parts = []
    for piece in cleaned.split("."):
        digits = "".join(ch for ch in piece if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


def latest_release() -> dict:
    """The latest GitHub release: {tag, version, assets{name: url}}."""
    payload = _fetch_json(API_URL)
    tag = str(payload.get("tag_name") or "")
    if not tag:
        raise UpdateError("GitHub's latest-release answer had no tag.")
    assets = {}
    for asset in payload.get("assets") or []:
        if isinstance(asset, dict) and asset.get("name"):
            assets[asset["name"]] = asset.get("browser_download_url") or ""
    return {"tag": tag, "version": tag.lstrip("vV"),
            "assets": assets}


def update_available(info: dict, current: str = None) -> bool:
    return parse_version(info["version"]) > parse_version(
        current or __version__)


def detect_install(argv0=None, package_file=None) -> tuple:
    """How this copy is installed: ('zipapp', path) / ('source', root)
    / ('other', None).

    Zipapp: argv[0] is a file and this package lives inside it.
    Source: a .git directory in an ancestor of the package.
    """
    argv0 = sys.argv[0] if argv0 is None else argv0
    package_file = Path(__file__) if package_file is None \
        else Path(package_file)
    candidate = Path(argv0)
    if candidate.is_file() and str(package_file).startswith(
            str(candidate) + os.sep):
        return "zipapp", candidate
    here = package_file.resolve()
    for parent in here.parents:
        if (parent / ".git").exists():
            return "source", parent
    return "other", None


# ---------------------------------------------------------------------------
# The update behaviours

def _update_zipapp(target: Path, info: dict) -> int:
    url = info["assets"].get(ASSET_NAME)
    if not url:
        print(f"The {info['tag']} release exists, but it carries no "
              f"'{ASSET_NAME}' executable asset yet — nothing was changed.")
        return 1
    handle, tmp_name = tempfile.mkstemp(dir=str(target.parent),
                                        prefix=target.name + ".new-")
    os.close(handle)
    tmp = Path(tmp_name)
    try:
        _download(url, tmp)
    except UpdateError as exc:
        tmp.unlink(missing_ok=True)
        print(f"Update stopped: {exc} The installed Weaverbird is untouched.")
        return 1
    tmp.chmod(0o755)
    code, out = _run([str(tmp), "--version"])
    if code != 0 or info["version"] not in out:
        tmp.unlink(missing_ok=True)
        print(f"The downloaded executable did not verify — it should "
              f"report version {info['version']} and run cleanly. The "
              "installed Weaverbird is untouched.")
        return 1
    backup = target.with_name(target.name + ".bak")
    shutil.copy2(target, backup)
    os.replace(tmp, target)
    print(f"Updated Weaverbird to {info['version']}. The previous "
          f"version is kept at {backup} — rename it back to "
          f"{target.name} to roll back.")
    return 0


def _update_source(root: Path, info: dict) -> int:
    code, out = _run(["git", "status", "--porcelain"], cwd=root)
    if code != 0:
        print("Update stopped: git could not report the checkout's "
              "state. Nothing was changed.")
        return 1
    if out.strip():
        print("Update refused: the source checkout has uncommitted "
              "changes. Commit or stash them, then run "
              "'weaverbird update' again. Nothing was changed.")
        return 1
    code, _ = _run(["git", "pull", "--ff-only"], cwd=root)
    if code != 0:
        print("Update stopped: 'git pull --ff-only' did not complete. "
              "Nothing was rebuilt.")
        return 1
    code, _ = _run(["sh", "./build.sh"], cwd=root)
    if code != 0:
        print("Update stopped: the pull worked but ./build.sh failed. "
              "The checkout is updated; the old executable is still in "
              "dist/.")
        return 1
    code, out = _run([str(root / "dist" / "weaverbird"), "version"])
    if code != 0 or info["version"] not in out:
        print("Update stopped: the rebuilt dist/weaverbird did not "
              f"report version {info['version']}.")
        return 1
    print(f"Updated the checkout and rebuilt dist/weaverbird — now "
          f"at {info['version']}.")
    return 0


def run_update(check_only: bool = False) -> int:
    """The `weaverbird update` command. Prints; returns an exit code."""
    try:
        info = latest_release()
    except UpdateError as exc:
        print(f"Update check failed: {exc}")
        return 1
    current = __version__
    available = update_available(info, current)
    print(f"Installed: weaverbird {current}")
    print(f"Latest release: {info['tag']}")
    if not available:
        print("Weaverbird is up to date.")
        return 0
    if check_only:
        print(f"An update is available: {current} → {info['version']}. "
              "Run 'weaverbird update' to take it.")
        return 0
    kind, where = detect_install()
    if kind == "zipapp":
        return _update_zipapp(Path(where), info)
    if kind == "source":
        return _update_source(Path(where), info)
    print("This install does not update itself (it was installed "
          "another way, e.g. pip). Upgrade with:")
    print(f"  {PIP_UPGRADE}")
    return 0
