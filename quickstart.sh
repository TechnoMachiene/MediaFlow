#!/usr/bin/env bash
# quickstart.sh - Ubuntu / WSL. Installs tools, starts Redis, builds venv, runs ALL tests.
# Safe to re-run.   Usage:  bash quickstart.sh
set -euo pipefail
cd "$(dirname "$0")"

case "$PWD" in
  /mnt/*) echo "STOP: project sits on a Windows drive ($PWD)."
          echo "Move it:  mkdir -p ~/code && cp -r \"$PWD\" ~/code/ && cd ~/code/media_queue"
          exit 1 ;;
esac

SUDO=""
if [ "$(id -u)" -ne 0 ]; then SUDO="sudo"; fi

echo "== 1/5 system packages"
$SUDO apt-get update -y
$SUDO apt-get install -y redis-server postgresql python3-venv python3-pip make unzip git

echo "== 2/5 python version (tested on 3.12; need 3.11+)"
python3 - <<'PY'
import sys
v = sys.version_info
print(f"python {v.major}.{v.minor}.{v.micro}")
sys.exit(0 if (v.major, v.minor) >= (3, 11) else 1)
PY

echo "== 3/5 start redis"
$SUDO service redis-server start || true
$SUDO service postgresql start || true
redis-cli ping

echo "== 4/5 virtualenv + packages"
python3 -m venv .venv
# shellcheck disable=SC1091
. .venv/bin/activate
pip install -r requirements-dev.txt

echo "== 5/5 full test gate"
REDIS_URL="${REDIS_URL:-redis://localhost:6379/0}" make 'done'

cat <<'MSG'

ALL GOOD. Next, every new terminal:
  cd ~/code/media_queue
  source .venv/bin/activate
  sudo service redis-server start
  export REDIS_URL=redis://localhost:6379/0
Then real Gemini: cp -n .env.example .env && nano .env && make live
FastAPI setup and run instructions: see the "FastAPI backend" section in README.md.
MSG
