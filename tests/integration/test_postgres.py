"""Checks that need real PostgreSQL: the migration chain and the audit trail's append-only
trigger. Set POSTGRES_TEST_URL to a server URL with rights to create databases, e.g.
postgresql+psycopg://postgres:postgres@localhost:5432/postgres (CI does). Each run works in a
fresh, throwaway database."""

import os
import uuid
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def fresh_db(monkeypatch):
    server = os.environ.get("POSTGRES_TEST_URL")
    if not server:
        pytest.skip("POSTGRES_TEST_URL not set")
    name = "mq_test_" + uuid.uuid4().hex[:10]
    admin = create_engine(server, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    url = make_url(server).set(database=name).render_as_string(hide_password=False)
    monkeypatch.setenv("DATABASE_URL", url)
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    engine = create_engine(url)
    try:
        yield cfg, engine
    finally:
        engine.dispose()
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


def test_migrations_upgrade_downgrade_and_upgrade_again(fresh_db):
    cfg, engine = fresh_db
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "-1")
    command.upgrade(cfg, "head")
    with engine.connect() as conn:
        tables = set(conn.execute(text(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
        )).scalars())
    assert {"api_jobs", "workspaces", "workspace_invitations", "audit_events"} <= tables


def test_audit_events_are_append_only(fresh_db):
    cfg, engine = fresh_db
    command.upgrade(cfg, "head")
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO workspaces (id, name) VALUES ('ws', 'W')"))
        conn.execute(text(
            "INSERT INTO audit_events (workspace_id, actor_email, action, target_type, "
            "target_id) VALUES ('ws', 'a@example.com', 'member.role_changed', 'user', 'u1')"
        ))
    for statement in (
        "UPDATE audit_events SET actor_email = 'someone-else@example.com'",
        "DELETE FROM audit_events",
    ):
        with pytest.raises(DBAPIError, match="append-only"), engine.begin() as conn:
            conn.execute(text(statement))
    with engine.connect() as conn:
        assert conn.execute(text(
            "SELECT actor_email FROM audit_events"
        )).scalars().all() == ["a@example.com"]
    # A workspace with history can't be deleted out from under it either.
    with pytest.raises(DBAPIError), engine.begin() as conn:
        conn.execute(text("DELETE FROM workspaces WHERE id = 'ws'"))
