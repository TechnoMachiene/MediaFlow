from __future__ import annotations

import math
import os
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import anyio
import anyio.to_thread
import jwt
from anyio.lowlevel import RunVar
from pwdlib import PasswordHash

password_hash = PasswordHash.recommended()


# ── Bounded password hashing ───────────────────────────────────────────────────────────────
# Argon2 is slow and memory-hard on purpose (the recommended settings use 64 MiB per hash).
# Unbounded, a burst of 100 sign-ins would run up to the thread pool's 40 hashes at once:
# about 2.5 GiB of memory with every CPU saturated. At most PASSWORD_HASH_CONCURRENCY run at
# once (default: the CPUs this container may use); the rest wait their turn, up to
# PASSWORD_HASH_WAIT_S (default 30 s), then get "busy, try again" (503 with Retry-After).
#
# Sign-in waits in an async queue (verify_password_async), so a waiting sign-in holds no
# request thread: a burst of sign-ins doesn't slow down everything else while it queues.


class HashingBusy(Exception):
    """Too many password checks are already running; the caller should retry shortly."""


def _env_int(name: str, default: int, low: int) -> int:
    try:
        return max(low, int(os.environ.get(name, "").strip() or default))
    except ValueError:
        return default


def effective_cpus() -> int:
    """CPUs this process may really use. os.cpu_count() reports the host's, ignoring a
    container's CPU limit (Docker --cpus, Kubernetes limits): with 1 CPU allowed on a 4-CPU
    host it says 4, and 4 concurrent hashes then each run four times slower."""
    count = os.cpu_count() or 1
    try:  # cgroup v2: "max 100000" or "<quota> <period>"
        quota, period = open("/sys/fs/cgroup/cpu.max").read().split()[:2]
        if quota != "max":
            count = min(count, math.ceil(int(quota) / int(period)))
    except (OSError, ValueError):
        try:  # cgroup v1
            quota = int(open("/sys/fs/cgroup/cpu/cpu.cfs_quota_us").read())
            period = int(open("/sys/fs/cgroup/cpu/cpu.cfs_period_us").read())
            if quota > 0:
                count = min(count, math.ceil(quota / period))
        except (OSError, ValueError):
            pass
    return max(1, count)


HASH_CONCURRENCY = _env_int("PASSWORD_HASH_CONCURRENCY", effective_cpus(), 1)
HASH_WAIT_S = float(_env_int("PASSWORD_HASH_WAIT_S", 30, 1))
_slots = threading.BoundedSemaphore(HASH_CONCURRENCY)
_queue: RunVar[anyio.CapacityLimiter] = RunVar("password_hash_queue")


@contextmanager
def _hashing_slot():
    if not _slots.acquire(timeout=HASH_WAIT_S):
        raise HashingBusy()
    try:
        yield
    finally:
        _slots.release()


def hash_password(password: str) -> str:
    with _hashing_slot():
        return password_hash.hash(password)


def verify_password(password: str, hashed: str) -> bool:
    with _hashing_slot():
        return password_hash.verify(password, hashed)


# Checked when the email has no account, so an unknown address takes as long as a wrong
# password: the response time doesn't reveal which emails have accounts.
_dummy_hash: str | None = None


def verify_password_or_dummy(password: str, hashed: str | None) -> bool:
    global _dummy_hash
    if hashed is None:
        if _dummy_hash is None:
            _dummy_hash = hash_password("not-a-real-password")
        verify_password(password, _dummy_hash)
        return False
    return verify_password(password, hashed)


async def verify_password_async(password: str, hashed: str | None) -> bool:
    """verify_password_or_dummy for async routes: waits its turn (first come, first served)
    without holding a thread, then checks on a worker thread."""
    try:
        queue = _queue.get()
    except LookupError:  # one queue per event loop
        queue = anyio.CapacityLimiter(HASH_CONCURRENCY)
        _queue.set(queue)
    with anyio.move_on_after(HASH_WAIT_S) as waited:
        await queue.acquire()
    if waited.cancelled_caught:
        raise HashingBusy()
    try:
        return await anyio.to_thread.run_sync(verify_password_or_dummy, password, hashed)
    finally:
        queue.release()


def create_access_token(subject: str, secret: str, ttl_minutes: int) -> str:
    return create_versioned_access_token(subject, secret, ttl_minutes, token_version=0)


def create_versioned_access_token(
    subject: str,
    secret: str,
    ttl_minutes: int,
    *,
    token_version: int,
) -> str:
    now = datetime.now(timezone.utc)
    return jwt.encode(
        {
            "sub": subject,
            "ver": token_version,
            "iat": now,
            "exp": now + timedelta(minutes=ttl_minutes),
        },
        secret,
        algorithm="HS256",
    )


def decode_access_token(token: str, secret: str) -> str | None:
    try:
        payload = jwt.decode(token, secret, algorithms=["HS256"])
    except jwt.InvalidTokenError:
        return None
    subject = payload.get("sub")
    return subject if isinstance(subject, str) else None


def decode_versioned_access_token(token: str, secret: str) -> tuple[str, int] | None:
    try:
        payload = jwt.decode(token, secret, algorithms=["HS256"])
    except jwt.InvalidTokenError:
        return None
    subject = payload.get("sub")
    version = payload.get("ver")
    if (
        not isinstance(subject, str)
        or not isinstance(version, int)
        or isinstance(version, bool)
        or version < 0
    ):
        return None
    return subject, version
