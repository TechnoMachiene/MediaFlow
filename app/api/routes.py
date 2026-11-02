from __future__ import annotations

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
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
    status,
)
from fastapi.responses import FileResponse
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.database import (
    ApiJob,
    MediaAsset,
    ScheduledPost,
    User,
)
from app.api.dependencies import CurrentUser, DbSession, QueueRuntime
from app.api.media_storage import resolve_media_path, store_media_asset
from app.api.roles import SUPER_ADMIN_EMAIL, is_admin, is_reviewer, is_super_admin
from app.api.schemas import (
    ApprovalDecision,
    CalendarDay,
    CalendarResponse,
    Credentials,
    JobPage,
    JobResponse,
    JobView,
    RoleUpdate,
    ScheduledPostPage,
    ScheduledPostView,
    SchedulePostRequest,
    StatsResponse,
    TokenResponse,
    UserCreate,
    UserPage,
    UserUpdate,
    UserView,
)
from app.api.security import create_versioned_access_token, hash_password, verify_password
from app.errors import MediaError
from app.media import ALLOWED, inspect_media

router = APIRouter(prefix="/api/v1")
JOB_RESULT_WAIT_S = 5 * 60
JOB_RESULT_POLL_S = 0.25


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


@router.post("/users", response_model=UserView, status_code=status.HTTP_201_CREATED)
def create_user(
    payload: UserCreate,
    actor: CurrentUser,
    db: DbSession,
):
    if not is_reviewer(actor):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only managers and admins can create users")
    if actor.role == "manager" and payload.role not in {"user", "manager"}:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Managers can only create user and manager accounts",
        )
    if str(payload.email).lower() == SUPER_ADMIN_EMAIL:
        raise HTTPException(status.HTTP_409_CONFLICT, "The super-admin account is reserved")
    user = User(
        email=str(payload.email),
        password_hash=hash_password(payload.password),
        role=payload.role,
    )
    db.add(user)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "An account with that email already exists",
        ) from None
    db.refresh(user)
    return UserView(id=user.id, email=user.email, role=user.role, created_at=user.created_at)


@router.get("/users", response_model=UserPage)
def list_users(
    actor: CurrentUser,
    db: DbSession,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
):
    if not is_admin(actor):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Admin role required")
    rows = db.scalars(
        select(User).order_by(User.created_at.desc(), User.id).limit(limit).offset(offset)
    ).all()
    return UserPage(
        items=[
            UserView(id=row.id, email=row.email, role=row.role, created_at=row.created_at)
            for row in rows
        ],
        limit=limit,
        offset=offset,
    )


@router.get("/users/{user_id}", response_model=UserView)
def get_user(user_id: str, actor: CurrentUser, db: DbSession):
    if not is_admin(actor):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Admin role required")
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    return UserView(id=user.id, email=user.email, role=user.role, created_at=user.created_at)


def _protect_super_admin(target: User) -> None:
    if is_super_admin(target):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "The super-admin account cannot be modified or deleted through the API",
        )


@router.patch("/users/{user_id}/role", response_model=UserView)
def update_user_role(
    user_id: str,
    payload: RoleUpdate,
    actor: CurrentUser,
    db: DbSession,
):
    if not is_admin(actor):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Admin role required")
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    _protect_super_admin(user)
    if is_admin(user) and payload.role != "admin":
        admin_count = db.scalar(
            select(func.count()).select_from(User).where(User.role.in_(("admin", "super_admin")))
        )
        if admin_count <= 1:
            raise HTTPException(status.HTTP_409_CONFLICT, "Cannot demote the last admin")
    user.role = payload.role
    db.commit()
    db.refresh(user)
    return UserView(id=user.id, email=user.email, role=user.role, created_at=user.created_at)


@router.patch("/users/{user_id}", response_model=UserView)
def update_user(user_id: str, payload: UserUpdate, actor: CurrentUser, db: DbSession):
    if not is_admin(actor):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Admin role required")
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    _protect_super_admin(user)

    if payload.email is not None:
        if str(payload.email).lower() == SUPER_ADMIN_EMAIL:
            raise HTTPException(status.HTTP_409_CONFLICT, "The super-admin email is reserved")
        user.email = str(payload.email).lower()
    if payload.password is not None:
        user.password_hash = hash_password(payload.password)
        user.token_version += 1
    if payload.role is not None:
        if is_admin(user) and payload.role != "admin":
            admin_count = db.scalar(
                select(func.count())
                .select_from(User)
                .where(User.role.in_(("admin", "super_admin")))
            )
            if admin_count <= 1:
                raise HTTPException(status.HTTP_409_CONFLICT, "Cannot demote the last admin")
        user.role = payload.role

    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "An account with that email already exists",
        ) from None
    db.refresh(user)
    return UserView(id=user.id, email=user.email, role=user.role, created_at=user.created_at)


@router.delete("/users/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_user(
    user_id: str,
    actor: CurrentUser,
    db: DbSession,
    runtime: QueueRuntime,
):
    if not is_admin(actor):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Admin role required")
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")
    _protect_super_admin(user)

    jobs = db.scalars(select(ApiJob).where(ApiJob.owner_id == user_id)).all()
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

    db.execute(delete(ApiJob).where(ApiJob.owner_id == user_id))
    db.delete(user)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


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


def _job_view(row: ApiJob, data: dict, *, submitter_email: str | None = None) -> JobView:
    live_status = data.get("status", "unknown")
    if live_status == "unknown":
        live_status = "done" if row.result is not None else "expired"
    result = data.get("result")
    if result is None:
        result = row.result
    if result is not None:
        live_status = "done"
    row.status = live_status
    if result is not None:
        row.result = result
    return JobView(
        job_id=row.id,
        status=live_status,
        approval_status=row.approval_status,
        submitter_email=submitter_email,
        original_filename=row.filename,
        attempts=data.get("attempts", 0),
        cached=data.get("cached", False),
        error=data.get("error"),
        result=result,
        filename=row.filename,
        media_kind=row.media_kind,
        media_url=f"/api/v1/jobs/{row.id}/media",
        created_at=row.created_at,
    )


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
    user: CurrentUser,
    db: DbSession,
    runtime: QueueRuntime,
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
    storage_dir: Path = request.app.state.api_settings.media_storage_dir
    storage_dir.mkdir(mode=0o750, parents=True, exist_ok=True)
    staged_path = storage_dir / f".upload-{uuid.uuid4().hex}{suffix}"

    size = 0
    try:
        with staged_path.open("xb") as target:
            while chunk := file.file.read(1024 * 1024):
                size += len(chunk)
                if size > max_bytes:
                    raise HTTPException(
                        status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                        "Upload exceeds the configured size limit",
                    )
                target.write(chunk)
        os.chmod(staged_path, 0o600)
        if size == 0:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Uploaded file is empty")
        inspect_media(
            str(staged_path),
            max_image_mb=settings.max_image_mb,
            max_video_mb=settings.max_video_upload_mb,
        )

        media_sha256 = store_media_asset(db, staged_path, original_name, storage_dir)
        asset = db.get(MediaAsset, media_sha256)
        if asset is None:
            raise RuntimeError("Stored media asset was not available after registration")
        path = storage_dir / asset.stored_filename

        row = ApiJob(
            id=job_id,
            owner_id=user.id,
            media_sha256=media_sha256,
            filename=original_name[:255],
            media_kind=kind,
            status="queued",
        )
        db.add(row)
        db.commit()
        try:
            result = runtime.store.submit(user.id, str(path), job_id=job_id)
        except Exception:
            db.delete(row)
            db.commit()
            raise
        row.status = result["status"]
        row.cache_key = result["cache_key"]
        deadline = time.monotonic() + JOB_RESULT_WAIT_S
        job_status = runtime.store.status(job_id)
        while (
            job_status["status"] in {"queued", "waiting", "processing", "retrying"}
            and time.monotonic() < deadline
        ):
            time.sleep(min(JOB_RESULT_POLL_S, max(0, deadline - time.monotonic())))
            job_status = runtime.store.status(job_id)

        row.status = job_status["status"]
        row.result = job_status.get("result")
        db.commit()
        if job_status["status"] == "done":
            response.status_code = status.HTTP_200_OK
        elif job_status["status"] in {"failed", "dead"}:
            response.status_code = status.HTTP_502_BAD_GATEWAY
        return JobResponse(
            job_id=job_id,
            status=job_status["status"],
            cached=job_status.get("cached", result["cached"]),
            result=row.result,
            error=job_status.get("error"),
        )
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


def _require_reviewer(user: User) -> None:
    if not is_reviewer(user):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Manager or admin role required")


def _approval_jobs(status_value: Literal["pending", "approved", "rejected"], *, actor, db, runtime,
                   limit: int, offset: int) -> JobPage:
    _require_reviewer(actor)
    query = select(ApiJob).where(
        ApiJob.status == "done",
        ApiJob.approval_status == status_value,
    )
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


@router.get("/jobs/review", response_model=JobPage)
def list_jobs_for_review(
    actor: CurrentUser,
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
    actor: CurrentUser,
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
    actor: CurrentUser,
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
    actor: CurrentUser,
    db: DbSession,
    runtime: QueueRuntime,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
):
    rows = db.scalars(
        select(ApiJob)
        .where(ApiJob.owner_id == actor.id)
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
    actor: CurrentUser,
    db: DbSession,
    runtime: QueueRuntime,
):
    _require_reviewer(actor)
    row = db.get(ApiJob, job_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Job not found")
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
    view = _job_view(
        row,
        runtime.store.status(job_id),
        submitter_email=owner.email if owner else None,
    )
    db.commit()
    return view


@router.get("/jobs/{job_id}/media")
def get_job_media(
    job_id: str,
    request: Request,
    user: CurrentUser,
    db: DbSession,
):
    row = db.get(ApiJob, job_id)
    if row is None or (
        not is_reviewer(user) and row.owner_id != user.id
    ):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Job not found")
    suffix = Path(row.filename).suffix.lower()
    media = ALLOWED.get(suffix)
    if media is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Uploaded media not found")
    media_path = resolve_media_path(
        db, row.id, row.filename, request.app.state.api_settings.media_storage_dir
    )
    if not media_path.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Uploaded media not found")
    return FileResponse(media_path, media_type=media[1], filename=row.filename)


@router.get("/jobs", response_model=JobPage)
def list_jobs(
    actor: CurrentUser,
    db: DbSession,
    runtime: QueueRuntime,
    kind: Literal["image", "video"] | None = None,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
):
    if not is_admin(actor):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Admin role required")
    query = select(ApiJob)
    if kind is not None:
        query = query.where(ApiJob.media_kind == kind)
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


@router.get("/jobs/{job_id}", response_model=JobView)
def get_job(
    job_id: str,
    user: CurrentUser,
    db: DbSession,
    runtime: QueueRuntime,
):
    query = select(ApiJob).where(ApiJob.id == job_id)
    if not is_admin(user):
        query = query.where(ApiJob.owner_id == user.id)
    row = db.scalar(query)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Job not found")
    owner = db.get(User, row.owner_id) if is_reviewer(user) else None
    view = _job_view(
        row,
        runtime.store.status(job_id),
        submitter_email=owner.email if owner else None,
    )
    db.commit()
    return view


@router.delete("/jobs/{job_id}", response_model=JobView)
def cancel_job(
    job_id: str,
    user: CurrentUser,
    db: DbSession,
    runtime: QueueRuntime,
):
    query = select(ApiJob).where(ApiJob.id == job_id)
    if not is_admin(user):
        query = query.where(ApiJob.owner_id == user.id)
    row = db.scalar(query)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Job not found")
    if not runtime.store.cancel(job_id):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Job is already running or cannot be cancelled",
        )
    view = _job_view(row, runtime.store.status(job_id))
    db.commit()
    return view


@router.get("/stats", response_model=StatsResponse)
def stats(
    user: CurrentUser,
    runtime: QueueRuntime,
):
    if not is_admin(user):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Admin role required")
    return StatsResponse(**runtime.queue.depths(), gemini_calls_today=runtime.limiter.used_today())


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


def _list_scheduled_posts(
    db: Session,
    user: User,
    start: date,
    end: date,
    timezone_name: str,
    *,
    limit: int,
    offset: int = 0,
) -> list[ScheduledPostView]:
    beginning, after_end = _calendar_bounds(start, end, timezone_name)
    query = select(ScheduledPost).where(
        ScheduledPost.scheduled_at >= beginning,
        ScheduledPost.scheduled_at < after_end,
    )
    if not is_admin(user):
        query = query.where(ScheduledPost.owner_id == user.id)
    posts = db.scalars(
        query.order_by(ScheduledPost.scheduled_at, ScheduledPost.id)
        .limit(limit)
        .offset(offset)
    ).all()
    return [_scheduled_post_view(db, post, timezone_name) for post in posts]


@router.post(
    "/jobs/{job_id}/schedule",
    response_model=ScheduledPostView,
    status_code=status.HTTP_201_CREATED,
)
def schedule_job(
    job_id: str,
    payload: SchedulePostRequest,
    actor: CurrentUser,
    db: DbSession,
):
    job_query = select(ApiJob).where(ApiJob.id == job_id)
    job_query = job_query.where(ApiJob.owner_id == actor.id)
    job = db.scalar(job_query)
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Job not found")
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
        owner_id=job.owner_id,
        scheduled_at=payload.scheduled_at.astimezone(timezone.utc),
        timezone=payload.timezone,
    )
    db.add(post)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT, "This job already has a scheduled post"
        ) from None
    return _scheduled_post_view(db, post, payload.timezone)


@router.get("/scheduled-posts", response_model=ScheduledPostPage)
def list_scheduled_posts(
    actor: CurrentUser,
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
    actor: CurrentUser,
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
def get_scheduled_post(post_id: str, actor: CurrentUser, db: DbSession):
    query = select(ScheduledPost).where(ScheduledPost.id == post_id)
    if not is_admin(actor):
        query = query.where(ScheduledPost.owner_id == actor.id)
    post = db.scalar(query)
    if post is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Scheduled post not found")
    return _scheduled_post_view(db, post, post.timezone)


@router.patch("/scheduled-posts/{post_id}", response_model=ScheduledPostView)
def reschedule_post(
    post_id: str,
    payload: SchedulePostRequest,
    actor: CurrentUser,
    db: DbSession,
):
    query = select(ScheduledPost).where(ScheduledPost.id == post_id)
    if not is_admin(actor):
        query = query.where(ScheduledPost.owner_id == actor.id)
    post = db.scalar(query)
    if post is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Scheduled post not found")
    if payload.scheduled_at.astimezone(timezone.utc) <= datetime.now(timezone.utc):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "scheduled_at must be in the future"
        )
    post.scheduled_at = payload.scheduled_at.astimezone(timezone.utc)
    post.timezone = payload.timezone
    post.status = "scheduled"
    db.commit()
    return _scheduled_post_view(db, post, payload.timezone)


@router.delete("/scheduled-posts/{post_id}", response_model=ScheduledPostView)
def cancel_scheduled_post(post_id: str, actor: CurrentUser, db: DbSession):
    query = select(ScheduledPost).where(ScheduledPost.id == post_id)
    if not is_admin(actor):
        query = query.where(ScheduledPost.owner_id == actor.id)
    post = db.scalar(query)
    if post is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Scheduled post not found")
    post.status = "cancelled"
    db.commit()
    return _scheduled_post_view(db, post, post.timezone)
