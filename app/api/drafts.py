"""Draft editor: version history and review comments on a job's draft.

Who: the person who uploaded the job, and the workspace's managers and admins (the same people
who can already see it). Everything is scoped through the job, so it inherits the job's tenant
boundary.

Versions
  * Version 1 is Gemini's draft. It is stored the first time someone edits, so jobs that are
    never edited cost nothing.
  * A save names its `base_version`. If someone saved in between, the save is refused (409)
    rather than overwriting their work; a unique (job, number) index backs this up.
  * Saving puts the new text in `api_jobs.result`, so every screen shows the latest version,
    and sets `draft_version`, which stops the queue from ever overwriting the edit.
  * Editing an approved or rejected draft sends it back to review. A draft on the calendar
    can't be edited until it is taken off, so nothing scheduled changes behind a reviewer.

Comments
  * Anchored to the version they were written on and, optionally, to one field
    (`instagram.caption`), so a comment about old text is still understandable later.
  * One level of replies. Top-level comments can be resolved and reopened.
  * An approve/reject decision can carry a note, which joins the thread as its own kind.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api import audit
from app.api.database import ApiJob, DraftComment, DraftVersion, ScheduledPost
from app.api.dependencies import CurrentMember, DbSession, Member
from app.api.roles import is_reviewer
from app.api.schemas import (
    CommentCreate,
    CommentUpdate,
    CommentView,
    DraftSave,
    DraftSaved,
    DraftVersionView,
)

router = APIRouter(prefix="/api/v1")

CONFLICT = "Someone saved version {n} while you were editing. Reload to see it, then edit again."


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _job(db: Session, actor: Member, job_id: str, *, lock: bool = False) -> ApiJob:
    """The job, if the caller may work on its draft: its uploader, or a reviewer. 404 otherwise,
    exactly like the job endpoints, so nothing about other people's jobs leaks."""
    query = select(ApiJob).where(ApiJob.id == job_id, ApiJob.workspace_id == actor.workspace_id)
    if not is_reviewer(actor):
        query = query.where(ApiJob.owner_id == actor.id)
    if lock:
        query = query.with_for_update()
    job = db.scalar(query)
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Job not found")
    return job


def _require_draft(job: ApiJob) -> None:
    if job.status != "done" or job.result is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "This job has no draft yet")


def _structured(text: str) -> bool:
    """Same rule as the web app's parseContent: a JSON object (optionally fenced)."""
    body = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        return isinstance(json.loads(body), dict)
    except ValueError:
        return False


def _version_view(v: DraftVersion, current: int) -> DraftVersionView:
    return DraftVersionView(
        number=v.number, text=v.text, source=v.source, restored_from=v.restored_from,
        note=v.note, author_email=v.author_email, created_at=_utc(v.created_at),
        current=v.number == current,
    )


def _stored_versions(db: Session, job: ApiJob) -> list[DraftVersion]:
    return db.scalars(
        select(DraftVersion).where(DraftVersion.job_id == job.id).order_by(DraftVersion.number)
    ).all()


def _original(job: ApiJob) -> DraftVersion:
    """Gemini's draft as version 1 (not yet added to the session)."""
    return DraftVersion(
        job_id=job.id, workspace_id=job.workspace_id, number=1, text=job.result["text"],
        source="ai", created_at=_utc(job.updated_at or job.created_at),
    )


@router.get("/jobs/{job_id}/versions", response_model=list[DraftVersionView])
def list_versions(job_id: str, actor: CurrentMember, db: DbSession):
    """Every version, newest first. A never-edited draft has just version 1."""
    job = _job(db, actor, job_id)
    if job.result is None:
        return []
    versions = _stored_versions(db, job) or [_original(job)]
    return [_version_view(v, job.draft_version) for v in reversed(versions)]


@router.post(
    "/jobs/{job_id}/versions", response_model=DraftSaved, status_code=status.HTTP_201_CREATED
)
def save_version(job_id: str, payload: DraftSave, actor: CurrentMember, db: DbSession):
    job = _job(db, actor, job_id, lock=True)  # serializes concurrent saves on PostgreSQL
    _require_draft(job)
    if payload.base_version != job.draft_version:
        raise HTTPException(
            status.HTTP_409_CONFLICT, CONFLICT.format(n=job.draft_version),
            headers={"X-Current-Version": str(job.draft_version)},
        )
    if db.scalar(select(ScheduledPost.id).where(
        ScheduledPost.job_id == job.id, ScheduledPost.status == "scheduled"
    )):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "This draft is on the calendar. Take it off the calendar before editing it.",
        )

    versions = _stored_versions(db, job)
    if not versions:
        versions = [_original(job)]
        db.add(versions[0])
    if payload.restore is not None:
        source = next((v for v in versions if v.number == payload.restore), None)
        if source is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Version not found")
        text, kind = source.text, "restore"
    else:
        text, kind = payload.text, "edit"
    if not text.strip():
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "The draft can't be empty")
    if text == job.result["text"]:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Nothing changed")
    if _structured(versions[0].text) and not _structured(text):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "This draft is organised by platform; keep it as a JSON object",
        )

    number = job.draft_version + 1
    version = DraftVersion(
        job_id=job.id, workspace_id=job.workspace_id, number=number, text=text, source=kind,
        restored_from=payload.restore, note=(payload.note or "").strip() or None,
        author_id=actor.id, author_email=actor.email,
    )
    db.add(version)
    previous_approval = job.approval_status
    returned = previous_approval != "pending"
    audit.record(
        db, workspace_id=job.workspace_id, actor=actor, action="draft.edited",
        target_type="job", target_id=job.id, target_label=job.filename,
        before={"version": job.draft_version, "approval_status": previous_approval},
        after={"version": number, "approval_status": "pending",
               **({"restored_from": payload.restore} if payload.restore else {})},
    )
    job.result = {**job.result, "text": text}
    job.draft_version = number
    job.approval_status = "pending"
    try:
        db.commit()
    except IntegrityError:  # a concurrent save took this number first (SQLite has no row lock)
        db.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, CONFLICT.format(n=number)) from None
    return DraftSaved(
        version=_version_view(version, number), approval_status="pending",
        returned_to_review=returned,
    )


# ── Comments ──────────────────────────────────────────────────────────────────────────────


def _comment_view(c: DraftComment) -> CommentView:
    return CommentView(
        id=c.id, parent_id=c.parent_id, version=c.version, field=c.field, kind=c.kind,
        body=c.body, author_email=c.author_email, created_at=_utc(c.created_at),
        resolved_at=_utc(c.resolved_at) if c.resolved_at else None,
        resolved_by_email=c.resolved_by_email,
    )


def add_comment(db: Session, job: ApiJob, actor: Member, body: str, *, kind: str = "comment",
                field: str | None = None, parent_id: str | None = None) -> DraftComment:
    """Stage a comment (caller commits). Also used for approve/reject notes."""
    comment = DraftComment(
        job_id=job.id, workspace_id=job.workspace_id, parent_id=parent_id,
        version=job.draft_version, field=field, kind=kind, body=body,
        author_id=actor.id, author_email=actor.email,
    )
    db.add(comment)
    return comment


@router.get("/jobs/{job_id}/comments", response_model=list[CommentView])
def list_comments(job_id: str, actor: CurrentMember, db: DbSession):
    """Oldest first; replies carry `parent_id`."""
    job = _job(db, actor, job_id)
    rows = db.scalars(
        select(DraftComment).where(DraftComment.job_id == job.id)
        .order_by(DraftComment.created_at, DraftComment.id)
    ).all()
    return [_comment_view(c) for c in rows]


@router.post(
    "/jobs/{job_id}/comments", response_model=CommentView, status_code=status.HTTP_201_CREATED
)
def create_comment(job_id: str, payload: CommentCreate, actor: CurrentMember, db: DbSession):
    job = _job(db, actor, job_id)
    _require_draft(job)
    parent_id = None
    if payload.parent_id:
        parent = db.scalar(select(DraftComment).where(
            DraftComment.id == payload.parent_id, DraftComment.job_id == job.id
        ))
        if parent is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Comment not found")
        parent_id = parent.parent_id or parent.id  # replies stay one level deep
    comment = add_comment(db, job, actor, payload.body, field=payload.field, parent_id=parent_id)
    db.commit()
    return _comment_view(comment)


@router.patch("/jobs/{job_id}/comments/{comment_id}", response_model=CommentView)
def update_comment(job_id: str, comment_id: str, payload: CommentUpdate, actor: CurrentMember,
                   db: DbSession):
    """Resolve or reopen a thread."""
    job = _job(db, actor, job_id)
    comment = db.scalar(select(DraftComment).where(
        DraftComment.id == comment_id, DraftComment.job_id == job.id
    ))
    if comment is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Comment not found")
    if comment.parent_id is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "Resolve the thread, not a reply")
    if payload.resolved and comment.resolved_at is None:
        comment.resolved_at, comment.resolved_by_email = datetime.now(timezone.utc), actor.email
    elif not payload.resolved:
        comment.resolved_at = comment.resolved_by_email = None
    db.commit()
    return _comment_view(comment)

