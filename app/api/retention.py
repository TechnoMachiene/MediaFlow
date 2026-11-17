"""Deletion policies for uploaded media, applied per workspace. See docs/STORAGE.md.

Originals are content-addressed and shared: two workspaces that upload the same bytes share
one stored object. So deletion happens in two steps:

1. **Per job** (`media_deleted_at`): a workspace's policy, or a person, removes *that job's*
   original. From then on its media and every link to it answer 410, even if another job
   (perhaps in another tenant) still keeps the same bytes. Drafts, approvals, calendar entries
   and publications are kept.
2. **Per object** (`media_assets.purged_at`): the stored bytes are deleted once no job anywhere
   still has them. Uploading the same bytes again stores them again for the new job only.

Each workspace's policy, within the operator's ceiling (MEDIA_RETENTION_MAX_DAYS):

* `originals_days` (default MEDIA_RETENTION_DAYS; 0 = keep): a job's original goes this many
  days after its upload, or after its calendar date if that is later. Never while the job is
  still processing, still on the calendar ahead, or still being published.
* `after_publish`: the original goes as soon as every publication of its post succeeded.
* `rejected_days` (default: keep): rejected jobs are deleted entirely, drafts included, this
  many days after the decision.

Every deletion is in the audit trail with the actor `system:retention`. `dry_run=True` counts
what a run would do without doing it (the policy preview).

Race safety: an upload locks the asset row to bump `last_used_at`, and an object is never
collected within an hour of its last upload, which covers the gap between storing the bytes
and inserting the job row that uses them.
"""

from __future__ import annotations

import logging
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, delete, exists, select
from sqlalchemy.orm import Session

from app.api import audit
from app.api.database import (
    ACTIVE_STATUSES,
    ApiJob,
    DraftComment,
    DraftVersion,
    MediaAsset,
    Publication,
    ScheduledPost,
    Workspace,
)
from app.obs.metrics import MEDIA_DELETIONS, RETENTION_PURGED
from app.storage import LocalStorage, MediaStorage

log = logging.getLogger("retention")
STAGED_MAX_AGE_S = 24 * 3600
FRESH_UPLOAD_GRACE = timedelta(hours=1)
SYSTEM = "system:retention"
IN_FLIGHT = ("queued", "publishing", "processing", "retrying")


@dataclass(frozen=True)
class Policy:
    originals_days: int  # 0 = keep forever
    after_publish: bool
    rejected_days: int | None  # None = keep rejected drafts


def effective_policy(ws: Workspace, *, default_days: int, max_days: int = 0) -> Policy:
    days = ws.retention_originals_days
    if days is None:
        days = default_days
    if max_days and (days == 0 or days > max_days):
        days = max_days  # the operator's ceiling always wins
    return Policy(days, bool(ws.retention_after_publish), ws.retention_rejected_days)


def _publishing(job_id_col):
    """The job's post still has a publication on its way."""
    return exists().where(
        ScheduledPost.job_id == job_id_col,
        Publication.scheduled_post_id == ScheduledPost.id,
        Publication.status.in_(IN_FLIGHT),
    )


def _live(ws_id: str):
    return (ApiJob.workspace_id == ws_id, ApiJob.media_deleted_at.is_(None),
            ApiJob.media_sha256.is_not(None), ApiJob.status.not_in(ACTIVE_STATUSES))


def mark_deleted(db: Session, job: ApiJob, reason: str, *, actor=None, now: datetime,
                 detail: dict | None = None) -> None:
    """Remove one job's original (caller commits). The bytes go later, if nobody else has them."""
    job.media_deleted_at, job.media_deleted_reason = now, reason
    audit.record(
        db, workspace_id=job.workspace_id, actor=actor, system_actor=SYSTEM,
        action="media.deleted", target_type="job", target_id=job.id, target_label=job.filename,
        after={"reason": reason, **(detail or {})},
    )
    MEDIA_DELETIONS.labels(reason).inc()


def apply_retention(
    session_factory: Callable[[], Session],
    storage: MediaStorage,
    *,
    default_days: int,
    max_days: int = 0,
    now: datetime | None = None,
    dry_run: bool = False,
    workspace_id: str | None = None,
    override: Policy | None = None,
    batch: int = 500,
) -> dict[str, int]:
    now = now or datetime.now(timezone.utc)
    counts: Counter[str] = Counter()
    if not dry_run and workspace_id is None:
        counts["staged_removed"] = _clear_staged(storage)
    with session_factory() as db:
        query = select(Workspace)
        if workspace_id:
            query = query.where(Workspace.id == workspace_id)
        workspaces = db.scalars(query).all()
    for ws in workspaces:
        policy = override or effective_policy(ws, default_days=default_days, max_days=max_days)
        # A dry run only counts: a plain session, nothing written.
        with (session_factory() if dry_run else session_factory.begin()) as db:
            counts["originals_expired"] += _expire(db, ws.id, policy, now, dry_run, batch)
            counts["originals_published"] += _published(db, ws.id, policy, now, dry_run, batch)
            counts["rejected_deleted"] += _rejected(db, ws.id, policy, now, dry_run, batch)
    if not dry_run:
        counts["objects_purged"] = collect(session_factory, storage, now=now, batch=batch)
    out = {k: v for k, v in counts.items()}
    if any(out.get(k) for k in ("originals_expired", "originals_published", "rejected_deleted",
                                "objects_purged")) and not dry_run:
        log.info("retention applied", extra={"retention": out})
    return out


def _expire(db, ws_id, policy, now, dry_run, batch) -> int:
    if policy.originals_days <= 0:
        return 0
    cutoff = now - timedelta(days=policy.originals_days)
    on_calendar = exists().where(
        ScheduledPost.job_id == ApiJob.id, ScheduledPost.status == "scheduled",
        ScheduledPost.scheduled_at >= cutoff,  # ahead, or within the window after its date
    )
    jobs = db.scalars(
        select(ApiJob).where(*_live(ws_id), ApiJob.created_at < cutoff, ~on_calendar,
                             ~_publishing(ApiJob.id))
        .order_by(ApiJob.created_at).limit(batch).with_for_update(skip_locked=True)
    ).all()
    if not dry_run:
        for job in jobs:
            mark_deleted(db, job, "policy", now=now, detail={"days": policy.originals_days})
    return len(jobs)


def _published(db, ws_id, policy, now, dry_run, batch) -> int:
    if not policy.after_publish:
        return 0
    of_job = (ScheduledPost.job_id == ApiJob.id, Publication.scheduled_post_id == ScheduledPost.id)
    done = exists().where(*of_job, Publication.status == "published")
    unfinished = exists().where(*of_job, Publication.status.not_in(("published", "cancelled")))
    jobs = db.scalars(
        select(ApiJob).where(*_live(ws_id), done, ~unfinished)
        .limit(batch).with_for_update(skip_locked=True)
    ).all()
    if not dry_run:
        for job in jobs:
            mark_deleted(db, job, "published", now=now)
    return len(jobs)


def _rejected(db, ws_id, policy, now, dry_run, batch) -> int:
    if policy.rejected_days is None:
        return 0
    cutoff = now - timedelta(days=policy.rejected_days)
    jobs = db.scalars(
        select(ApiJob).where(
            ApiJob.workspace_id == ws_id, ApiJob.approval_status == "rejected",
            ApiJob.reviewed_at.is_not(None), ApiJob.reviewed_at < cutoff,
            ApiJob.status.not_in(ACTIVE_STATUSES),
        ).limit(batch).with_for_update(skip_locked=True)
    ).all()
    if dry_run:
        return len(jobs)
    for job in jobs:
        audit.record(
            db, workspace_id=ws_id, actor=None, system_actor=SYSTEM, action="job.purged",
            target_type="job", target_id=job.id, target_label=job.filename,
            before={"approval_status": "rejected",
                    "reviewed_at": _utc(job.reviewed_at).isoformat()},
            after={"days": policy.rejected_days},
        )
        posts = select(ScheduledPost.id).where(ScheduledPost.job_id == job.id)
        db.execute(delete(Publication).where(Publication.scheduled_post_id.in_(posts)))
        for model in (ScheduledPost, DraftComment, DraftVersion):
            db.execute(delete(model).where(model.job_id == job.id))
        db.delete(job)
        MEDIA_DELETIONS.labels("rejected_job").inc()
    return len(jobs)


def collect(session_factory, storage: MediaStorage, *, now: datetime | None = None,
            sha256: str | None = None, grace: timedelta = FRESH_UPLOAD_GRACE,
            batch: int = 500) -> int:
    """Delete stored objects no live job uses any more. `sha256` limits it to one object
    (right after a manual deletion, with a shorter `grace`)."""
    now = now or datetime.now(timezone.utc)
    unused = (
        MediaAsset.purged_at.is_(None),
        MediaAsset.last_used_at < now - grace,
        ~exists().where(and_(ApiJob.media_sha256 == MediaAsset.sha256,
                             ApiJob.media_deleted_at.is_(None))),
    )
    with session_factory() as db:
        query = select(MediaAsset.sha256).where(*unused)
        if sha256:
            query = query.where(MediaAsset.sha256 == sha256)
        candidates = db.scalars(query.limit(batch)).all()
    purged = 0
    for digest in candidates:
        with session_factory.begin() as db:
            asset = db.scalar(select(MediaAsset).where(MediaAsset.sha256 == digest, *unused)
                              .with_for_update(skip_locked=True))
            if asset is None:
                continue
            storage.delete(asset.stored_filename)
            asset.purged_at = now
            purged += 1
            RETENTION_PURGED.inc()
    return purged


def _clear_staged(storage: MediaStorage) -> int:
    removed = 0
    if isinstance(storage, LocalStorage) and storage.directory.is_dir():
        oldest = time.time() - STAGED_MAX_AGE_S
        for leftover in storage.directory.glob(".upload-*"):
            try:
                if leftover.stat().st_mtime < oldest:
                    leftover.unlink()
                    removed += 1
            except FileNotFoundError:
                pass
    return removed


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def prune_media(session_factory, storage, *, retention_days: int, max_days: int = 0,
                now: datetime | None = None, **kw) -> dict[str, int]:
    """Back-compatible entry point (worker, CLI): MEDIA_RETENTION_DAYS is the default policy."""
    return apply_retention(session_factory, storage, default_days=retention_days,
                           max_days=max_days, now=now, **kw)
