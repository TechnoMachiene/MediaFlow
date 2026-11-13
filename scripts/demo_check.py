#!/usr/bin/env python3
"""Walk the complete MediaFlow workflow against a running demo, over HTTP only.

    python3 scripts/demo_check.py                     # default http://localhost:8081
    python3 scripts/demo_check.py http://localhost:8000

Standard library only, so it runs anywhere (CI, a fresh clone, inside a container):
member uploads -> draft generated -> manager approves -> member schedules -> calendar shows it.
Exits non-zero with the failing step if anything is off.
"""

from __future__ import annotations

import json
import struct
import sys
import time
import urllib.error
import urllib.request
import uuid
import zlib
from datetime import datetime, timedelta, timezone

PASSWORD = "demo-password-123"
MEMBER, MANAGER = "demo-member@example.com", "demo-manager@example.com"


def png(width: int, height: int, rgb: tuple[int, int, int]) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    row = b"\x00" + bytes(rgb) * width
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(row * height))
        + chunk(b"IEND", b"")
    )


class Demo:
    def __init__(self, base: str):
        self.base = base.rstrip("/")

    def call(self, method, path, *, token=None, body=None, files=None, expect=(200,)):
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        data = None
        if files:
            boundary = uuid.uuid4().hex
            name, content, mime = files
            data = (
                f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
                f'filename="{name}"\r\nContent-Type: {mime}\r\n\r\n'
            ).encode() + content + f"\r\n--{boundary}--\r\n".encode()
            headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
        elif body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(self.base + path, data, headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                status, raw = response.status, response.read()
        except urllib.error.HTTPError as e:
            status, raw = e.code, e.read()
        if status not in expect:
            raise SystemExit(f"FAIL {method} {path}: HTTP {status} {raw[:300]!r}")
        return json.loads(raw) if raw else None

    def login(self, email):
        return self.call("POST", "/api/v1/auth/login",
                         body={"email": email, "password": PASSWORD})["access_token"]


def step(message):
    print(f"  ok  {message}", flush=True)


def main(base: str = "http://localhost:8081") -> int:
    demo = Demo(base)
    print(f"Checking the demo workflow at {base}")

    for _ in range(60):  # give a just-started stack a moment
        try:
            demo.call("GET", "/health/ready")
            break
        except (SystemExit, OSError):
            time.sleep(1)
    else:
        raise SystemExit(f"FAIL {base} never became ready")
    meta = demo.call("GET", "/api/v1/meta")
    if not meta["demo_mode"] or meta["demo_password"] != PASSWORD:
        raise SystemExit(f"FAIL {base} is not running in demo mode: {meta}")
    step("API is ready and in demo mode")

    member, manager = demo.login(MEMBER), demo.login(MANAGER)
    me = demo.call("GET", "/api/v1/auth/me", token=member)
    step(f"signed in as {MEMBER} ({me['role']} of '{me['workspace']['name']}')")

    image = png(160, 90, tuple(uuid.uuid4().bytes[:3]))  # fresh bytes: never a cache hit
    job = demo.call("POST", "/api/v1/jobs", token=member,
                    files=("demo-check.png", image, "image/png"), expect=(200, 202))
    job_id = job["job_id"]
    deadline = time.monotonic() + 90
    while job["status"] != "done":
        if job["status"] in ("failed", "dead", "cancelled") or time.monotonic() > deadline:
            raise SystemExit(f"FAIL analysis ended as {job['status']}: {job.get('error')}")
        time.sleep(0.5)
        job = demo.call("GET", f"/api/v1/jobs/{job_id}", token=member)
    draft = json.loads(job["result"]["text"])
    if job["result"]["model"] != "demo" or "instagram" not in draft:
        raise SystemExit(f"FAIL unexpected draft: {job['result']}")
    step(f"uploaded and analysed: {draft['content_summary']['main_message']!r}")

    queue = demo.call("GET", "/api/v1/jobs/review?limit=100", token=manager)["items"]
    if job_id not in {item["job_id"] for item in queue}:
        raise SystemExit("FAIL job is missing from the manager's review queue")
    demo.call("PATCH", f"/api/v1/jobs/{job_id}/approval", token=manager,
              body={"status": "approved"})
    step(f"approved by {MANAGER}")

    when = (datetime.now(timezone.utc) + timedelta(days=2)).replace(microsecond=0)
    post = demo.call("POST", f"/api/v1/jobs/{job_id}/schedule", token=member,
                     body={"scheduled_at": when.isoformat(), "timezone": "UTC"}, expect=(201,))
    day = post["local_date"]
    calendar = demo.call(
        "GET", f"/api/v1/calendar?start={day}&end={day}&timezone=UTC", token=member
    )
    if post["id"] not in {p["id"] for d in calendar["days"] for p in d["posts"]}:
        raise SystemExit("FAIL scheduled post is missing from the calendar")
    step(f"scheduled for {day} and shown on the calendar")

    print("Demo workflow passed: upload -> draft -> review -> schedule -> calendar")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(*sys.argv[1:2]))
