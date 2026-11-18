"""Shared builders for API tests: an app on in-memory SQLite plus helpers for accounts."""

from __future__ import annotations

import io
from pathlib import Path

from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.config import ApiSettings
from app.api.database import Base, User, Workspace, WorkspaceMember
from app.api.main import create_app
from app.api.persistence import attach
from app.api.security import hash_password

PASSWORD = "a-very-long-test-password"


# Every engine built here, so a conftest fixture can dispose them after each test even when the
# test leaves early (continue, return, a failed assertion). dispose() is idempotent.
ENGINES: list = []


def sqlite_sessions():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    ENGINES.append(engine)
    Base.metadata.create_all(engine)
    return engine, sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def build_app(runtime, media_dir: Path, *, storage=None, hook_worker=True, **cfg_over):
    engine, sessions = sqlite_sessions()
    if hook_worker:
        attach(runtime.worker, sessions)
    fields = dict(
        database_url="postgresql+psycopg://unused",
        jwt_secret="test-secret-" + "x" * 40,
        token_ttl_minutes=30,
        media_storage_dir=Path(media_dir),
    )
    fields.update(cfg_over)
    application = create_app(
        api_settings=ApiSettings(**fields),
        runtime=runtime,
        engine=engine,
        session_factory=sessions,
        storage=storage,
    )
    return application, engine, sessions


def add_member(sessions, email, role="user", workspace_id="ws-a", platform_role=None) -> str:
    with sessions.begin() as db:
        if db.get(Workspace, workspace_id) is None:
            db.add(Workspace(id=workspace_id, name=workspace_id))
        from sqlalchemy import select

        user = db.scalar(select(User).where(User.email == email))
        if user is None:
            user = User(
                email=email,
                password_hash=hash_password(PASSWORD),
                role=platform_role or "user",
            )
            db.add(user)
            db.flush()
        db.add(WorkspaceMember(workspace_id=workspace_id, user_id=user.id, role=role))
        return user.id


def login(client: TestClient, email: str, workspace_id: str | None = None) -> dict:
    response = client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})
    assert response.status_code == 200, response.text
    headers = {"Authorization": f"Bearer {response.json()['access_token']}"}
    if workspace_id:
        headers["X-Workspace-Id"] = workspace_id
    return headers


def png(color=(1, 2, 3), size=(32, 24)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, "PNG")
    return buffer.getvalue()


def upload(client, headers, content: bytes, name="cat.png", mime="image/png"):
    return client.post("/api/v1/jobs", headers=headers, files={"file": (name, content, mime)})


def send_for_review(client, headers, job_id: str) -> None:
    """The uploader (or an Admin) accepts the draft, putting it in the review queue."""
    response = client.post(f"/api/v1/jobs/{job_id}/submit", headers=headers)
    assert response.status_code == 200, response.text
