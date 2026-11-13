#!/usr/bin/env bash
# Starts the MediaFlow demo in the background on port 8082 (runs on every Codespace start).
# Demo mode: offline drafts, SQLite, a worker inside the API process, seeded demo accounts.
# Logs: /tmp/mediaflow-demo.log    Restart: bash .devcontainer/start-demo.sh --restart
set -euo pipefail
cd "$(dirname "$0")/.."
PORT=8082
LOG=/tmp/mediaflow-demo.log
URL="http://localhost:$PORT"

if [ "${1:-}" = "--restart" ]; then
  pkill -f "uvicorn app.api.main:app --host 0.0.0.0 --port $PORT" 2>/dev/null || true
  sleep 1
fi

if curl -fsS "$URL/health/live" >/dev/null 2>&1; then
  echo "MediaFlow demo is already running: open the 'Ports' tab, port $PORT."
  exit 0
fi

echo "== Starting the MediaFlow demo (log: $LOG)"
# Blank GEMINI_API_KEY: demo mode refuses to run next to a real key.
DEMO_MODE=1 GEMINI_API_KEY= QUEUE_PREFIX=demo \
DATABASE_URL=sqlite:///demo.sqlite3 MEDIA_STORAGE=local MEDIA_STORAGE_DIR=demo-uploads \
EMBEDDED_WORKER=1 LOG_FORMAT=text \
  nohup setsid python -m uvicorn app.api.main:app --host 0.0.0.0 --port "$PORT" \
  >"$LOG" 2>&1 < /dev/null &

for _ in $(seq 1 60); do
  if curl -fsS "$URL/health/ready" >/dev/null 2>&1; then
    cat <<EOF

  MediaFlow demo is running on port $PORT (it opens in a new tab; or use the 'Ports' tab).

  Sign in with one click on the sign-in page, or with:
    admin     demo-admin@example.com
    manager   demo-manager@example.com
    member    demo-member@example.com
  Password for all: demo-password-123

EOF
    exit 0
  fi
  sleep 1
done

echo "The demo did not become ready within 60 s. Last lines of $LOG:" >&2
tail -n 30 "$LOG" >&2
exit 1
