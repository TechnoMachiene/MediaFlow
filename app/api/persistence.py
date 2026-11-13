"""Worker -> PostgreSQL writes. Redis is the working copy; these make the database durable."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone

from sqlalchemy import update
from sqlalchemy.orm import Session

from app.api.database import ApiJob

# A status write must never move a job backwards out of these.
FINAL = ("done", "cancelled")


def persist_api_result(session_factory: Callable[[], Session], job_id: str, result: dict) -> None:
    """Store a finished result on the job and on every same-workspace job that waited on it."""
    with session_factory.begin() as db:
        row = db.get(ApiJob, job_id)
        if row is None:
            return
        statement = update(ApiJob).where(ApiJob.id == job_id)
        if row.cache_key is not None:
            # cache_key is workspace-scoped, so followers in other tenants are never touched.
            statement = update(ApiJob).where(
                ApiJob.cache_key == row.cache_key, ApiJob.status != "cancelled"
            )
        db.execute(
            statement.values(
                result=result, status="done", error=None, updated_at=datetime.now(timezone.utc)
            )
        )


def persist_api_status(
    session_factory: Callable[[], Session], job_id: str, status: str, error: str | None
) -> None:
    with session_factory.begin() as db:
        db.execute(
            update(ApiJob)
            .where(ApiJob.id == job_id, ApiJob.status.not_in(FINAL))
            .values(
                status=status,
                error=(error or None) and error[:500],
                updated_at=datetime.now(timezone.utc),
            )
        )


def attach(worker, session_factory: Callable[[], Session]) -> None:
    """Wire a worker's hooks to the database."""
    worker.on_result = lambda job_id, result: persist_api_result(session_factory, job_id, result)
    worker.on_status = lambda job_id, status, error: persist_api_status(
        session_factory, job_id, status, error
    )
