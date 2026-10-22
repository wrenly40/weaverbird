#!/bin/sh
# Build Weaverbird into one self-contained executable: dist/weaverbird
#
# The executable is a Python zipapp. Everything the app needs lives
# inside it — the core library, the CLI, all templates and the
# connector registry, plus PyYAML (the app's one library dependency,
# copied in from the Python environment running this script).
# It needs Python 3.11+ on the machine that runs it, and nothing else
# from this repository.
#
# Usage:  ./build.sh
set -e
cd "$(dirname "$0")"

if ! python3 -c "import yaml" 2>/dev/null; then
    echo "build.sh: PyYAML is not installed for python3." >&2
    echo "Install it first (for example: python3 -m pip install pyyaml), then run ./build.sh again." >&2
    exit 1
fi

PYAML_DIR=$(python3 -c "import pathlib, yaml; print(pathlib.Path(yaml.__file__).resolve().parent)")

STAGE=$(mktemp -d)
trap 'rm -rf "$STAGE"' EXIT INT TERM
mkdir -p "$STAGE/app"

# The app itself.
cp -R src/weaverbird "$STAGE/app/weaverbird"
# The one dependency, bundled in (PyYAML is MIT-licensed; see its
# LICENSE inside the yaml package).
cp -R "$PYAML_DIR" "$STAGE/app/yaml"
find "$STAGE/app" -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true

# Zipapp entry point.
printf '%s\n' \
    'from weaverbird.cli import main' \
    '' \
    'if __name__ == "__main__":' \
    '    raise SystemExit(main())' \
    > "$STAGE/app/__main__.py"

mkdir -p dist
python3 -m zipapp "$STAGE/app" --python "/usr/bin/env python3" --output "dist/weaverbird.tmp"
mv "dist/weaverbird.tmp" "dist/weaverbird"
chmod +x "dist/weaverbird"

echo "Built dist/weaverbird"
echo "Try it:  ./dist/weaverbird doctor"
