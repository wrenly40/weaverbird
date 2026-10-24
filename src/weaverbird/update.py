"""Self-update — ``weaverbird update [--check]``.

Weaverbird keeps itself current from its GitHub releases. The latest
release of wrenly40/weaverbird is read from the GitHub Releases API;
its tag (leading "v" stripped) is compared with the installed
version. What an update does depends on how Weaverbird is installed:

- The standalone executable (the zipapp file): the release asset
  named ``weaverbird`` is downloaded next to the installed file
  and — when the release carries a ``SHA256SUMS`` asset — its
  sha256 is verified against that file BEFORE the download is
  executed for anything (0.13.0, finding F5: the version-string
  run used to be the only check, so a hostile asset executed
  during "verification" itself). A checksum mismatch aborts with
  the old binary untouched. Only then is it run once to prove it
  works and reports the new version, and swapped in atomically.
  The previous version is kept as ``weaverbird.bak``. A release
  old enough to have no ``SHA256SUMS`` falls back to the version
  check alone, and says plainly that its integrity rests on the
  TLS/GitHub chain. A release with no ``weaverbird`` asset is
  reported plainly — it exists, but carries no executable yet.
- A native install (a PyInstaller binary, ``sys.frozen``): the
  platform-matching native asset is preferred —
  ``weaverbird-linux``, ``weaverbird-windows.exe`` or
  ``weaverbird-macos.zip`` (a zipped .app bundle; the whole
  bundle is swapped, kept as ``.app.bak``) — falling back to the
  zipapp asset when the release carries no native build for this
  platform. The swap semantics are the zipapp's: verify first
  (checksum against the release's SHA256SUMS when offered, then
  the download must run and report the new version), keep a
  backup, change nothing on failure.
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

import hashlib
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
CHECKSUMS_ASSET = "SHA256SUMS"
# The native release assets, by platform family (see
# docs/RELEASING.md). The macOS asset is a zip holding the .app
# bundle; the others are the bare binary.
NATIVE_ASSET_NAMES = {
    "linux": "weaverbird-linux",
    "windows": "weaverbird-windows.exe",
    "macos": "weaverbird-macos.zip",
}
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


# ---------------------------------------------------------------------------
# Download integrity (F5)

def _sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 256), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _parse_checksums(text: str) -> dict:
    """A SHA256SUMS file's {asset name: hex digest}. Lines are
    the sha256sum format: '<hex>  <name>' (or '<hex> *<name>')."""
    sums = {}
    for line in str(text or "").splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) != 2:
            continue
        hex_digest, name = parts
        name = name.strip().lstrip("*").strip()
        if len(hex_digest) == 64 and name:
            try:
                int(hex_digest, 16)
            except ValueError:
                continue
            sums[Path(name).name] = hex_digest.lower()
    return sums


def _verify_integrity(path: Path, info: dict, asset_name: str,
                      scratch_dir: Path) -> bool:
    """Check a downloaded asset against the release's SHA256SUMS
    BEFORE anything executes it. Prints what it did; returns True
    only when the update may proceed:

    - the release carries SHA256SUMS and the digest matches →
      proceed (the version run may still follow);
    - digest mismatch, an unreadable SHA256SUMS, or no entry for
      this asset → False: abort, nothing was executed;
    - the release predates SHA256SUMS → proceed on the legacy
      version check alone, saying plainly that integrity rests
      on the TLS/GitHub chain.
    """
    sums_url = (info.get("assets") or {}).get(CHECKSUMS_ASSET)
    if not sums_url:
        print(f"Note: the {info.get('tag', 'release')} release "
              "predates checksum files (it carries no SHA256SUMS "
              "asset), so this update's integrity rests on the "
              "TLS/GitHub chain alone — the version check is the "
              "only verification.")
        return True
    sums_path = Path(scratch_dir) / (CHECKSUMS_ASSET + ".download")
    try:
        _download(sums_url, sums_path)
        sums = _parse_checksums(
            sums_path.read_text(encoding="utf-8", errors="replace"))
    except (UpdateError, OSError):
        print("Update stopped: the release's SHA256SUMS file could "
              "not be read, so the download cannot be verified. "
              "Nothing was changed and the download was not run.")
        return False
    finally:
        sums_path.unlink(missing_ok=True)
    expected = sums.get(asset_name)
    if expected is None:
        print(f"Update stopped: the release's SHA256SUMS does not "
              f"list {asset_name}, so the download cannot be "
              "verified. Nothing was changed and the download "
              "was not run.")
        return False
    if _sha256_of(path) != expected:
        print(f"Update stopped: the downloaded {asset_name} does "
              "not match its SHA256SUMS entry (sha256 mismatch). "
              "It was not run, and the installed Weaverbird is "
              "untouched.")
        return False
    print(f"Checksum verified: {asset_name} matches the release's "
          "SHA256SUMS.")
    return True


def native_asset_name(platform: str = None) -> str | None:
    """The native release asset for a platform (sys.platform
    spelling), or None for a platform with no native build."""
    platform = sys.platform if platform is None else platform
    if platform.startswith("win"):
        return NATIVE_ASSET_NAMES["windows"]
    if platform == "darwin":
        return NATIVE_ASSET_NAMES["macos"]
    if platform.startswith("linux"):
        return NATIVE_ASSET_NAMES["linux"]
    return None


def select_update_asset(assets: dict, platform: str = None,
                        frozen: bool = False):
    """Which release asset an update downloads: (name, url) or
    None.

    A native (frozen) install takes its platform's native asset
    first and falls back to the zipapp asset; every other
    self-updating install takes the zipapp asset, as always.
    Pure function of the release payload — unit-tested against
    fake payloads.
    """
    if frozen:
        native = native_asset_name(platform)
        if native and assets.get(native):
            return native, assets[native]
    if assets.get(ASSET_NAME):
        return ASSET_NAME, assets[ASSET_NAME]
    return None


def detect_install(argv0=None, package_file=None, frozen=None,
                   executable=None) -> tuple:
    """How this copy is installed: ('native', path) /
    ('zipapp', path) / ('source', root) / ('other', None).

    Native: a frozen (PyInstaller) executable — sys.executable.
    Zipapp: argv[0] is a file and this package lives inside it.
    Source: a .git directory in an ancestor of the package.
    """
    if frozen is None:
        frozen = bool(getattr(sys, "frozen", False))
    if frozen:
        return "native", Path(
            sys.executable if executable is None else executable)
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

def _swap_executable(target: Path, info: dict, selected) -> int:
    """Download one executable asset, verify it (checksum first,
    when the release offers one — the download is never executed
    before its integrity passes), then swap it in, keeping the
    old file as ``.bak``. Shared by the zipapp and native
    (single-file) update paths."""
    name, url = selected
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
    if not _verify_integrity(tmp, info, name, target.parent):
        tmp.unlink(missing_ok=True)
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


def _update_zipapp(target: Path, info: dict) -> int:
    selected = select_update_asset(info["assets"], frozen=False)
    if not selected:
        print(f"The {info['tag']} release exists, but it carries no "
              f"'{ASSET_NAME}' executable asset yet — nothing was changed.")
        return 1
    return _swap_executable(target, info, selected)


def _update_native(target: Path, info: dict) -> int:
    selected = select_update_asset(info["assets"], frozen=True)
    if not selected:
        print(f"The {info['tag']} release exists, but it carries no "
              "executable asset for this platform yet — nothing "
              "was changed.")
        return 1
    name, _url = selected
    if name.endswith(".zip"):
        return _swap_macos_bundle(target, info, selected)
    return _swap_executable(target, info, selected)


def _swap_macos_bundle(target: Path, info: dict, selected) -> int:
    """The macOS native update: the asset is a zip holding the
    new .app bundle. The bundle inside the download is verified
    (its executable must run and report the new version) before
    the installed bundle is moved aside to ``.app.bak`` and the
    new one put in its place."""
    import zipfile

    name, url = selected
    app_dir = None
    for parent in (target, *target.parents):
        if parent.suffix == ".app":
            app_dir = parent
            break
    if app_dir is None:
        print("Update stopped: this install is not inside a .app "
              "bundle, so the macOS bundle asset cannot be swapped "
              "in. Nothing was changed.")
        return 1
    stage = Path(tempfile.mkdtemp(dir=str(app_dir.parent),
                                  prefix=app_dir.name + ".new-"))
    try:
        archive = stage / "download.zip"
        try:
            _download(url, archive)
        except UpdateError as exc:
            print(f"Update stopped: {exc} The installed Weaverbird "
                  "is untouched.")
            return 1
        if not _verify_integrity(archive, info, name, stage):
            return 1
        try:
            with zipfile.ZipFile(archive) as bundle:
                bundle.extractall(stage / "extracted")
        except zipfile.BadZipFile:
            print("Update stopped: the downloaded macOS asset was "
                  "not a readable zip. Nothing was changed.")
            return 1
        candidates = sorted((stage / "extracted").rglob("*.app"))
        if len(candidates) != 1:
            print("Update stopped: the downloaded macOS asset did "
                  "not contain exactly one .app bundle. Nothing "
                  "was changed.")
            return 1
        new_app = candidates[0]
        executables = [p for p in
                       (new_app / "Contents" / "MacOS").iterdir()
                       if p.is_file()]
        if not executables:
            print("Update stopped: the downloaded .app bundle has "
                  "no executable. Nothing was changed.")
            return 1
        new_exe = executables[0]
        new_exe.chmod(0o755)
        code, out = _run([str(new_exe), "--version"])
        if code != 0 or info["version"] not in out:
            print(f"The downloaded bundle did not verify — its "
                  f"executable should report version "
                  f"{info['version']} and run cleanly. The "
                  "installed Weaverbird is untouched.")
            return 1
        backup = app_dir.with_name(app_dir.name + ".bak")
        if backup.exists():
            shutil.rmtree(backup)
        shutil.move(str(app_dir), str(backup))
        try:
            shutil.move(str(new_app), str(app_dir))
        except OSError:
            shutil.move(str(backup), str(app_dir))
            print("Update stopped while swapping the bundle; the "
                  "previous version was restored.")
            return 1
        print(f"Updated Weaverbird to {info['version']}. The "
              f"previous version is kept at {backup} — rename it "
              f"back to {app_dir.name} to roll back.")
        return 0
    finally:
        shutil.rmtree(stage, ignore_errors=True)


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
    if kind == "native":
        return _update_native(Path(where), info)
    if kind == "zipapp":
        return _update_zipapp(Path(where), info)
    if kind == "source":
        return _update_source(Path(where), info)
    print("This install does not update itself (it was installed "
          "another way, e.g. pip). Upgrade with:")
    print(f"  {PIP_UPGRADE}")
    return 0
