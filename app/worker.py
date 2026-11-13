"""Worker: claim -> limiter -> Gemini -> store result -> ack.
Failures never lose a job: retry (deferred with backoff), dead-letter, or fail permanently.

Two optional hooks connect the worker to the durable database (see docs/JOB_LIFECYCLE.md):
- on_result(job_id, result): MUST succeed before the job is acked; a failure re-defers the job
  and the next claim replays the result from the Redis cache instead of calling Gemini again.
- on_status(job_id, status, error): best effort (processing/retrying/failed/dead). If it fails,
  the reconciler copies the status from Redis later.
"""

from __future__ import annotations

import contextlib
import dataclasses
import logging
import threading
import time
from collections.abc import Callable

from app.errors import Permanent, RateLimited, Transient
from app.jobs import JobStore
from app.limiter import GeminiLimiter, backoff_s
from app.redis_queue import ReliableQueue

log = logging.getLogger("worker")


class Worker:
    def __init__(
        self,
        *,
        queue: ReliableQueue,
        store: JobStore,
        limiter: GeminiLimiter,
        client,
        max_attempts: int = 5,
        max_inline_wait_s: float = 30.0,
        sleep=time.sleep,
        backoff=backoff_s,
        on_result: Callable[[str, dict], None] | None = None,
        on_status: Callable[[str, str, str | None], None] | None = None,
        materialize: Callable[[str], contextlib.AbstractContextManager[str]] | None = None,
    ):
        self.queue, self.store, self.limiter, self.client = queue, store, limiter, client
        self.max_attempts, self.max_inline_wait_s = max_attempts, max_inline_wait_s
        self.sleep, self.backoff = sleep, backoff
        self.on_result = on_result
        self.on_status = on_status
        # Turns the job's media ref into a local file (identity for local storage).
        self.materialize = materialize or contextlib.nullcontext
        # (interval_s, fn, next_due) - housekeeping run from the worker loop.
        self._periodic: list[list] = []

    def every(self, interval_s: float, fn: Callable[[], object]) -> None:
        """Run `fn` from the worker loop at most once per `interval_s` (first run: immediately)."""
        self._periodic.append([interval_s, fn, 0.0])

    def _notify(self, job_id: str, status: str, error: str | None = None) -> None:
        if self.on_status is None:
            return
        try:
            self.on_status(job_id, status, error)
        except Exception:
            log.warning("could not record status %s for job %s; reconciler will retry",
                        status, job_id, exc_info=True)

    def process_one(self) -> str:
        """Returns: idle | missing | cached | deferred | done | retry | dead | failed"""
        jid = self.queue.claim()
        if jid is None:
            return "idle"
        job = self.store.get(jid)
        if job is None:
            self.queue.ack(jid)
            return "missing"
        if self.store.result_exists(job.cache_key):  # solved elsewhere / crash-replay
            result = self.store.get_result(job.cache_key)
            self.store.finish_cached(job)
            if result is not None and not self._persist_result(job.job_id, result):
                return "persist_retry"
            self.queue.ack(jid)
            return "cached"

        waited = 0.0
        while True:
            allowed, wait = self.limiter.try_acquire()
            if allowed:
                break
            if wait > self.max_inline_wait_s or waited + wait > self.max_inline_wait_s:
                self.store.mark(jid, status="queued")
                self.queue.defer(jid, wait)  # long wait (cooldown / daily cap): free the worker
                return "deferred"
            self.sleep(wait)
            waited += wait

        attempts = self.store.incr_attempts(jid)
        self.store.mark(jid, status="processing")
        self._notify(jid, "processing")
        try:
            with self.materialize(job.path) as local_path:
                spec = job.spec()
                if local_path != spec.path:
                    spec = dataclasses.replace(spec, path=local_path)
                result = self.client.analyse(spec)
        except Permanent as e:
            self.store.finish_failed(job, "failed", str(e))
            self._notify(jid, "failed", str(e))
            self.queue.ack(jid)
            return "failed"
        except (RateLimited, Transient) as e:
            return self._retry_or_dead(job, attempts, e, isinstance(e, RateLimited))
        except Exception as e:  # bug or unexpected: treat as transient, log traceback
            log.exception("unexpected error on job %s", jid)
            return self._retry_or_dead(job, attempts, e, False)
        self.store.finish_ok(job, result)
        if not self._persist_result(jid, result):
            return "persist_retry"
        self.queue.ack(jid)
        return "done"

    def _persist_result(self, job_id: str, result: dict) -> bool:
        if self.on_result is None:
            return True
        try:
            self.on_result(job_id, result)
        except Exception:
            log.exception("could not persist result for job %s; will retry", job_id)
            self.queue.defer(job_id, 1.0)
            return False
        return True

    def _retry_or_dead(self, job, attempts: int, err: Exception, rate_limited: bool) -> str:
        if attempts >= self.max_attempts:
            message = f"gave up after {attempts} attempts: {err}"
            self.store.finish_failed(job, "dead", message)
            self.queue.dead_letter(job.job_id)
            self._notify(job.job_id, "dead", message)
            return "dead"
        delay = self.backoff(attempts)
        if rate_limited:
            self.limiter.cooldown(delay)  # whole fleet pauses
        self.store.mark(job.job_id, status="retrying", error=str(err)[:300])
        self.queue.defer(job.job_id, delay)
        self._notify(job.job_id, "retrying", str(err)[:300])
        return "retry"

    def maintenance(self) -> None:
        self.queue.promote_deferred()
        self.queue.reap()
        now = time.monotonic()
        for task in self._periodic:
            interval, fn, due = task
            if now < due:
                continue
            task[2] = now + interval
            try:
                fn()
            except Exception:
                log.exception("periodic task %s failed", getattr(fn, "__name__", fn))

    def run(self, stop: threading.Event, poll_s: float = 1.0) -> None:
        while not stop.is_set():
            self.maintenance()
            if self.process_one() == "idle":
                stop.wait(poll_s)

    def drain(self, max_idle_rounds: int = 3, poll_s: float = 0.2) -> dict[str, int]:
        """Process until queue stays empty. For demos and tests."""
        counts: dict[str, int] = {}
        idle = 0
        while idle < max_idle_rounds:
            self.maintenance()
            out = self.process_one()
            counts[out] = counts.get(out, 0) + 1
            if out == "idle":
                if self.queue.depths()["deferred"] == 0:
                    idle += 1
                time.sleep(poll_s)
            else:
                idle = 0
        counts.pop("idle", None)
        return counts
