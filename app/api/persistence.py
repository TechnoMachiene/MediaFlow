"""Worker -> PostgreSQL writes. Redis is the working copy; these make the database durable.

Both writes are guarded by the transition table in app/api/lifecycle.py inside the UPDATE
itself, so a late hook (a slow worker whose lease expired, a replayed attempt) can never move a
row out of a final state, even when it races another writer."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone

from sqlalchemy import func, update
from sqlalchemy.orm import Session

from app.api.database import ApiJob
from app.api.lifecycle import allowed_from


def persist_api_result(session_factory: Callable[[], Session], job_id: str, result: dict) -> None:
    """Store a finished result on the job and on every same-workspace job that waited on it."""
    with session_factory.begin() as db:
        row = db.get(ApiJob, job_id)
        if row is None:
            return
        statement = update(ApiJob).where(ApiJob.id == job_id)
        if row.cache_key is not None:
            # cache_key is workspace-scoped, so followers in other tenants are never touched.
            statement = update(ApiJob).where(ApiJob.cache_key == row.cache_key)
        db.execute(
            statement.where(
                ApiJob.status.in_(allowed_from("done")),
                # A replayed or shared result must never overwrite a draft someone edited.
                ApiJob.draft_version == 1,
            ).values(
                result=result, status="done", error=None, updated_at=datetime.now(timezone.utc),
                completed_at=func.coalesce(ApiJob.completed_at, datetime.now(timezone.utc)),
            )
        )


def persist_api_status(
    session_factory: Callable[[], Session], job_id: str, status: str, error: str | None
) -> None:
    now = datetime.now(timezone.utc)
    stamps = {}
    if status == "processing":
        stamps["started_at"] = func.coalesce(ApiJob.started_at, now)
    elif status in ("failed", "dead"):
        stamps["completed_at"] = func.coalesce(ApiJob.completed_at, now)
    with session_factory.begin() as db:
        db.execute(
            update(ApiJob)
            .where(ApiJob.id == job_id, ApiJob.status.in_(allowed_from(status)))
            .values(status=status, error=(error or None) and error[:500], updated_at=now,
                    **stamps)
        )


def attach(worker, session_factory: Callable[[], Session]) -> None:
    """Wire a worker's hooks to the database."""
    worker.on_result = lambda job_id, result: persist_api_result(session_factory, job_id, result)
    worker.on_status = lambda job_id, status, error: persist_api_status(
        session_factory, job_id, status, error
    )
