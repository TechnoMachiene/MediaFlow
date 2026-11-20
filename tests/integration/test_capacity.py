"""Capacity safeguards: bounded password hashing, the database connection released during a
sign-in, quick 503s instead of slow 500s, and pool settings from the environment."""

import threading

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import TimeoutError as PoolTimeout

from app.api import security
from app.api.database import pool_settings
from tests.api_harness import PASSWORD, add_member, build_app, login
from tests.helpers import ScriptedClient

pytestmark = pytest.mark.integration


@pytest.fixture
def app_client(make_rt, tmp_path):
    runtime = make_rt(client=ScriptedClient())
    application, engine, sessions = build_app(runtime, tmp_path / "media")
    add_member(sessions, "alice@a.example.com", "user", "ws-a")
    with TestClient(application) as client:
        yield client, sessions
    engine.dispose()


def sign_in(client, password=PASSWORD, email="alice@a.example.com"):
    return client.post("/api/v1/auth/login", json={"email": email, "password": password})


def test_sign_ins_beyond_the_hashing_limit_are_told_to_retry_not_failed(app_client, monkeypatch):
    client, _ = app_client
    full = threading.BoundedSemaphore(1)
    full.acquire()  # every hashing slot taken
    monkeypatch.setattr(security, "_slots", full)
    monkeypatch.setattr(security, "HASH_WAIT_S", 0.05)
    busy = sign_in(client)
    assert busy.status_code == 503
    assert busy.headers["retry-after"] == "2" and "Try again" in busy.json()["detail"]
    full.release()
    assert sign_in(client).status_code == 200


def test_the_database_connection_is_free_while_the_password_is_checked(app_client, monkeypatch):
    client, sessions = app_client
    engine = sessions.kw["bind"]
    seen = {}
    real = security.verify_password

    def watching(password, hashed):
        seen["checked_out"] = engine.pool.checkedout() if hasattr(engine.pool, "checkedout") \
            else None
        return real(password, hashed)

    monkeypatch.setattr(security, "verify_password", watching)
    assert login(client, "alice@a.example.com")["Authorization"].startswith("Bearer ")
    # SQLite's test pool doesn't count; PostgreSQL's QueuePool does, and must read 0 here.
    assert seen["checked_out"] in (0, None)


def test_unknown_emails_cost_as_much_as_wrong_passwords(app_client, monkeypatch):
    client, _ = app_client
    checks = []
    real = security.verify_password
    monkeypatch.setattr(security, "verify_password",
                        lambda p, h: checks.append(h) or real(p, h))
    assert sign_in(client, password="wrong-password-123").status_code == 401
    assert sign_in(client, email="nobody@a.example.com").status_code == 401
    assert len(checks) == 2  # the unknown address was checked against a stand-in hash too


def test_a_full_connection_pool_answers_503_quickly(app_client):
    client, _ = app_client

    def _probe():
        raise PoolTimeout("QueuePool limit reached")

    client.app.add_api_route("/api/v1/_pool_full_probe", _probe)
    routes = client.app.router.routes
    routes.insert(0, routes.pop())  # ahead of the web app's catch-all route
    response = client.get("/api/v1/_pool_full_probe")
    assert response.status_code == 503 and response.headers["retry-after"] == "2"


def test_pool_settings_come_from_the_environment(monkeypatch):
    for name in ("DB_POOL_SIZE", "DB_MAX_OVERFLOW", "DB_POOL_TIMEOUT", "DB_POOL_RECYCLE"):
        monkeypatch.delenv(name, raising=False)
    assert pool_settings() == {"pool_size": 20, "max_overflow": 20, "pool_timeout": 10,
                               "pool_recycle": 1800}
    monkeypatch.setenv("DB_POOL_SIZE", "30")
    monkeypatch.setenv("DB_MAX_OVERFLOW", "0")
    monkeypatch.setenv("DB_POOL_TIMEOUT", "nonsense")
    assert pool_settings() | {} == {"pool_size": 30, "max_overflow": 0, "pool_timeout": 10,
                                    "pool_recycle": 1800}
