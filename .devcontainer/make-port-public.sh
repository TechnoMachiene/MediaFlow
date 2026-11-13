#!/usr/bin/env bash
# Makes the demo port public, so the demo opens for whoever owns this Codespace even when
# their browser refuses GitHub's port sign-in cookie (Chrome shows "HTTP ERROR 401").
#
# Public means anyone *with this Codespace's URL* can open the demo. The URL is long and
# random, each Codespace signs logins with its own random key (start-demo.sh), and a demo
# holds only demo data. Make it private again in the Ports tab whenever you like.
#
# Runs on attach (after GitHub has forwarded the port). Never fails the Codespace.
set -uo pipefail
PORT=8082

if [ "${CODESPACES:-}" != "true" ] || [ -z "${CODESPACE_NAME:-}" ]; then
  exit 0  # not in a Codespace (local Docker / dev container): nothing to do
fi
if ! command -v gh >/dev/null 2>&1; then
  echo "gh not found; to share the demo, set port $PORT to Public in the Ports tab." >&2
  exit 0
fi

# The port is forwarded a moment after the server starts; retry for up to a minute.
for _ in $(seq 1 12); do
  if gh codespace ports visibility "$PORT:public" -c "$CODESPACE_NAME" >/dev/null 2>&1; then
    url="https://${CODESPACE_NAME}-${PORT}.${GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN:-app.github.dev}"
    echo "MediaFlow demo port $PORT is public: $url"
    exit 0
  fi
  sleep 5
done
echo "Could not make port $PORT public automatically. In the Ports tab, right-click $PORT >" \
     "Port Visibility > Public." >&2
exit 0
