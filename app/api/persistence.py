from __future__ import annotations

from collections.abc import Callable

from sqlalchemy import update
from sqlalchemy.orm import Session

from app.api.database import ApiJob


def persist_api_result(session_factory: Callable[[], Session], job_id: str, result: dict) -> None:
    with session_factory.begin() as db:
        row = db.get(ApiJob, job_id)
        if row is None:
            return
        statement = update(ApiJob).where(ApiJob.id == job_id)
        if row.cache_key is not None:
            statement = update(ApiJob).where(ApiJob.cache_key == row.cache_key)
        db.execute(statement.values(result=result, status="done"))
