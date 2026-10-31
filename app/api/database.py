from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, DateTime, ForeignKey, Index, String, create_engine, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(
        String(16),
        default="user",
        server_default="user",
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )


class MediaAsset(Base):
    __tablename__ = "media_assets"

    sha256: Mapped[str] = mapped_column(String(64), primary_key=True)
    stored_filename: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ApiJob(Base):
    __tablename__ = "api_jobs"
    __table_args__ = (Index("ix_api_jobs_owner_created", "owner_id", "created_at"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
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
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


def create_database(url: str):
    engine = create_engine(url, pool_pre_ping=True)
    return engine, sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
