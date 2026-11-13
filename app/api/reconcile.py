"""Keep PostgreSQL (durable intent) and Redis (working queue) in agreement.

PostgreSQL is the source of truth for *which jobs exist*; Redis is the source of truth for
*where a job is in processing*. Writes to the two cannot share a transaction, so every
cross-store step is either idempotent or repaired here. The full failure matrix, with the test
that covers each row, is in docs/JOB_LIFECYCLE.md.

run_once() does two sweeps:

1. Dispatch: rows accepted by the API but never handed to Redis (`enqueued_at IS NULL`), e.g.
   the API process died between its commit and its Redis call. They are submitted now.
2. Sync: dispatched rows that the database still thinks are active. Redis is asked for the
   live state and the row is brought up to date; jobs whose Redis records vanished are
   re-submitted (bounded by `max_recoveries`) and ids missing from every queue structure are
   pushed back to `ready`.

Safe to run from many workers at once: a short Redis lock elects one runner per sweep, and
row locks (`FOR UPDATE SKIP LOCKED` on PostgreSQL) guard each row.
"""

from __future__ import annotations

import logging
import uuid
from collections import Counter
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.database import ACTIVE_STATUSES, ApiJob
from app.api.media_storage import locate_job_media
from app.jobs import JobStore
from app.media import ALLOWED, sha256_file
from app.redis_queue import ReliableQueue
from app.storage import MediaStorage

log = logging.getLogger("reconcile")

MEDIA_GONE = "The original upload is no longer available; upload it again."
LOST = "The job was lost from the queue repeatedly and was stopped; upload it again."


def dispatch_job(
    db: Session, row: ApiJob, store: JobStore, storage: MediaStorage, *, force: bool = False
) -> dict:
    """Hand a job row to Redis and record that on the row (caller commits).

    Idempotent per job id: dispatching a job that Redis already knows returns its status."""
    where = locate_job_media(db, row, storage)
    now = datetime.now(timezone.utc)
    if where.storage_ref is None:
        row.status, row.error = "failed", MEDIA_GONE
        return {"job_id": row.id, "status": "failed", "cached": False, "cache_key": None}
    sha256 = row.media_sha256
    if sha256 is None:  # legacy upload stored before content hashing
        with storage.materialize(where.storage_ref) as local:
            sha256 = sha256_file(local)
    mime = ALLOWED[Path(row.filename).suffix.lower()][1]
    out = store.submit_prepared(
        row.owner_id,
        media_ref=where.storage_ref,
        kind=row.media_kind,
        mime=mime,
        sha256=sha256,
        job_id=row.id,
        scope=row.workspace_id,
        force=force,
    )
    row.cache_key = out["cache_key"]
    row.enqueued_at = now
    row.status = out["status"]
    row.updated_at = now
    if out["status"] == "done":
        row.result = store.status(row.id).get("result") or row.result
    return out


class Reconciler:
    def __init__(
        self,
        session_factory: Callable[[], Session],
        store: JobStore,
        queue: ReliableQueue,
        storage: MediaStorage,
        *,
        grace_s: float = 60.0,
        batch: int = 200,
        max_recoveries: int = 3,
    ):
        self.sessions, self.store, self.queue, self.storage = (
            session_factory, store, queue, storage,
        )
        self.grace_s, self.batch, self.max_recoveries = grace_s, batch, max_recoveries
        self._lock_key = f"{store.p}:reconcile:lock"

    def run_once(self) -> dict[str, int]:
        token = uuid.uuid4().hex
        if not self.store.r.set(self._lock_key, token, nx=True, ex=max(5, int(self.grace_s))):
            return {"skipped": 1}
        try:
            counts: Counter[str] = Counter()
            self._dispatch_pending(counts)
            self._sync_active(counts)
            if counts:
                log.info("reconciled: %s", dict(counts))
            return dict(counts)
        finally:
            self.store._del_if_eq(keys=[self._lock_key], args=[token])

    def _cutoff(self) -> datetime:
        return datetime.now(timezone.utc) - timedelta(seconds=self.grace_s)

    def _locked_row(self, db: Session, job_id: str, *conditions) -> ApiJob | None:
        return db.scalar(
            select(ApiJob)
            .where(ApiJob.id == job_id, ApiJob.status.in_(ACTIVE_STATUSES), *conditions)
            .with_for_update(skip_locked=True)
        )

    def _dispatch_pending(self, counts: Counter) -> None:
        cutoff = self._cutoff()
        pending = (ApiJob.enqueued_at.is_(None), ApiJob.created_at < cutoff)
        with self.sessions() as db:
            ids = db.scalars(
                select(ApiJob.id)
                .where(ApiJob.status.in_(ACTIVE_STATUSES), *pending)
                .order_by(ApiJob.created_at)
                .limit(self.batch)
            ).all()
        for job_id in ids:
            with self.sessions.begin() as db:
                row = self._locked_row(db, job_id, *pending)
                if row is None:
                    continue
                out = dispatch_job(db, row, self.store, self.storage)
                counts["failed" if out["status"] == "failed" else "dispatched"] += 1

    def _sync_active(self, counts: Counter) -> None:
        cutoff = self._cutoff()
        stale = (ApiJob.enqueued_at.is_not(None), ApiJob.updated_at < cutoff)
        with self.sessions() as db:
            ids = db.scalars(
                select(ApiJob.id)
                .where(ApiJob.status.in_(ACTIVE_STATUSES), *stale)
                .order_by(ApiJob.updated_at)
                .limit(self.batch)
            ).all()
        for job_id in ids:
            with self.sessions.begin() as db:
                row = self._locked_row(db, job_id, *stale)
                if row is None:
                    continue
                counts[self._sync_one(db, row)] += 1

    def _sync_one(self, db: Session, row: ApiJob) -> str:
        now = datetime.now(timezone.utc)
        live = self.store.status(row.id)
        status = live["status"]
        row.updated_at = now
        if status == "unknown":
            # Redis no longer knows the job (data loss, flush, or TTL expiry before it ran).
            if row.result is not None:
                row.status, row.error = "done", None
                return "synced"
            if row.recoveries >= self.max_recoveries:
                row.status, row.error = "failed", LOST
                return "abandoned"
            row.recoveries += 1
            out = dispatch_job(db, row, self.store, self.storage, force=True)
            return "failed" if out["status"] == "failed" else "recovered"
        if status == "done" and live.get("result") is not None:
            row.status, row.result, row.error = "done", live["result"], None
            return "synced"
        if status in ("failed", "dead", "cancelled"):
            row.status, row.error = status, live.get("error")
            return "synced"
        if status in ("queued", "retrying", "processing") and self.queue.requeue_if_lost(row.id):
            # The hash survived but the id is in no queue structure: put it back on `ready`.
            # At-least-once: if a worker had in fact just finished it, the next claim is
            # answered from the result cache without calling Gemini.
            row.status = "queued"
            return "requeued"
        row.status = status
        return "unchanged"
