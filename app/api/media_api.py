"""Media access and deletion: expiring links, deleting an original, workspace policies.

* `POST /jobs/{id}/media-link`: an expiring URL for one job's original (app/api/media_links.py).
  Same rule as viewing the media: the uploader, or a reviewer.
* `DELETE /jobs/{id}/media`: delete a job's original now (uploader or admin). Drafts stay.
* `GET/PUT /workspaces/{id}/retention`: the workspace's deletion policy (admins of it), with a
  dry-run preview of what the next run would delete.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, Request, Response, status
from sqlalchemy import exists, select

from app.api import audit
from app.api.database import ACTIVE_STATUSES, Publication, ScheduledPost, Workspace
from app.api.dependencies import CurrentMember, CurrentUser, DbSession, effective_role
from app.api.media_links import deleted_detail, issue
from app.api.media_storage import locate_job_media
from app.api.retention import IN_FLIGHT, apply_retention, collect, effective_policy, mark_deleted
from app.api.roles import is_admin, is_reviewer
from app.api.routes import _workspace_job
from app.api.schemas import MediaLink, RetentionPolicy, RetentionPreview, RetentionView
from app.api.workspaces import _own_membership

router = APIRouter(prefix="/api/v1")
MANUAL_GRACE = timedelta(seconds=60)


@router.post("/jobs/{job_id}/media-link", response_model=MediaLink)
def media_link(job_id: str, request: Request, actor: CurrentMember, db: DbSession):
    job = _workspace_job(db, actor, job_id, any_owner=is_reviewer(actor))
    where = locate_job_media(db, job, request.app.state.storage)
    if where.purged:
        raise HTTPException(status.HTTP_410_GONE, deleted_detail(job))
    if where.key is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Uploaded media not found")
    url, expires_at, kind = issue(request, job, where.key)
    return MediaLink(url=url, expires_at=expires_at, kind=kind)


@router.delete("/jobs/{job_id}/media", status_code=status.HTTP_204_NO_CONTENT)
def delete_original(job_id: str, request: Request, actor: CurrentMember, db: DbSession):
    """Delete this job's original now. Its drafts, approval and calendar entry are kept; the
    stored bytes go too unless another job still has the same file."""
    job = _workspace_job(db, actor, job_id, any_owner=is_admin(actor))
    if job.media_deleted_at is not None:
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    if job.status in ACTIVE_STATUSES:
        raise HTTPException(status.HTTP_409_CONFLICT,
                            "The job is still being analyzed; cancel it or wait")
    if db.scalar(select(exists().where(
        ScheduledPost.job_id == job.id, Publication.scheduled_post_id == ScheduledPost.id,
        Publication.status.in_(IN_FLIGHT),
    ))):
        raise HTTPException(status.HTTP_409_CONFLICT,
                            "This post is still being published; cancel that first")
    sha256 = job.media_sha256
    mark_deleted(db, job, "manual", actor=actor, now=datetime.now(timezone.utc))
    db.commit()
    if sha256:
        # Right away, unless these bytes were uploaded in the last minute (an upload may be
        # about to use them); then the next sweep removes them.
        collect(request.app.state.session_factory, request.app.state.storage, sha256=sha256,
                grace=MANUAL_GRACE)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _view(request: Request, ws: Workspace) -> RetentionView:
    cfg = request.app.state.api_settings
    policy = effective_policy(ws, default_days=cfg.media_retention_days,
                              max_days=cfg.media_retention_max_days)
    preview = apply_retention(request.app.state.session_factory, request.app.state.storage,
                              default_days=cfg.media_retention_days,
                              max_days=cfg.media_retention_max_days,
                              dry_run=True, workspace_id=ws.id)
    return RetentionView(
        originals_days=ws.retention_originals_days,
        delete_after_publish=ws.retention_after_publish,
        rejected_days=ws.retention_rejected_days,
        effective_originals_days=policy.originals_days,
        default_days=cfg.media_retention_days, max_days=cfg.media_retention_max_days,
        preview=RetentionPreview(**{k: preview.get(k, 0) for k in RetentionPreview.model_fields}),
    )


def _validated(request: Request, payload: RetentionPolicy) -> None:
    ceiling = request.app.state.api_settings.media_retention_max_days
    if ceiling and payload.originals_days is not None and not (
            1 <= payload.originals_days <= ceiling):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"This installation keeps originals at most {ceiling} days; choose 1 to {ceiling}",
        )


def _admin_workspace(db, user, workspace_id: str) -> Workspace:
    membership = _own_membership(db, user, workspace_id)
    if effective_role(user, membership.role) not in {"admin", "super_admin"}:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Admin role required")
    return db.get(Workspace, workspace_id)


@router.get("/workspaces/{workspace_id}/retention", response_model=RetentionView)
def get_retention(workspace_id: str, request: Request, user: CurrentUser, db: DbSession):
    return _view(request, _admin_workspace(db, user, workspace_id))


@router.post("/workspaces/{workspace_id}/retention/preview", response_model=RetentionPreview)
def preview_retention(workspace_id: str, payload: RetentionPolicy, request: Request,
                      user: CurrentUser, db: DbSession):
    """What the next run would delete *if* this policy were saved. Changes nothing."""
    ws = _admin_workspace(db, user, workspace_id)
    _validated(request, payload)
    cfg = request.app.state.api_settings
    candidate = Workspace(retention_originals_days=payload.originals_days,
                          retention_after_publish=payload.delete_after_publish,
                          retention_rejected_days=payload.rejected_days)
    policy = effective_policy(candidate, default_days=cfg.media_retention_days,
                              max_days=cfg.media_retention_max_days)
    out = apply_retention(request.app.state.session_factory, request.app.state.storage,
                          default_days=cfg.media_retention_days, dry_run=True,
                          workspace_id=ws.id, override=policy)
    return RetentionPreview(**{k: out.get(k, 0) for k in RetentionPreview.model_fields})


@router.put("/workspaces/{workspace_id}/retention", response_model=RetentionView)
def set_retention(workspace_id: str, payload: RetentionPolicy, request: Request,
                  user: CurrentUser, db: DbSession):
    ws = _admin_workspace(db, user, workspace_id)
    _validated(request, payload)
    before = {"originals_days": ws.retention_originals_days,
              "delete_after_publish": ws.retention_after_publish,
              "rejected_days": ws.retention_rejected_days}
    after = payload.model_dump()
    if before != after:
        audit.record(db, workspace_id=ws.id, actor=user, action="workspace.retention_changed",
                     target_type="workspace", target_id=ws.id, target_label=ws.name,
                     before=before, after=after)
        ws.retention_originals_days = payload.originals_days
        ws.retention_after_publish = payload.delete_after_publish
        ws.retention_rejected_days = payload.rejected_days
        db.commit()
    return _view(request, ws)
