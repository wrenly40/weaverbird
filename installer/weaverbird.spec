# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the native Weaverbird binary.

One file, console, named ``weaverbird`` — the same CLI the
zipapp carries, frozen with its interpreter so the machine
running it needs no Python at all. Built by build-native.sh
(locally) and by the manual GitHub workflow (all platforms);
see docs/RELEASING.md.

Everything the app reads at runtime is package data reached
through importlib.resources — the web UI (web/), the scope /
connector / prompt templates (templates/), the design tokens
(assets/), and the Godot templates (skeleton_templates/,
game_templates/). The datas list below walks the package and
bundles every non-Python file, which is exactly the
package-data list pyproject.toml declares; if a file is not
in the package it cannot be bundled, and vice versa.
"""

from pathlib import Path

ROOT = Path(SPECPATH).resolve().parent
SRC = ROOT / "src"
PACKAGE = SRC / "weaverbird"

datas = []
for path in sorted(PACKAGE.rglob("*")):
    if not path.is_file():
        continue
    if "__pycache__" in path.parts or path.suffix in (".py", ".pyc"):
        continue
    dest = Path("weaverbird") / path.relative_to(PACKAGE).parent
    datas.append((str(path), str(dest)))

a = Analysis(
    [str(ROOT / "installer" / "entry.py")],
    pathex=[str(SRC)],
    binaries=[],
    datas=datas,
    hiddenimports=["yaml"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="weaverbird",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
