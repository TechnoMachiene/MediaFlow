"""Sign-in and signup throttling (app/api/throttle.py)."""

import pytest
from fastapi.testclient import TestClient

from tests.api_harness import PASSWORD, add_member, build_app

pytestmark = pytest.mark.integration


@pytest.fixture
def app_env(make_rt, tmp_path):
    runtime = make_rt()
    application, engine, sessions = build_app(
        runtime, tmp_path / "media", login_max_failures=3, login_ip_max_failures=5,
        allow_signup=True, signup_ip_max=2,
    )
    add_member(sessions, "anna@a.example.com", "admin", "ws-a")
    add_member(sessions, "alice@a.example.com", "user", "ws-a")
    with TestClient(application) as client:
        yield client, runtime
    engine.dispose()


def sign_in(client, email, password=PASSWORD):
    return client.post("/api/v1/auth/login", json={"email": email, "password": password})


def test_an_account_is_blocked_after_repeated_failures_even_with_the_right_password(app_env):
    client, _ = app_env
    for _ in range(3):
        assert sign_in(client, "anna@a.example.com", "wrong-password-123").status_code == 401
    blocked = sign_in(client, "anna@a.example.com")  # correct, but blocked: no oracle
    assert blocked.status_code == 429
    assert 0 < int(blocked.headers["Retry-After"]) <= 900
    assert sign_in(client, "alice@a.example.com").status_code == 200  # others unaffected


def test_a_successful_sign_in_clears_the_accounts_count(app_env):
    client, _ = app_env
    for _ in range(2):
        sign_in(client, "anna@a.example.com", "wrong-password-123")
    assert sign_in(client, "anna@a.example.com").status_code == 200
    for _ in range(2):
        assert sign_in(client, "anna@a.example.com", "wrong-password-123").status_code == 401
    assert sign_in(client, "anna@a.example.com").status_code == 200


def test_one_address_guessing_many_accounts_is_blocked(app_env):
    client, _ = app_env
    for n in range(5):  # spread across accounts, each under its own limit
        sign_in(client, f"guess{n}@a.example.com", "wrong-password-123")
    assert sign_in(client, "alice@a.example.com").status_code == 429


def test_the_block_ends_with_the_window(app_env):
    client, runtime = app_env
    for _ in range(3):
        sign_in(client, "anna@a.example.com", "wrong-password-123")
    assert sign_in(client, "anna@a.example.com").status_code == 429
    for key in runtime.r.scan_iter(match=f"{runtime.settings.prefix}:throttle:login:*"):
        runtime.r.delete(key)  # what expiry does after LOGIN_WINDOW_S
    assert sign_in(client, "anna@a.example.com").status_code == 200


def test_keys_never_hold_raw_emails(app_env):
    client, runtime = app_env
    sign_in(client, "anna@a.example.com", "wrong-password-123")
    keys = [k.decode() for k in runtime.r.scan_iter(
        match=f"{runtime.settings.prefix}:throttle:*")]
    assert keys and not any("anna" in k or "example" in k for k in keys)


def test_signup_is_limited_per_address(app_env):
    client, _ = app_env

    def signup(n):
        return client.post("/api/v1/auth/signup", json={
            "email": f"founder{n}@example.com", "password": "a-very-long-password",
            "workspace_name": f"W{n}"})

    assert [signup(n).status_code for n in range(3)] == [201, 201, 429]


def test_a_redis_outage_never_locks_people_out(app_env, monkeypatch):
    client, _ = app_env
    throttle = client.app.state.throttle

    def down(*args, **kwargs):
        raise ConnectionError("redis down")

    monkeypatch.setattr(throttle.r, "mget", down)
    monkeypatch.setattr(throttle.r, "pipeline", down)
    assert sign_in(client, "anna@a.example.com", "wrong-password-123").status_code == 401
    assert sign_in(client, "anna@a.example.com").status_code == 200
