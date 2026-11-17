"""Operator commands: admin bootstrap, and one-shot reconcile / prune-media / worker."""

import json
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import update

from app.api import admin
from app.api.database import (
    ApiJob,
    Base,
    MediaAsset,
    User,
    Workspace,
    WorkspaceMember,
    create_database,
)
from app.cli import main
from tests.helpers import ScriptedClient

pytestmark = pytest.mark.integration
LONG_AGO = datetime(2000, 1, 1, tzinfo=timezone.utc)


@pytest.fixture
def db_url(tmp_path):
    url = f"sqlite:///{tmp_path / 'ops.db'}"
    engine, _ = create_database(url)
    Base.metadata.create_all(engine)
    engine.dispose()
    return url


def test_admin_command_bootstraps_a_workspace_on_a_fresh_install(db_url, monkeypatch, capsys):
    monkeypatch.setenv("DATABASE_URL", db_url)
    monkeypatch.setenv("DEMO_MODE", "1")  # lets the admin command accept SQLite here
    monkeypatch.setattr("app.api.admin.load_dotenv", lambda *a, **k: False)
    monkeypatch.setattr("getpass.getpass", lambda prompt="": "a-very-long-admin-password")
    assert admin.main(["first-admin@example.com"]) == 0
    assert "Default workspace" in capsys.readouterr().out
    assert admin.main(["first-admin@example.com", "--workspace", "Second"]) == 0

    engine, sessions = create_database(db_url)
    with sessions() as db:
        user = db.query(User).filter_by(email="first-admin@example.com").one()
        assert user.role == "admin"
        memberships = {
            db.get(Workspace, m.workspace_id).name: m.role
            for m in db.query(WorkspaceMember).filter_by(user_id=user.id)
        }
        assert memberships == {"Default workspace": "admin", "Second": "admin"}
    engine.dispose()


@pytest.fixture
def ops_env(monkeypatch, tmp_path, redis_client, db_url):
    prefix = "tops" + uuid.uuid4().hex[:8]
    monkeypatch.setenv("DATABASE_URL", db_url)
    monkeypatch.setenv("MEDIA_STORAGE_DIR", str(tmp_path / "media"))
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("GEMINI_MODEL", "test-model")
    monkeypatch.setenv("QUEUE_PREFIX", prefix)
    monkeypatch.setenv("GEMINI_RPM", "600")
    monkeypatch.setenv("GEMINI_BURST", "10")
    gemini = ScriptedClient()
    monkeypatch.setattr("app.wiring.build_client", lambda settings: gemini)
    yield db_url, gemini, tmp_path / "media"
    for k in redis_client.scan_iter(match=f"{prefix}:*", count=500):
        redis_client.delete(k)


def make_undispatched_job(db_url, media_dir, png_bytes):
    """A job row in exactly the state a crashed API leaves behind."""
    media_dir.mkdir(parents=True, exist_ok=True)
    (media_dir / "photo.png").write_bytes(png_bytes)
    engine, sessions = create_database(db_url)
    with sessions.begin() as db:
        db.add(Workspace(id="ws", name="ws"))
        db.add(User(id="u", email="u@example.com", password_hash="x"))
        db.add(MediaAsset(sha256="a" * 64, stored_filename="photo.png"))
        db.flush()
        db.add(ApiJob(id="j" * 32, workspace_id="ws", owner_id="u", media_sha256="a" * 64,
                      filename="photo.png", media_kind="image", status="queued"))
    return engine, sessions


def test_reconcile_then_worker_then_prune(ops_env, capsys):
    from tests.api_harness import png

    db_url, gemini, media_dir = ops_env
    engine, sessions = make_undispatched_job(db_url, media_dir, png())
    with sessions.begin() as db:  # pretend it has been stuck for a while
        db.execute(update(ApiJob).values(created_at=LONG_AGO))

    assert main(["reconcile"]) == 0
    assert json.loads(capsys.readouterr().out.strip().splitlines()[-1]) == {"dispatched": 1}
    assert main(["worker", "--drain"]) == 0
    with sessions() as db:
        job = db.get(ApiJob, "j" * 32)
        assert job.status == "done" and job.result["text"].startswith("scripted")
    assert len(gemini.calls) == 1

    with sessions.begin() as db:
        db.execute(update(MediaAsset).values(last_used_at=LONG_AGO))
    capsys.readouterr()
    assert main(["prune-media"]) == 0
    assert json.loads(capsys.readouterr().out.strip())["objects_purged"] == 1
    assert not (media_dir / "photo.png").exists()
    engine.dispose()


def test_db_commands_need_a_database(ops_env, monkeypatch, capsys):
    monkeypatch.delenv("DATABASE_URL")
    monkeypatch.delenv("DEMO_MODE", raising=False)
    assert main(["reconcile"]) == 2
    assert "DATABASE_URL" in capsys.readouterr().err
