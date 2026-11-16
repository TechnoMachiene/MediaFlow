from __future__ import annotations

import hashlib
import logging
import os
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from datetime import time as datetime_time
from pathlib import Path
from typing import Annotated, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import (
    APIRouter,
    File,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
    status,
)
from fastapi.responses import FileResponse, StreamingResponse
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api import audit
from app.api.database import (
    ACTIVE_STATUSES,
    ApiJob,
    DraftComment,
    DraftVersion,
    ScheduledPost,
    User,
    Workspace,
    WorkspaceMember,
)
from app.api.dependencies import (
    CurrentMember,
    CurrentUser,
    DbSession,
    Member,
    QueueRuntime,
    Storage,
    effective_role,
)
from app.api.drafts import add_comment
from app.api.lifecycle import set_status
from app.api.media_storage import locate_job_media, store_media_asset
from app.api.reconcile import dispatch_job
from app.api.roles import (
    SUPER_ADMIN_EMAIL,
    is_admin,
    is_platform_admin,
    is_reviewer,
    is_super_admin,
)
from app.api.schemas import (
    ApprovalDecision,
    CalendarDay,
    CalendarResponse,
    Credentials,
    DemoAccount,
    FleetStats,
    JobPage,
    JobResponse,
    JobView,
    MetaResponse,
    MeView,
    RoleUpdate,
    ScheduledPostPage,
    ScheduledPostView,
    SchedulePostRequest,
    SignupRequest,
    StatsResponse,
    TokenResponse,
    UserCreate,
    UserPage,
    UserUpdate,
    UserView,
    WorkspaceCreate,
    WorkspaceView,
)
from app.api.security import create_versioned_access_token, hash_password, verify_password
from app.errors import MediaError
from app.media import ALLOWED, inspect_media
from app.obs.metrics import JOBS_ACCEPTED

log = logging.getLogger("api.jobs")
router = APIRouter(prefix="/api/v1")
JOB_RESULT_WAIT_S = 5 * 60
JOB_RESULT_POLL_S = 0.25
WORKER_GRACE_S = 1.0     # a just-started worker gets this long to send its first heartbeat
WORKER_RECHECK_S = 2.0   # then liveness is re-checked, so a worker dying mid-wait is noticed
# Visible ASCII, as in the IETF Idempotency-Key draft's examples; a UUID v4 is the usual choice.
IDEMPOTENCY_KEY_PATTERN = r"^[\x21-\x7e]{1,255}$"


def _token(request: Request, user: User) -> TokenResponse:
    cfg = request.app.state.api_settings
    return TokenResponse(
        access_token=create_versioned_access_token(
            user.id,
            cfg.jwt_secret,
            cfg.token_ttl_minutes,
            token_version=user.token_version,
        ),
        expires_in=cfg.token_ttl_minutes * 60,
    )




# ── Accounts and workspace membership ──────────────────────────────────────────────────────
# A workspace admin manages *memberships* of their workspace. Account credentials (email,
# password) can only be changed by a workspace admin when the account belongs to no other
# workspace, so one tenant can never take over a person who also works in another.


def _user_view(user: User, membership_role: str) -> UserView:
    return UserView(
        id=user.id,
        email=user.email,
        role=effective_role(user, membership_role),
        created_at=user.created_at,
    )


def _membership(db: Session, workspace_id: str, user_id: str) -> tuple[User, WorkspaceMember]:
    row = db.execute(
        select(User, WorkspaceMember)
        .join(WorkspaceMember, WorkspaceMember.user_id == User.id)
        .where(WorkspaceMember.workspace_id == workspace_id, User.id == user_id)
    ).first()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    return row[0], row[1]


def _require_admin(actor: Member) -> None:
    if not is_admin(actor):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Admin role required")


def _protect_super_admin(target: User) -> None:
    if is_super_admin(target):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "The super-admin account cannot be modified or deleted through the API",
        )


def _ensure_another_admin(db: Session, workspace_id: str) -> None:
    admins = db.scalar(
        select(func.count())
        .select_from(WorkspaceMember)
        .where(WorkspaceMember.workspace_id == workspace_id, WorkspaceMember.role == "admin")
    )
    if admins <= 1:
        raise HTTPException(status.HTTP_409_CONFLICT, "Cannot demote the last admin")


def _other_workspaces(db: Session, user_id: str, workspace_id: str) -> int:
    return db.scalar(
        select(func.count())
        .select_from(WorkspaceMember)
        .where(WorkspaceMember.user_id == user_id, WorkspaceMember.workspace_id != workspace_id)
    )


@router.post("/users", response_model=UserView, status_code=status.HTTP_201_CREATED)
def create_user(payload: UserCreate, actor: CurrentMember, db: DbSession):
    if not is_reviewer(actor):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only managers and admins can create users")
    if actor.role == "manager" and payload.role not in {"user", "manager"}:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Managers can only create user and manager accounts",
        )
    if str(payload.email).lower() == SUPER_ADMIN_EMAIL:
        raise HTTPException(status.HTTP_409_CONFLICT, "The super-admin account is reserved")
    user = User(email=str(payload.email), password_hash=hash_password(payload.password))
    db.add(user)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "An account with that email already exists. Invite them instead; they accept "
            "by signing in.",
        ) from None
    db.add(WorkspaceMember(workspace_id=actor.workspace_id, user_id=user.id, role=payload.role))
    audit.record(
        db, workspace_id=actor.workspace_id, actor=actor, action="member.created",
        target_type="user", target_id=user.id, target_label=user.email,
        after={"role": payload.role},
    )
    db.commit()
    db.refresh(user)
    return _user_view(user, payload.role)


@router.get("/users", response_model=UserPage)
def list_users(
    actor: CurrentMember,
    db: DbSession,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
):
    _require_admin(actor)
    rows = db.execute(
        select(User, WorkspaceMember.role)
        .join(WorkspaceMember, WorkspaceMember.user_id == User.id)
        .where(WorkspaceMember.workspace_id == actor.workspace_id)
        .order_by(User.created_at.desc(), User.id)
        .limit(limit)
        .offset(offset)
    ).all()
    return UserPage(
        items=[_user_view(user, role) for user, role in rows], limit=limit, offset=offset
    )


@router.get("/users/{user_id}", response_model=UserView)
def get_user(user_id: str, actor: CurrentMember, db: DbSession):
    _require_admin(actor)
    user, membership = _membership(db, actor.workspace_id, user_id)
    return _user_view(user, membership.role)


@router.patch("/users/{user_id}/role", response_model=UserView)
def update_user_role(user_id: str, payload: RoleUpdate, actor: CurrentMember, db: DbSession):
    _require_admin(actor)
    user, membership = _membership(db, actor.workspace_id, user_id)
    _protect_super_admin(user)
    if membership.role == "admin" and payload.role != "admin":
        _ensure_another_admin(db, actor.workspace_id)
    _change_role(db, actor, user, membership, payload.role)
    db.commit()
    return _user_view(user, membership.role)


def _change_role(db: Session, actor: Member, user: User, membership: WorkspaceMember,
                 role: str) -> None:
    if membership.role == role:
        return
    audit.record(
        db, workspace_id=actor.workspace_id, actor=actor, action="member.role_changed",
        target_type="user", target_id=user.id, target_label=user.email,
        before={"role": membership.role}, after={"role": role},
    )
    membership.role = role


@router.patch("/users/{user_id}", response_model=UserView)
def update_user(user_id: str, payload: UserUpdate, actor: CurrentMember, db: DbSession):
    _require_admin(actor)
    user, membership = _membership(db, actor.workspace_id, user_id)
    _protect_super_admin(user)

    if (payload.email is not None or payload.password is not None) and _other_workspaces(
        db, user.id, actor.workspace_id
    ):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "This person also belongs to another workspace, so only they can change their "
            "email or password",
        )
    if payload.email is not None:
        if str(payload.email).lower() == SUPER_ADMIN_EMAIL:
            raise HTTPException(status.HTTP_409_CONFLICT, "The super-admin email is reserved")
        new_email = str(payload.email).lower()
        if new_email != user.email:
            audit.record(
                db, workspace_id=actor.workspace_id, actor=actor, action="member.email_changed",
                target_type="user", target_id=user.id, target_label=new_email,
                before={"email": user.email}, after={"email": new_email},
            )
            user.email = new_email
    if payload.password is not None:
        user.password_hash = hash_password(payload.password)
        user.token_version += 1
        audit.record(
            db, workspace_id=actor.workspace_id, actor=actor, action="member.password_reset",
            target_type="user", target_id=user.id, target_label=user.email,
        )
    if payload.role is not None:
        if membership.role == "admin" and payload.role != "admin":
            _ensure_another_admin(db, actor.workspace_id)
        _change_role(db, actor, user, membership, payload.role)

    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "An account with that email already exists",
        ) from None
    db.refresh(user)
    return _user_view(user, membership.role)


@router.delete("/users/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_user(user_id: str, actor: CurrentMember, db: DbSession, runtime: QueueRuntime):
    """Remove someone from this workspace together with their jobs here. The account itself is
    deleted only when it belongs to no other workspace."""
    _require_admin(actor)
    user, membership = _membership(db, actor.workspace_id, user_id)
    _protect_super_admin(user)
    if membership.role == "admin":
        _ensure_another_admin(db, actor.workspace_id)

    in_workspace = (ApiJob.owner_id == user_id, ApiJob.workspace_id == actor.workspace_id)
    jobs = db.scalars(select(ApiJob).where(*in_workspace)).all()
    for job in jobs:
        job_status = runtime.store.status(job.id)["status"]
        if job_status in {"queued", "waiting", "retrying"}:
            if not runtime.store.cancel(job.id):
                raise HTTPException(
                    status.HTTP_409_CONFLICT,
                    "Cannot delete account while one of its jobs is being processed",
                )
        elif job_status == "processing":
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "Cannot delete account while one of its jobs is being processed",
            )

    job_ids = [job.id for job in jobs]
    if job_ids:
        for model in (ScheduledPost, DraftComment, DraftVersion):
            db.execute(delete(model).where(model.job_id.in_(job_ids)))
        db.execute(delete(ApiJob).where(ApiJob.id.in_(job_ids)))
    audit.record(
        db, workspace_id=actor.workspace_id, actor=actor, action="member.removed",
        target_type="user", target_id=user.id, target_label=user.email,
        before={"role": membership.role}, after={"jobs_deleted": len(job_ids)},
    )
    db.delete(membership)
    if not _other_workspaces(db, user.id, actor.workspace_id):
        db.delete(user)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ── Auth, sign-up, workspaces ──────────────────────────────────────────────────────────────


@router.get("/meta", response_model=MetaResponse)
def read_meta(request: Request):
    """Public facts the sign-in screen needs: demo mode and whether sign-up is open."""
    cfg = request.app.state.api_settings
    if not cfg.demo_mode:
        return MetaResponse(demo_mode=False, allow_signup=cfg.allow_signup)
    from app.demo import DEMO_ACCOUNTS, DEMO_PASSWORD

    return MetaResponse(
        demo_mode=True,
        allow_signup=cfg.allow_signup,
        demo_password=DEMO_PASSWORD,
        demo_accounts=[DemoAccount(email=e, role=r) for e, r in DEMO_ACCOUNTS],
    )


@router.post("/auth/login", response_model=TokenResponse)
def login(payload: Credentials, request: Request, db: DbSession):
    user = db.scalar(select(User).where(User.email == str(payload.email)))
    if user is None or not verify_password(payload.password, user.password_hash):
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "Incorrect email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return _token(request, user)


@router.post("/auth/signup", response_model=TokenResponse, status_code=status.HTTP_201_CREATED)
def signup(payload: SignupRequest, request: Request, db: DbSession):
    """Self-serve: a new account plus a new workspace that it administers."""
    if not request.app.state.api_settings.allow_signup:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Sign-up is not enabled")
    if str(payload.email).lower() == SUPER_ADMIN_EMAIL:
        raise HTTPException(status.HTTP_409_CONFLICT, "An account with that email already exists")
    user = User(email=str(payload.email), password_hash=hash_password(payload.password))
    workspace = Workspace(name=payload.workspace_name)
    db.add_all([user, workspace])
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT, "An account with that email already exists"
        ) from None
    db.add(WorkspaceMember(workspace_id=workspace.id, user_id=user.id, role="admin"))
    _workspace_created(db, workspace, user)
    db.commit()
    return _token(request, user)


def _workspace_created(db: Session, workspace: Workspace, user: User) -> None:
    audit.record(
        db, workspace_id=workspace.id, actor=user, action="workspace.created",
        target_type="workspace", target_id=workspace.id, target_label=workspace.name,
        after={"name": workspace.name, "admin": user.email},
    )


def _my_workspaces(db: Session, user: User) -> list[WorkspaceView]:
    rows = db.execute(
        select(Workspace, WorkspaceMember.role)
        .join(WorkspaceMember, WorkspaceMember.workspace_id == Workspace.id)
        .where(WorkspaceMember.user_id == user.id)
        .order_by(WorkspaceMember.created_at, Workspace.id)
    ).all()
    return [
        WorkspaceView(id=ws.id, name=ws.name, role=effective_role(user, role)) for ws, role in rows
    ]


@router.get("/auth/me", response_model=MeView)
def read_current_user(member: CurrentMember, db: DbSession):
    return MeView(
        id=member.id,
        email=member.email,
        role=member.role,
        created_at=member.created_at,
        workspace=WorkspaceView(
            id=member.workspace_id, name=member.workspace_name, role=member.role
        ),
        workspaces=_my_workspaces(db, member.user),
        platform_admin=is_platform_admin(member.user),
    )


@router.get("/workspaces", response_model=list[WorkspaceView])
def list_workspaces(user: CurrentUser, db: DbSession):
    return _my_workspaces(db, user)


@router.post("/workspaces", response_model=WorkspaceView, status_code=status.HTTP_201_CREATED)
def create_workspace(payload: WorkspaceCreate, request: Request, user: CurrentUser, db: DbSession):
    if not (request.app.state.api_settings.allow_signup or is_platform_admin(user)):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "Only platform admins can create workspaces here"
        )
    workspace = Workspace(name=payload.name)
    db.add(workspace)
    db.flush()
    db.add(WorkspaceMember(workspace_id=workspace.id, user_id=user.id, role="admin"))
    _workspace_created(db, workspace, user)
    db.commit()
    return WorkspaceView(id=workspace.id, name=workspace.name, role=effective_role(user, "admin"))


# ── Jobs ───────────────────────────────────────────────────────────────────────────────────


def _job_view(row: ApiJob, data: dict, *, submitter_email: str | None = None) -> JobView:
    live_status = data.get("status", "unknown")
    if live_status == "unknown":
        # Redis forgot the job (TTL or data loss). The row is authoritative now, and an active
        # row is picked up again by the reconciler.
        live_status = "done" if row.result is not None else row.status
    # The row's copy wins: it is durable, and once someone edits the draft it differs from the
    # Gemini result still cached in Redis, which must never overwrite the edit.
    result = row.result if row.result is not None else data.get("result")
    if result is not None:
        live_status = "done"
        row.result = result
    # Guarded: Redis can't move a row out of a final state (see app/api/lifecycle.py).
    set_status(row, live_status)
    live_status = row.status
    return JobView(
        job_id=row.id,
        status=live_status,
        approval_status=row.approval_status,
        draft_version=row.draft_version,
        submitter_email=submitter_email,
        original_filename=row.filename,
        attempts=data.get("attempts", 0),
        cached=data.get("cached", False),
        error=data.get("error") or (row.error if live_status != "done" else None),
        result=result,
        filename=row.filename,
        media_kind=row.media_kind,
        media_url=f"/api/v1/jobs/{row.id}/media",
        created_at=row.created_at,
    )


def _workspace_job(db: Session, actor: Member, job_id: str, *, any_owner: bool) -> ApiJob:
    query = select(ApiJob).where(ApiJob.id == job_id, ApiJob.workspace_id == actor.workspace_id)
    if not any_owner:
        query = query.where(ApiJob.owner_id == actor.id)
    row = db.scalar(query)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Job not found")
    return row


@router.post(
    "/jobs",
    response_model=JobResponse,
    status_code=status.HTTP_202_ACCEPTED,
    responses={
        status.HTTP_200_OK: {
            "model": JobResponse,
            "description": "Job completed during the response wait",
        },
        status.HTTP_502_BAD_GATEWAY: {
            "model": JobResponse,
            "description": "The worker or Gemini could not process the media",
        },
    },
)
def upload_job(
    request: Request,
    response: Response,
    file: Annotated[UploadFile, File()],
    user: CurrentMember,
    db: DbSession,
    runtime: QueueRuntime,
    storage: Storage,
    idempotency_key: Annotated[
        str | None,
        Header(
            alias="Idempotency-Key",
            pattern=IDEMPOTENCY_KEY_PATTERN,
            description=(
                "Optional client-chosen key (e.g. a UUID). Retrying with the same key and the "
                "same file returns the job the first request created instead of a new one; "
                "the response then carries `Idempotent-Replayed: true`. Reusing a key for a "
                "different file is rejected with 422."
            ),
        ),
    ] = None,
):
    original_name = (file.filename or "upload").replace("\\", "/").rsplit("/", 1)[-1]
    suffix = Path(original_name).suffix.lower()
    media = ALLOWED.get(suffix)
    if media is None:
        raise HTTPException(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            "Unsupported image/video file type",
        )
    kind, _ = media
    settings = runtime.settings
    max_bytes = (
        settings.max_image_mb if kind == "image" else settings.max_video_upload_mb
    ) * 1024 * 1024
    job_id = uuid.uuid4().hex
    filename = original_name[:255]
    staging_dir: Path = request.app.state.api_settings.media_storage_dir
    staging_dir.mkdir(mode=0o750, parents=True, exist_ok=True)
    staged_path = staging_dir / f".upload-{uuid.uuid4().hex}{suffix}"

    size = 0
    digest = hashlib.sha256()
    try:
        with staged_path.open("xb") as target:
            while chunk := file.file.read(1024 * 1024):
                size += len(chunk)
                if size > max_bytes:
                    raise HTTPException(
                        status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                        "Upload exceeds the configured size limit",
                    )
                digest.update(chunk)
                target.write(chunk)
        os.chmod(staged_path, 0o600)
        if size == 0:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Uploaded file is empty")
        sha256 = digest.hexdigest()

        # A retry of a request we already accepted: answer with that job, never a second one.
        if idempotency_key is not None:
            existing = _keyed_job(db, user, idempotency_key)
            if existing is not None:
                return _replay(existing, sha256, filename, response, db, runtime, storage)

        inspect_media(
            str(staged_path),
            max_image_mb=settings.max_image_mb,
            max_video_mb=settings.max_video_upload_mb,
        )

        asset = store_media_asset(db, staged_path, original_name, storage, sha256=sha256)

        # Step 1 (durable intent): the row is committed before Redis hears of the job, with
        # enqueued_at NULL. From this commit on the job is accepted: if anything below fails
        # or this process dies, the reconciler dispatches it (api_jobs is the outbox).
        row = ApiJob(
            id=job_id,
            workspace_id=user.workspace_id,
            owner_id=user.id,
            media_sha256=asset.sha256,
            filename=filename,
            media_kind=kind,
            status="queued",
            idempotency_key=idempotency_key,
        )
        db.add(row)
        try:
            db.commit()
        except IntegrityError:
            # Only the idempotency index can collide (ids are random): a concurrent request
            # with the same key committed first. Theirs is the job; answer with it.
            db.rollback()
            existing = _keyed_job(db, user, idempotency_key) if idempotency_key else None
            if existing is None:
                raise
            return _replay(existing, sha256, filename, response, db, runtime, storage)

        # Step 2 (dispatch): hand it to Redis and record enqueued_at. Atomic and idempotent
        # per job id, so a retry (ours, a replay's or the reconciler's) never double-enqueues.
        try:
            result = dispatch_job(db, row, runtime.store, storage)
            db.commit()
        except Exception:
            # Redis down, its reply lost, or the enqueued_at write failed. Either Redis has the
            # job or the row still says enqueued_at NULL; both converge. No compensating delete:
            # Redis may in fact have the job, and the client must not be told it was refused.
            db.rollback()
            JOBS_ACCEPTED.labels("dispatch_deferred").inc()
            log.warning(
                "could not hand job to the queue; the reconciler will dispatch it",
                exc_info=True, extra={"job_id": job_id},
            )
            return JobResponse(job_id=job_id, status="queued")
        JOBS_ACCEPTED.labels(result["status"]).inc()
        log.info(
            "job accepted",
            extra={"job_id": job_id, "dispatch": result["status"], "kind": kind, "bytes": size},
        )
        return _respond(row, _await_result(runtime, job_id), response, db,
                        cached=result["cached"])
    except MediaError as exc:
        db.rollback()
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from None
    except HTTPException:
        db.rollback()
        raise
    finally:
        file.file.close()
        if staged_path.exists():
            staged_path.unlink()


def _keyed_job(db: Session, user: Member, key: str) -> ApiJob | None:
    return db.scalar(
        select(ApiJob).where(
            ApiJob.workspace_id == user.workspace_id,
            ApiJob.owner_id == user.id,
            ApiJob.idempotency_key == key,
        )
    )


def _replay(
    row: ApiJob, sha256: str, filename: str, response: Response, db: Session, runtime, storage
) -> JobResponse:
    """Answer a retried upload with the job its first attempt created."""
    if (row.media_sha256, row.filename) != (sha256, filename):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "This Idempotency-Key was already used for a different upload",
        )
    response.headers["Idempotent-Replayed"] = "true"
    JOBS_ACCEPTED.labels("replayed").inc()
    log.info("idempotent replay", extra={"job_id": row.id})
    if row.enqueued_at is None and row.status in ACTIVE_STATUSES:
        # The first attempt never reached Redis (or never recorded that it did). The client is
        # here now, so dispatch instead of waiting for the reconciler. Safe to race it.
        try:
            dispatch_job(db, row, runtime.store, storage)
            db.commit()
        except Exception:
            db.rollback()
            log.warning("replay could not dispatch; the reconciler will",
                        exc_info=True, extra={"job_id": row.id})
            response.status_code = status.HTTP_202_ACCEPTED
            return JobResponse(job_id=row.id, status=row.status, error=row.error)
    return _respond(row, _await_result(runtime, row.id), response, db)


def _await_result(runtime, job_id: str) -> dict:
    """Wait briefly for the result, but only while some worker is consuming the queue: with
    none alive, holding the request open for minutes helps nobody. The job stays safely queued
    either way, and the client gets 202 to poll. A Redis error ends the wait early."""
    deadline = time.monotonic() + JOB_RESULT_WAIT_S
    next_liveness_check = time.monotonic() + WORKER_GRACE_S
    try:
        job_status = runtime.store.status(job_id)
        while job_status["status"] in ACTIVE_STATUSES and time.monotonic() < deadline:
            if time.monotonic() >= next_liveness_check:
                if runtime.queue.live_workers() == 0:
                    log.warning("no live worker; not waiting for the result",
                                extra={"job_id": job_id})
                    break
                next_liveness_check = time.monotonic() + WORKER_RECHECK_S
            time.sleep(min(JOB_RESULT_POLL_S, max(0, deadline - time.monotonic())))
            job_status = runtime.store.status(job_id)
    except Exception:
        log.warning("could not read job status; answering from the database",
                    exc_info=True, extra={"job_id": job_id})
        return {"status": "unknown"}
    return job_status


def _respond(row: ApiJob, job_status: dict, response: Response, db: Session, *,
             cached: bool = False) -> JobResponse:
    """Fold the live Redis state into the row (guarded) and pick the HTTP status from it."""
    db.refresh(row)
    if row.result is None and job_status.get("result") is not None:
        row.result = job_status["result"]
    live = "done" if row.result is not None else job_status["status"]
    if live != "unknown":
        set_status(row, live)
    db.commit()
    if row.status == "done":
        response.status_code = status.HTTP_200_OK
    elif row.status in {"failed", "dead"}:
        response.status_code = status.HTTP_502_BAD_GATEWAY
    else:
        response.status_code = status.HTTP_202_ACCEPTED
    return JobResponse(
        job_id=row.id,
        status=row.status,
        cached=job_status.get("cached", cached),
        result=row.result,
        error=None if row.status == "done" else (job_status.get("error") or row.error),
    )


def _require_reviewer(user: Member) -> None:
    if not is_reviewer(user):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Manager or admin role required")


def _jobs_page(query, *, db: Session, runtime, limit: int, offset: int) -> JobPage:
    rows = db.execute(
        query.join(User, User.id == ApiJob.owner_id)
        .add_columns(User.email)
        .order_by(ApiJob.created_at.desc(), ApiJob.id.desc())
        .limit(limit)
        .offset(offset)
    ).all()
    items = [
        _job_view(row, runtime.store.status(row.id), submitter_email=email)
        for row, email in rows
    ]
    db.commit()
    return JobPage(items=items, limit=limit, offset=offset)


def _approval_jobs(status_value: Literal["pending", "approved", "rejected"], *, actor: Member,
                   db, runtime, limit: int, offset: int) -> JobPage:
    _require_reviewer(actor)
    query = select(ApiJob).where(
        ApiJob.workspace_id == actor.workspace_id,
        ApiJob.status == "done",
        ApiJob.approval_status == status_value,
    )
    return _jobs_page(query, db=db, runtime=runtime, limit=limit, offset=offset)


@router.get("/jobs/review", response_model=JobPage)
def list_jobs_for_review(
    actor: CurrentMember,
    db: DbSession,
    runtime: QueueRuntime,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
):
    return _approval_jobs(
        "pending", actor=actor, db=db, runtime=runtime, limit=limit, offset=offset
    )


@router.get("/jobs/approved", response_model=JobPage)
def list_approved_jobs(
    actor: CurrentMember,
    db: DbSession,
    runtime: QueueRuntime,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
):
    return _approval_jobs(
        "approved", actor=actor, db=db, runtime=runtime, limit=limit, offset=offset
    )


@router.get("/jobs/rejected", response_model=JobPage)
def list_rejected_jobs(
    actor: CurrentMember,
    db: DbSession,
    runtime: QueueRuntime,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
):
    return _approval_jobs(
        "rejected", actor=actor, db=db, runtime=runtime, limit=limit, offset=offset
    )


@router.get("/jobs/mine", response_model=JobPage)
def list_my_jobs(
    actor: CurrentMember,
    db: DbSession,
    runtime: QueueRuntime,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
):
    rows = db.scalars(
        select(ApiJob)
        .where(ApiJob.owner_id == actor.id, ApiJob.workspace_id == actor.workspace_id)
        .order_by(ApiJob.created_at.desc(), ApiJob.id.desc())
        .limit(limit)
        .offset(offset)
    ).all()
    items = [_job_view(row, runtime.store.status(row.id)) for row in rows]
    db.commit()
    return JobPage(items=items, limit=limit, offset=offset)


@router.patch("/jobs/{job_id}/approval", response_model=JobView)
def review_job(
    job_id: str,
    decision: ApprovalDecision,
    actor: CurrentMember,
    db: DbSession,
    runtime: QueueRuntime,
):
    _require_reviewer(actor)
    row = _workspace_job(db, actor, job_id, any_owner=True)
    if row.status != "done":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "A job can only be approved or rejected after Gemini processing is complete",
        )
    if row.approval_status != "pending":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Job has already been {row.approval_status}",
        )
    row.approval_status = decision.status
    owner = db.get(User, row.owner_id)
    audit.record(
        db, workspace_id=actor.workspace_id, actor=actor, action=f"job.{decision.status}",
        target_type="job", target_id=row.id, target_label=row.filename,
        before={"approval_status": "pending"},
        after={"approval_status": decision.status, "owner": owner.email if owner else None},
    )
    if decision.comment and decision.comment.strip():
        # The reason lands in the draft's thread, where the uploader will look for it.
        add_comment(db, row, actor, decision.comment.strip(), kind=decision.status)
    view = _job_view(
        row,
        runtime.store.status(job_id),
        submitter_email=owner.email if owner else None,
    )
    db.commit()
    return view


@router.get("/jobs/{job_id}/media")
def get_job_media(job_id: str, user: CurrentMember, db: DbSession, storage: Storage):
    row = _workspace_job(db, user, job_id, any_owner=is_reviewer(user))
    media = ALLOWED.get(Path(row.filename).suffix.lower())
    if media is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Uploaded media not found")
    where = locate_job_media(db, row, storage)
    if where.purged:
        raise HTTPException(
            status.HTTP_410_GONE,
            "The original was deleted by the media retention policy; the drafts are kept",
        )
    if where.key is None or not storage.exists(where.key):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Uploaded media not found")
    local = storage.local_path(where.key)
    if local is not None:
        return FileResponse(local, media_type=media[1], filename=row.filename)
    return StreamingResponse(
        storage.read_chunks(where.key),
        media_type=media[1],
        headers={"Content-Disposition": f'attachment; filename="{row.filename}"'},
    )


@router.get("/jobs", response_model=JobPage)
def list_jobs(
    actor: CurrentMember,
    db: DbSession,
    runtime: QueueRuntime,
    kind: Literal["image", "video"] | None = None,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
):
    _require_admin(actor)
    query = select(ApiJob).where(ApiJob.workspace_id == actor.workspace_id)
    if kind is not None:
        query = query.where(ApiJob.media_kind == kind)
    return _jobs_page(query, db=db, runtime=runtime, limit=limit, offset=offset)


@router.get("/jobs/{job_id}", response_model=JobView)
def get_job(job_id: str, user: CurrentMember, db: DbSession, runtime: QueueRuntime):
    row = _workspace_job(db, user, job_id, any_owner=is_admin(user))
    owner = db.get(User, row.owner_id) if is_reviewer(user) else None
    view = _job_view(
        row,
        runtime.store.status(job_id),
        submitter_email=owner.email if owner else None,
    )
    db.commit()
    return view


@router.delete("/jobs/{job_id}", response_model=JobView)
def cancel_job(job_id: str, user: CurrentMember, db: DbSession, runtime: QueueRuntime):
    row = _workspace_job(db, user, job_id, any_owner=is_admin(user))
    if row.enqueued_at is None and row.status in ACTIVE_STATUSES:
        # Accepted but not yet handed to Redis: cancel it in the outbox. The row lock orders
        # this against a concurrent dispatch; whichever commits first wins.
        pending = db.scalar(
            select(ApiJob)
            .where(ApiJob.id == job_id, ApiJob.enqueued_at.is_(None))
            .with_for_update()
        )
        previous = pending.status if pending is not None else None
        if pending is not None and set_status(pending, "cancelled"):
            pending.updated_at = datetime.now(timezone.utc)
            _job_cancelled(db, user, pending, previous)
            db.commit()
            return _job_view(pending, {"status": "cancelled"})
        db.rollback()
    previous = row.status
    if not runtime.store.cancel(job_id):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Job is already running or cannot be cancelled",
        )
    view = _job_view(row, runtime.store.status(job_id))
    if row.status == "cancelled":
        _job_cancelled(db, user, row, previous)
    db.commit()
    return view


def _job_cancelled(db: Session, actor: Member, row: ApiJob, previous: str | None) -> None:
    audit.record(
        db, workspace_id=actor.workspace_id, actor=actor, action="job.cancelled",
        target_type="job", target_id=row.id, target_label=row.filename,
        before={"status": previous}, after={"status": "cancelled"},
    )


@router.get("/stats", response_model=StatsResponse)
def stats(user: CurrentMember, db: DbSession, runtime: QueueRuntime):
    """Job counts for this workspace. Redis queue depths are shared by every tenant, so only
    platform admins also get the installation-wide numbers."""
    _require_admin(user)
    counts = dict(
        db.execute(
            select(ApiJob.status, func.count())
            .where(ApiJob.workspace_id == user.workspace_id)
            .group_by(ApiJob.status)
        ).all()
    )
    out = StatsResponse(
        ready=counts.get("queued", 0) + counts.get("waiting", 0),
        processing=counts.get("processing", 0),
        deferred=counts.get("retrying", 0),
        dead=counts.get("failed", 0) + counts.get("dead", 0),
    )
    if is_platform_admin(user.user):
        out.fleet = FleetStats(**runtime.queue.depths())
        out.gemini_calls_today = runtime.limiter.used_today()
    return out


# ── Calendar ───────────────────────────────────────────────────────────────────────────────


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _scheduled_post_view(
    db: Session, post: ScheduledPost, timezone_name: str
) -> ScheduledPostView:
    job = db.get(ApiJob, post.job_id)
    if job is None or job.result is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "The scheduled job no longer has a completed Gemini result",
        )
    scheduled_at = _as_utc(post.scheduled_at)
    return ScheduledPostView(
        id=post.id,
        job_id=post.job_id,
        original_filename=job.filename,
        media_kind=job.media_kind,
        status=post.status,
        scheduled_at=scheduled_at,
        timezone=post.timezone,
        local_date=scheduled_at.astimezone(ZoneInfo(timezone_name)).date(),
        content=str(job.result["text"]),
        created_at=_as_utc(post.created_at),
    )


def _calendar_bounds(start: date, end: date, timezone_name: str) -> tuple[datetime, datetime]:
    if end < start:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "end must be on or after start")
    if (end - start).days > 92:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "Calendar range cannot exceed 93 days",
        )
    try:
        zone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "Invalid IANA timezone"
        ) from None
    beginning = datetime.combine(start, datetime_time.min, tzinfo=zone).astimezone(timezone.utc)
    after_end = datetime.combine(end + timedelta(days=1), datetime_time.min, tzinfo=zone)
    return beginning, after_end.astimezone(timezone.utc)


def _post_query(actor: Member):
    query = select(ScheduledPost).where(ScheduledPost.workspace_id == actor.workspace_id)
    if not is_admin(actor):
        query = query.where(ScheduledPost.owner_id == actor.id)
    return query


def _list_scheduled_posts(
    db: Session,
    actor: Member,
    start: date,
    end: date,
    timezone_name: str,
    *,
    limit: int,
    offset: int = 0,
) -> list[ScheduledPostView]:
    beginning, after_end = _calendar_bounds(start, end, timezone_name)
    query = _post_query(actor).where(
        ScheduledPost.scheduled_at >= beginning,
        ScheduledPost.scheduled_at < after_end,
    )
    posts = db.scalars(
        query.order_by(ScheduledPost.scheduled_at, ScheduledPost.id)
        .limit(limit)
        .offset(offset)
    ).all()
    return [_scheduled_post_view(db, post, timezone_name) for post in posts]


def _workspace_post(db: Session, actor: Member, post_id: str) -> ScheduledPost:
    post = db.scalar(_post_query(actor).where(ScheduledPost.id == post_id))
    if post is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Scheduled post not found")
    return post


@router.post(
    "/jobs/{job_id}/schedule",
    response_model=ScheduledPostView,
    status_code=status.HTTP_201_CREATED,
)
def schedule_job(
    job_id: str,
    payload: SchedulePostRequest,
    actor: CurrentMember,
    db: DbSession,
):
    job = _workspace_job(db, actor, job_id, any_owner=False)
    if job.status != "done" or job.result is None or job.approval_status != "approved":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Only completed, manager-approved jobs can be scheduled",
        )
    if payload.scheduled_at.astimezone(timezone.utc) <= datetime.now(timezone.utc):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "scheduled_at must be in the future"
        )
    if db.scalar(select(ScheduledPost.id).where(ScheduledPost.job_id == job.id)):
        raise HTTPException(status.HTTP_409_CONFLICT, "This job already has a scheduled post")

    post = ScheduledPost(
        job_id=job.id,
        workspace_id=job.workspace_id,
        owner_id=job.owner_id,
        scheduled_at=payload.scheduled_at.astimezone(timezone.utc),
        timezone=payload.timezone,
    )
    db.add(post)
    try:
        db.flush()
        audit.record(
            db, workspace_id=actor.workspace_id, actor=actor, action="post.scheduled",
            target_type="post", target_id=post.id, target_label=job.filename,
            after={**_when(post), "job_id": job.id},
        )
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT, "This job already has a scheduled post"
        ) from None
    return _scheduled_post_view(db, post, payload.timezone)


@router.get("/scheduled-posts", response_model=ScheduledPostPage)
def list_scheduled_posts(
    actor: CurrentMember,
    db: DbSession,
    start: date,
    end: date,
    timezone_name: str = Query(alias="timezone"),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
):
    return ScheduledPostPage(
        items=_list_scheduled_posts(
            db, actor, start, end, timezone_name, limit=limit, offset=offset
        ),
        limit=limit,
        offset=offset,
    )


@router.get("/calendar", response_model=CalendarResponse)
def get_calendar(
    actor: CurrentMember,
    db: DbSession,
    start: date,
    end: date,
    timezone_name: str = Query(alias="timezone"),
):
    posts = _list_scheduled_posts(
        db, actor, start, end, timezone_name, limit=1001
    )
    if len(posts) > 1000:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "Calendar range contains more than 1000 posts; use the paginated scheduled-posts API",
        )
    grouped = {
        start + timedelta(days=index): []
        for index in range((end - start).days + 1)
    }
    for post in posts:
        grouped[post.local_date].append(post)
    return CalendarResponse(
        timezone=timezone_name,
        days=[CalendarDay(date=day, posts=items) for day, items in grouped.items()],
    )


@router.get("/scheduled-posts/{post_id}", response_model=ScheduledPostView)
def get_scheduled_post(post_id: str, actor: CurrentMember, db: DbSession):
    post = _workspace_post(db, actor, post_id)
    return _scheduled_post_view(db, post, post.timezone)


@router.patch("/scheduled-posts/{post_id}", response_model=ScheduledPostView)
def reschedule_post(
    post_id: str,
    payload: SchedulePostRequest,
    actor: CurrentMember,
    db: DbSession,
):
    post = _workspace_post(db, actor, post_id)
    if payload.scheduled_at.astimezone(timezone.utc) <= datetime.now(timezone.utc):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "scheduled_at must be in the future"
        )
    before = _when(post)
    post.scheduled_at = payload.scheduled_at.astimezone(timezone.utc)
    post.timezone = payload.timezone
    post.status = "scheduled"
    if _when(post) != before:
        audit.record(
            db, workspace_id=actor.workspace_id, actor=actor, action="post.rescheduled",
            target_type="post", target_id=post.id, target_label=_post_label(db, post),
            before=before, after=_when(post),
        )
    db.commit()
    return _scheduled_post_view(db, post, payload.timezone)


@router.delete("/scheduled-posts/{post_id}", response_model=ScheduledPostView)
def cancel_scheduled_post(post_id: str, actor: CurrentMember, db: DbSession):
    post = _workspace_post(db, actor, post_id)
    if post.status != "cancelled":
        audit.record(
            db, workspace_id=actor.workspace_id, actor=actor, action="post.cancelled",
            target_type="post", target_id=post.id, target_label=_post_label(db, post),
            before=_when(post), after={"status": "cancelled"},
        )
    post.status = "cancelled"
    db.commit()
    return _scheduled_post_view(db, post, post.timezone)


def _when(post: ScheduledPost) -> dict:
    return {
        "scheduled_at": _as_utc(post.scheduled_at).isoformat(),
        "timezone": post.timezone,
        "status": post.status or "scheduled",
    }


def _post_label(db: Session, post: ScheduledPost) -> str | None:
    job = db.get(ApiJob, post.job_id)
    return job.filename if job else None
