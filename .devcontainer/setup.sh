#!/usr/bin/env bash
# Runs once when the Codespace is created: Python deps, then the web app build.
set -euo pipefail
cd "$(dirname "$0")/.."

echo "== Installing Python dependencies"
python -m pip install --disable-pip-version-check -q -r requirements-dev.txt

echo "== Building the web app"
(cd frontend && npm ci --no-audit --no-fund && npm run build)

echo "== Setup done. The demo starts next (see .devcontainer/WELCOME.md)."
