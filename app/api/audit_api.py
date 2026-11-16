"""Reading the audit trail: `GET /audit-events`, for admins of the current workspace only."""

from __future__ import annotations

from datetime import timezone
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import select

from app.api.audit import ACTIONS, CATEGORIES
from app.api.database import AuditEvent
from app.api.dependencies import CurrentMember, DbSession
from app.api.roles import is_admin
from app.api.schemas import AuditEventView, AuditPage

router = APIRouter(prefix="/api/v1")

Category = Literal[CATEGORIES]  # type: ignore[valid-type]


def _view(event: AuditEvent) -> AuditEventView:
    created = event.created_at
    if created.tzinfo is None:  # SQLite returns naive UTC
        created = created.replace(tzinfo=timezone.utc)
    return AuditEventView(
        id=event.id, created_at=created, actor_id=event.actor_id,
        actor_email=event.actor_email, action=event.action, target_type=event.target_type,
        target_id=event.target_id, target_label=event.target_label, before=event.before,
        after=event.after, request_id=event.request_id,
    )


@router.get("/audit-events", response_model=AuditPage)
def list_audit_events(
    actor: CurrentMember,
    db: DbSession,
    category: Category | None = None,
    action: Annotated[str | None, Query(max_length=48)] = None,
    actor_id: Annotated[str | None, Query(max_length=36)] = None,
    target_id: Annotated[str | None, Query(max_length=64)] = None,
    before: Annotated[int | None, Query(ge=1)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
):
    """Newest first. Filter by category (`member`, `invitation`, `job`, `post`, `workspace`),
    exact action, who did it, or what it was done to (e.g. one job's whole history)."""
    if not is_admin(actor):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Admin role required")
    if action is not None and action not in ACTIONS:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, f"Unknown action {action!r}")
    query = select(AuditEvent).where(AuditEvent.workspace_id == actor.workspace_id)
    if category:
        query = query.where(AuditEvent.action.like(f"{category}.%"))
    if action:
        query = query.where(AuditEvent.action == action)
    if actor_id:
        query = query.where(AuditEvent.actor_id == actor_id)
    if target_id:
        query = query.where(AuditEvent.target_id == target_id)
    if before:
        query = query.where(AuditEvent.id < before)
    rows = db.scalars(query.order_by(AuditEvent.id.desc()).limit(limit + 1)).all()
    more = len(rows) > limit
    rows = rows[:limit]
    return AuditPage(
        items=[_view(e) for e in rows], next_before=rows[-1].id if more and rows else None
    )
