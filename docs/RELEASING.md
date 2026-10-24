# Releasing Weaverbird

A release carries five assets: the single-file zipapp
(`weaverbird`, built locally), one native binary per platform
(`weaverbird-linux`, `weaverbird-windows.exe`,
`weaverbird-macos.zip`), built by the manual GitHub workflow —
and `SHA256SUMS`, the sha256 of each of the other four, in
sha256sum format. Installed copies update themselves from these
assets (`weaverbird update`): a native install takes its
platform's native asset first and falls back to the zipapp; a
zipapp install takes the zipapp. Since 0.13.0 the updater
verifies the downloaded asset against `SHA256SUMS` BEFORE
executing it for anything (older releases without the file fall
back to the version check, and the updater says plainly that
their integrity rests on the TLS/GitHub chain alone).

## How a release is cut

1. Bump `__version__` in `src/weaverbird/__init__.py` and
   `version` in `pyproject.toml` (they move together), and run
   the full suite: `PYTHONPATH=src python -m unittest discover -s tests`.
2. Build the zipapp locally: `./build.sh`, then check it:
   `./dist/weaverbird doctor`.
3. Commit, tag `vX.Y.Z`, push, and create the GitHub release for
   the tag. Attach `dist/weaverbird` — a release with no assets
   yet is fine at this point; updaters report it plainly.
4. Dispatch the workflow once, by hand: **Actions → Tests → Run
   workflow** (or `gh workflow run tests.yml`). It re-runs the
   test job and builds the three native binaries with
   PyInstaller (`build-native.sh`, the same script a local build
   uses), verifying each one runs `version` and `doctor` before
   uploading it as an artefact. Download the three artefacts —
   `weaverbird-linux`, `weaverbird-windows.exe`,
   `weaverbird-macos.zip` (a zip holding `Weaverbird.app`) — and
   attach them to the release.
5. With all four executables attached, generate the checksums
   over the EXACT files just attached and attach the result as
   the fifth asset:

   ```sh
   sha256sum weaverbird weaverbird-linux weaverbird-windows.exe \
       weaverbird-macos.zip > SHA256SUMS
   gh release upload vX.Y.Z SHA256SUMS
   ```

   Updaters refuse a download whose sha256 is not the listed one
   and never execute an unverified download — a release without
   `SHA256SUMS` still updates, but every updating user sees the
   "predates checksum files" caveat.
6. Sanity-check the published release the way a user would:
   on any machine, run the previous version's `weaverbird update`
   and confirm it lands on the new one.

## Cost note — read before dispatching

The workflow is **manual-only on purpose**: this account runs a
$0 GitHub Actions budget, and nothing in the workflow triggers
on push or pull request. One dispatch runs the test job plus
three native builds. GitHub bills macOS runners at **10×** the
Linux rate (Windows at 2×), so the macOS build is the expensive
line — a full native run costs on the order of a few dollars of
minutes if the included quota is already spent. Native release
builds are therefore a deliberate, occasional action: batch
releases, and don't dispatch the workflow just to check a hunch
(the Linux binary can always be built locally with
`./build-native.sh`).

## Local native builds

`./build-native.sh` builds the current machine's binary into
`dist/native/` (Linux: `weaverbird-linux`, macOS:
`weaverbird-macos` — the workflow wraps that one into the .app
zip; Windows: `weaverbird-windows.exe`). It needs PyInstaller
and PyYAML for the building Python; the binary itself needs no
Python on the machine that runs it. Verify any local build the
way the workflow does:

```
env -i dist/native/weaverbird-linux version
env -i dist/native/weaverbird-linux doctor
```

(the empty environment proves nothing leaks in from the build
machine's Python setup).
