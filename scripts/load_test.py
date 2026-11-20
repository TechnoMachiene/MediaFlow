"""Burst test: N simultaneous requests per scenario against a running MediaFlow.

    python scripts/load_test.py                       # the demo stack on :8081, 100 at once
    python scripts/load_test.py http://localhost:8081 --concurrency 100 --only login,me

Signs in with the demo accounts (docs: README, "Try the demo"), so point it at a demo stack,
never at production. Uploads create real jobs in that demo workspace.

Scenarios: health (GET /health/live), me (GET /api/v1/auth/me), login (POST
/api/v1/auth/login), upload (POST /api/v1/jobs, a distinct small PNG each). Each line shows
how many succeeded, the status codes, and average / p95 / max time.
"""

from __future__ import annotations

import argparse
import asyncio
import io
import statistics
import struct
import time
import zlib
from collections import Counter

import httpx

DEMO_PASSWORD = "demo-password-123"
MEMBER = "demo-member@example.com"


def png(seed: int) -> bytes:
    """A tiny distinct PNG (no Pillow needed), so uploads aren't deduplicated."""
    r, g, b = seed % 256, (seed // 256) % 256, (seed * 7) % 256
    raw = b"".join(b"\x00" + bytes([r, g, b]) * 8 for _ in range(8))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + kind + data
                + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF))

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 8, 8, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


async def burst(name: str, n: int, call, retry: bool = False) -> dict:
    async def one(i: int):
        started = time.perf_counter()
        try:
            response = await call(i)
            # Like the web app (frontend/src/api.ts): a busy answer is retried once, after the
            # wait the server asks for.
            wait = response.headers.get("retry-after", "")
            if retry and response.status_code == 503 and wait.isdigit() and int(wait) <= 5:
                await asyncio.sleep(int(wait))
                response = await call(i)
            status = response.status_code
        except httpx.HTTPError as err:
            status = type(err).__name__
        return status, time.perf_counter() - started

    began = time.perf_counter()
    results = await asyncio.gather(*(one(i) for i in range(n)))
    wall = time.perf_counter() - began
    times = sorted(t for _, t in results)
    codes = Counter(s for s, _ in results)
    ok = sum(c for s, c in codes.items() if isinstance(s, int) and s < 400)
    p95 = times[max(0, int(len(times) * 0.95) - 1)]
    avg = statistics.mean(times) * 1000
    line = (f"{name:<7} {ok:>3}/{n} ok  codes {dict(codes)}  avg {avg:6.0f} ms"
            f"  p95 {p95*1000:6.0f} ms  max {times[-1]*1000:6.0f} ms  (wall {wall:.1f} s)")
    print(line, flush=True)
    return {"name": name, "ok": ok, "n": n, "codes": codes}


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("base", nargs="?", default="http://localhost:8081")
    parser.add_argument("--concurrency", type=int, default=100)
    parser.add_argument("--only", default="health,me,login,upload")
    parser.add_argument("--retry", action="store_true",
                        help="retry a 503 once after Retry-After, as the web app does")
    args = parser.parse_args()
    n, wanted = args.concurrency, set(args.only.split(","))

    limits = httpx.Limits(max_connections=n, max_keepalive_connections=n)
    async with httpx.AsyncClient(base_url=args.base, timeout=120, limits=limits) as http:
        token = (await http.post("/api/v1/auth/login", json={
            "email": MEMBER, "password": DEMO_PASSWORD})).json().get("access_token")
        if not token:
            print("Could not sign in as the demo member: is this a demo stack?")
            return 2
        auth = {"Authorization": f"Bearer {token}"}
        failed = False
        if "health" in wanted:
            r = await burst("health", n, lambda i: http.get("/health/live"))
            failed |= r["ok"] < n
        if "me" in wanted:
            r = await burst("me", n, lambda i: http.get("/api/v1/auth/me", headers=auth))
            failed |= r["ok"] < n
        if "login" in wanted:
            r = await burst("login", n, lambda i: http.post("/api/v1/auth/login", json={
                "email": MEMBER, "password": DEMO_PASSWORD}), retry=args.retry)
            failed |= r["codes"].get(500, 0) > 0
        if "upload" in wanted:
            stamp = int(time.time())
            r = await burst("upload", n, lambda i: http.post(
                "/api/v1/jobs", headers={**auth, "Idempotency-Key": f"load-{stamp}-{i}"},
                files={"file": (f"load-{i}.png", io.BytesIO(png(stamp * 1000 + i)),
                                "image/png")}))
            failed |= r["codes"].get(500, 0) > 0
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
