from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    event,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    token_version: Mapped[int] = mapped_column(
        default=0,
        server_default="0",
        nullable=False,
    )
    role: Mapped[str] = mapped_column(
        String(16),
        default="user",
        server_default="user",
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )


class Workspace(Base):
    """Tenant boundary. Every job, post and membership belongs to exactly one workspace."""

    __tablename__ = "workspaces"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )
    # Deletion policy (docs/STORAGE.md). NULL days = the installation default.
    # Days to keep a job's original after its upload or calendar date; 0 = forever.
    retention_originals_days: Mapped[int | None] = mapped_column(nullable=True)
    # Delete the original as soon as every publication of its post succeeded.
    retention_after_publish: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="0", nullable=False
    )
    # Delete rejected jobs entirely (draft, versions, comments) this many days after the
    # decision. NULL = keep them.
    retention_rejected_days: Mapped[int | None] = mapped_column(nullable=True)


class WorkspaceMember(Base):
    """A user's role inside one workspace. Permissions come from here, not from users.role."""

    __tablename__ = "workspace_members"

    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True, index=True
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False, default="user")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )


class WorkspaceInvitation(Base):
    """An invitation for one email address to join one workspace with one role.

    Only the SHA-256 of the token is stored; the token itself is shown once, to the inviter.
    The state is derived: accepted (accepted_at), revoked (revoked_at), expired (expires_at in
    the past), otherwise pending. Acceptance is a guarded UPDATE, so a token works once."""

    __tablename__ = "workspace_invitations"
    __table_args__ = (
        Index("ix_workspace_invitations_workspace_email", "workspace_id", "email"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    token_sha256: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    invited_by: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    accepted_by: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AuditEvent(Base):
    """Append-only record of an authorization-relevant change in one workspace.

    Written in the same transaction as the change, so neither commits without the other. Actor
    and target are snapshots (ids and emails as they were) without foreign keys, so history
    survives deleting the people involved. On PostgreSQL a trigger rejects UPDATE and DELETE
    (migration 0014). See docs/AUDIT.md."""

    __tablename__ = "audit_events"
    __table_args__ = (
        Index("ix_audit_events_workspace_id", "workspace_id", "id"),
        Index("ix_audit_events_workspace_target", "workspace_id", "target_id"),
    )

    # Monotonic, so pages are stable (keyset on id) and gaps are visible.
    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True
    )
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="RESTRICT"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )
    actor_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    actor_email: Mapped[str] = mapped_column(String(320), nullable=False)
    action: Mapped[str] = mapped_column(String(48), nullable=False)
    target_type: Mapped[str] = mapped_column(String(24), nullable=False)
    target_id: Mapped[str] = mapped_column(String(64), nullable=False)
    target_label: Mapped[str | None] = mapped_column(String(320), nullable=True)
    before: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    after: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)


class MediaAsset(Base):
    __tablename__ = "media_assets"

    sha256: Mapped[str] = mapped_column(String(64), primary_key=True)
    # Storage key: a filename inside MEDIA_STORAGE_DIR, or an object key for S3.
    stored_filename: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    # Bumped on every upload of these bytes; retention only purges assets idle past the cutoff.
    last_used_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    # Set when retention deleted the original. Drafts survive; the media URL returns 410.
    purged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ApiJob(Base):
    """Durable job record. It doubles as the outbox entry for the Redis queue:
    `enqueued_at IS NULL` means the job was accepted but not yet handed to Redis.

    `idempotency_key` is the client's `Idempotency-Key` header, unique per (workspace, owner),
    so a retried upload returns the original job instead of creating a second one. NULLs are
    distinct, so uploads without a key are unaffected."""

    __tablename__ = "api_jobs"
    __table_args__ = (
        Index("ix_api_jobs_owner_created", "owner_id", "created_at"),
        Index("ix_api_jobs_workspace_created", "workspace_id", "created_at"),
        Index("ix_api_jobs_status_updated", "status", "updated_at"),
        Index(
            "uq_api_jobs_idempotency", "workspace_id", "owner_id", "idempotency_key", unique=True
        ),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    owner_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    media_sha256: Mapped[str | None] = mapped_column(
        ForeignKey("media_assets.sha256", ondelete="RESTRICT"), index=True
    )
    cache_key: Mapped[str | None] = mapped_column(String(512), index=True)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    media_kind: Mapped[str] = mapped_column(String(10), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="queued")
    approval_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending", server_default="pending"
    )
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(String(500), nullable=True)
    enqueued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # The draft version `result` currently holds. 1 is Gemini's draft; each human save adds one
    # (see DraftVersion). Once above 1, nothing from the queue may overwrite `result`.
    draft_version: Mapped[int] = mapped_column(default=1, server_default="1", nullable=False)
    # When the review decision was made (approve/reject); drives rejected-draft retention.
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # This job's original was deleted (policy | published | manual): its media and links answer
    # 410 even if other jobs, possibly in other workspaces, still keep the same bytes.
    media_deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    media_deleted_reason: Mapped[str | None] = mapped_column(String(16), nullable=True)
    recoveries: Mapped[int] = mapped_column(default=0, server_default="0", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class DraftVersion(Base):
    """One immutable version of a job's draft. Version 1 is Gemini's output (stored when the
    first edit happens); every save, including a restore of an older version, adds the next."""

    __tablename__ = "draft_versions"
    __table_args__ = (UniqueConstraint("job_id", "number", name="uq_draft_versions_job_number"),)

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True
    )
    job_id: Mapped[str] = mapped_column(
        ForeignKey("api_jobs.id", ondelete="CASCADE"), nullable=False
    )
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    number: Mapped[int] = mapped_column(nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(String(12), nullable=False)  # ai | edit | restore
    restored_from: Mapped[int | None] = mapped_column(nullable=True)
    note: Mapped[str | None] = mapped_column(String(280), nullable=True)
    # Snapshots, so history survives deleting the author. NULL author means Gemini.
    author_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    author_email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )


class DraftComment(Base):
    """A review comment on a draft: anchored to the version it was written on and, optionally,
    to one field (`instagram.caption`). Replies point at a top-level comment; only top-level
    comments are resolved. `kind` marks the note that came with an approve/reject decision."""

    __tablename__ = "draft_comments"
    __table_args__ = (Index("ix_draft_comments_job_created", "job_id", "created_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    job_id: Mapped[str] = mapped_column(
        ForeignKey("api_jobs.id", ondelete="CASCADE"), nullable=False
    )
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    parent_id: Mapped[str | None] = mapped_column(
        ForeignKey("draft_comments.id", ondelete="CASCADE"), nullable=True
    )
    version: Mapped[int] = mapped_column(nullable=False)
    field: Mapped[str | None] = mapped_column(String(80), nullable=True)
    kind: Mapped[str] = mapped_column(String(12), nullable=False, default="comment")
    body: Mapped[str] = mapped_column(Text, nullable=False)
    author_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    author_email: Mapped[str] = mapped_column(String(320), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_by_email: Mapped[str | None] = mapped_column(String(320), nullable=True)


class ScheduledPost(Base):
    __tablename__ = "scheduled_posts"
    __table_args__ = (
        Index("ix_scheduled_posts_owner_at", "owner_id", "scheduled_at"),
        Index("ix_scheduled_posts_workspace_at", "workspace_id", "scheduled_at"),
        UniqueConstraint("job_id", name="uq_scheduled_posts_job"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    job_id: Mapped[str] = mapped_column(
        ForeignKey("api_jobs.id", ondelete="CASCADE"), nullable=False
    )
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    owner_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    scheduled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="scheduled", server_default="scheduled"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class PublishingConnection(Base):
    """A workspace's authorized account on one publishing provider, for one channel (which part
    of a draft it posts: instagram, facebook, tiktok). Credentials are encrypted at rest
    (app/publishing/crypto.py) and wiped on disconnect; the row stays for history."""

    __tablename__ = "publishing_connections"
    __table_args__ = (Index("ix_publishing_connections_workspace", "workspace_id", "status"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    provider: Mapped[str] = mapped_column(String(24), nullable=False)
    channel: Mapped[str] = mapped_column(String(24), nullable=False)
    account_id: Mapped[str] = mapped_column(String(128), nullable=False)
    account_name: Mapped[str] = mapped_column(String(255), nullable=False)
    # active | needs_reauth | disconnected
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    credentials: Mapped[str | None] = mapped_column(Text, nullable=True)  # Fernet token
    token_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    options: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    last_error: Mapped[str | None] = mapped_column(String(500), nullable=True)
    connected_by_email: Mapped[str] = mapped_column(String(320), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc), nullable=False,
    )


class PublishingAuthState(Base):
    """One pending OAuth authorization: single use, short-lived, bound to the admin and the
    workspace that started it. Only the SHA-256 of `state` is stored; the PKCE verifier is
    encrypted."""

    __tablename__ = "publishing_auth_states"

    state_sha256: Mapped[str] = mapped_column(String(64), primary_key=True)
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    provider: Mapped[str] = mapped_column(String(24), nullable=False)
    channel: Mapped[str] = mapped_column(String(24), nullable=False)
    code_verifier: Mapped[str] = mapped_column(Text, nullable=False)  # Fernet token
    options: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Publication(Base):
    """Delivery of one scheduled post to one connection. The worker's delivery loop drives it
    through the states in docs/PUBLISHING.md; `attempts` doubles as the claim token, so a
    worker whose lease expired can't record a stale outcome."""

    __tablename__ = "publications"
    __table_args__ = (
        UniqueConstraint("scheduled_post_id", "connection_id", name="uq_publications_post_conn"),
        Index("ix_publications_due", "status", "next_attempt_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    scheduled_post_id: Mapped[str] = mapped_column(
        ForeignKey("scheduled_posts.id", ondelete="CASCADE"), nullable=False
    )
    connection_id: Mapped[str] = mapped_column(
        ForeignKey("publishing_connections.id", ondelete="CASCADE"), nullable=False
    )
    # queued | publishing | processing | retrying | published | failed | unknown | cancelled
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="queued")
    attempts: Mapped[int] = mapped_column(default=0, nullable=False)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    last_error: Mapped[str | None] = mapped_column(String(500), nullable=True)
    external_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    external_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    requested_by_email: Mapped[str] = mapped_column(String(320), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc), nullable=False,
    )


class SandboxPost(Base):
    """The sandbox *platform's* side: what the mock provider "published". Not app data; it is
    what a real platform would hold, kept so the sandbox is observable end to end."""

    __tablename__ = "sandbox_posts"

    external_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    account_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    channel: Mapped[str] = mapped_column(String(24), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    hashtags: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    media_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    state: Mapped[str] = mapped_column(String(16), nullable=False)  # processing | published
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )


class SandboxRequest(Base):
    """Every call the sandbox platform received, like a real platform's developer console."""

    __tablename__ = "sandbox_requests"

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True
    )
    account_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)  # publish | check | refresh
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    outcome: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )


ACTIVE_STATUSES = frozenset({"queued", "waiting", "processing", "retrying"})
TERMINAL_STATUSES = frozenset({"done", "failed", "dead", "cancelled", "expired"})


def create_database(url: str):
    if url.startswith("sqlite"):
        engine = create_engine(url, connect_args={"check_same_thread": False})

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_connection, _record):  # pragma: no cover - driver hook
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.close()
    else:
        engine = create_engine(url, pool_pre_ping=True)
    return engine, sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
