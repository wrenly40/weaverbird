#!/bin/sh
# Build Weaverbird as a native binary for THIS machine:
# dist/native/weaverbird-<platform>
#
# The binary is frozen with PyInstaller (installer/weaverbird.spec):
# the same CLI and app the zipapp carries, plus its Python
# interpreter, so the machine that runs it needs no Python at all.
# The zipapp (./build.sh) remains the default build; this is the
# no-Python route, one platform per machine — the manual GitHub
# workflow (.github/workflows/tests.yml) builds the other
# platforms' binaries the same way. See docs/RELEASING.md.
#
# Usage:  ./build-native.sh
set -e
cd "$(dirname "$0")"

if ! python3 -c "import PyInstaller" 2>/dev/null; then
    echo "build-native.sh: PyInstaller is not installed for python3." >&2
    echo "Install it first (for example: python3 -m pip install pyinstaller), then run ./build-native.sh again." >&2
    exit 1
fi
if ! python3 -c "import yaml" 2>/dev/null; then
    echo "build-native.sh: PyYAML is not installed for python3." >&2
    echo "Install it first (for example: python3 -m pip install pyyaml), then run ./build-native.sh again." >&2
    exit 1
fi

case "$(uname -s)" in
    Linux)          OUT="dist/native/weaverbird-linux" ;;
    Darwin)         OUT="dist/native/weaverbird-macos" ;;
    MINGW*|MSYS*|CYGWIN*) OUT="dist/native/weaverbird-windows.exe" ;;
    *)              OUT="dist/native/weaverbird" ;;
esac

mkdir -p dist/native
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT INT TERM

python3 -m PyInstaller --noconfirm --clean \
    --distpath "$WORK/dist" --workpath "$WORK/build" \
    installer/weaverbird.spec

# PyInstaller names the product after the spec's EXE name
# ("weaverbird", or "weaverbird.exe" on Windows).
if [ -f "$WORK/dist/weaverbird" ]; then
    mv "$WORK/dist/weaverbird" "$OUT"
elif [ -f "$WORK/dist/weaverbird.exe" ]; then
    mv "$WORK/dist/weaverbird.exe" "$OUT"
else
    echo "build-native.sh: PyInstaller did not produce the expected binary." >&2
    exit 1
fi
chmod +x "$OUT" 2>/dev/null || true

echo "Built $OUT"
echo "Try it:  $OUT doctor"
