"""Worker: claim -> limiter -> Gemini -> store result -> ack.
Failures never lose a job: retry (deferred with backoff), dead-letter, or fail permanently."""

from __future__ import annotations

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
    ):
        self.queue, self.store, self.limiter, self.client = queue, store, limiter, client
        self.max_attempts, self.max_inline_wait_s = max_attempts, max_inline_wait_s
        self.sleep, self.backoff = sleep, backoff
        self.on_result = on_result

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
        try:
            result = self.client.analyse(job.spec())
        except Permanent as e:
            self.store.finish_failed(job, "failed", str(e))
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
            self.store.finish_failed(job, "dead", f"gave up after {attempts} attempts: {err}")
            self.queue.dead_letter(job.job_id)
            return "dead"
        delay = self.backoff(attempts)
        if rate_limited:
            self.limiter.cooldown(delay)  # whole fleet pauses
        self.store.mark(job.job_id, status="retrying", error=str(err)[:300])
        self.queue.defer(job.job_id, delay)
        return "retry"

    def maintenance(self) -> None:
        self.queue.promote_deferred()
        self.queue.reap()

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
