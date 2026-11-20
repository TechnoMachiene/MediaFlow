#!/usr/bin/env python3
"""Run the real app for browser end-to-end tests (frontend/e2e, Playwright).

Demo mode, so no Gemini key is needed and drafts come from the offline writer, but everything
else is the real system: API, Redis queue, embedded worker, SQLite database and the built web
app. Each run gets a fresh database and upload directory in a temporary folder and its own
Redis prefix, all removed on exit, so runs never see each other's data or yours.

    python scripts/e2e_server.py [port]      # default 8093; Playwright starts it for you
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main(port: str = "8093") -> int:
    if not (ROOT / "frontend" / "dist" / "index.html").is_file():
        print("build the web app first: cd frontend && npm run build", file=sys.stderr)
        return 2
    prefix = f"demo-e2e-{uuid.uuid4().hex[:8]}"
    with tempfile.TemporaryDirectory(prefix="mediaflow-e2e-") as work:
        env = {
            **os.environ,
            "DEMO_MODE": "1", "GEMINI_API_KEY": "", "EMBEDDED_WORKER": "1",
            "DATABASE_URL": f"sqlite:///{work}/e2e.sqlite3", "QUEUE_PREFIX": prefix,
            "MEDIA_STORAGE": "local", "MEDIA_STORAGE_DIR": f"{work}/uploads",
            "ALLOW_SIGNUP": "0", "LOG_FORMAT": "text", "LOG_LEVEL": "WARNING",
        }
        server = subprocess.Popen(
            # Same keep-alive as the image (Dockerfile): idle connections aren't cut at 5 s.
            [sys.executable, "-m", "uvicorn", "app.api.main:app", "--port", port,
             "--timeout-keep-alive", "75"],
            cwd=ROOT, env=env,
        )

        def stop(*_):
            server.terminate()

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        try:
            return server.wait()
        finally:
            if server.poll() is None:
                server.kill()
            _forget(prefix)


def _forget(prefix: str) -> None:
    try:
        import redis

        r = redis.Redis.from_url(os.environ.get("REDIS_URL", "redis://localhost:6379/0"))
        for key in r.scan_iter(match=f"{prefix}:*", count=500):
            r.delete(key)
    except Exception as err:  # best effort: a leftover prefix is harmless, never reused
        print(f"could not clear Redis keys {prefix}:*: {err}", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main(*sys.argv[1:2]))
