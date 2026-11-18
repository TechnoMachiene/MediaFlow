"""Demo mode: the full product with no Gemini key, no PostgreSQL, and one process."""

import json
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.api.config import ApiSettings
from app.api.database import User, WorkspaceMember
from app.api.main import create_app
from app.config import Settings
from app.demo import (
    DEMO_ACCOUNTS,
    DEMO_PASSWORD,
    DemoClient,
    DemoIsolationError,
    demo_banner,
    main,
    seed,
)
from app.errors import ConfigError, Permanent
from app.gemini_client import build_client
from app.jobs import JobSpec
from tests.api_harness import png, sqlite_sessions

pytestmark = pytest.mark.integration


def spec(path, kind="image", sha="ab" * 32):
    return JobSpec("j1", str(path), kind, "image/png", "prompt", "demo", sha)


def test_demo_client_writes_the_same_shape_as_the_converted_gemini_reply(
        tmp_path, make_image, make_video):
    client = DemoClient(delay_s=0)
    red = make_image(color=(220, 20, 30), size=(60, 120))
    out = client.analyse(spec(red))
    draft = json.loads(out["text"])
    assert set(draft) == {"facebook", "instagram", "content_summary"}  # Meta only, like the prompt
    assert "red" in draft["instagram"]["caption"].lower()
    assert "red" in draft["facebook"]["caption"].lower()
    assert draft["instagram"]["hashtags"][0] == "#Demo"
    assert "portrait" in draft["content_summary"]["alt_text"][0]
    assert out["model"] == "demo" and out["kind"] == "image"
    assert client.analyse(spec(red)) == out  # deterministic

    video = json.loads(client.analyse(spec(make_video(), kind="video"))["text"])
    assert video["instagram"]["cover_title"] and video["instagram"]["reel_on_screen_text"]
    for colour, word in (((10, 10, 10), "dark"), ((250, 250, 250), "white"),
                         ((128, 128, 128), "grey"), ((20, 60, 220), "blue")):
        text = client.analyse(spec(make_image(color=colour)))["text"]
        assert word in json.loads(text)["instagram"]["caption"].lower()

    with pytest.raises(Permanent):
        client.analyse(spec(tmp_path / "missing.png"))
    broken = tmp_path / "broken.png"
    broken.write_bytes(b"not an image")
    with pytest.raises(Permanent):
        client.analyse(spec(broken))


def test_demo_settings_need_no_gemini_key_postgres_or_secret(monkeypatch):
    for name in ("GEMINI_API_KEY", "GEMINI_MODEL", "DATABASE_URL", "JWT_SECRET",
                 "ALLOW_SIGNUP", "EMBEDDED_WORKER", "QUEUE_PREFIX"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DEMO_MODE", "1")
    core = Settings.from_env()
    assert core.demo_mode and core.model == "demo" and core.prefix == "demo"
    assert isinstance(build_client(core), DemoClient)
    api = ApiSettings.from_env()
    assert api.database_url.startswith("sqlite") and api.embedded_worker and api.allow_signup

    monkeypatch.setenv("DEMO_MODE", "maybe")
    with pytest.raises(ConfigError, match="DEMO_MODE"):
        Settings.from_env()
    monkeypatch.setenv("DEMO_MODE", "0")
    with pytest.raises(ConfigError, match="DEMO_MODE=1"):
        Settings.from_env()  # the error tells you about demo mode


def test_scripted_provider_is_confined_to_demo_mode(monkeypatch):
    """The DemoClient can only exist in DEMO_MODE=1, and DEMO_MODE=1 refuses to run anywhere
    it could be mistaken for, or mixed into, a real deployment."""
    monkeypatch.delenv("GEMINI_MODEL", raising=False)
    monkeypatch.delenv("QUEUE_PREFIX", raising=False)
    monkeypatch.setenv("DEMO_MODE", "1")

    monkeypatch.setenv("GEMINI_API_KEY", "a-real-key")  # e.g. a production .env
    with pytest.raises(DemoIsolationError, match="GEMINI_API_KEY"):
        Settings.from_env()
    monkeypatch.setenv("GEMINI_API_KEY", "")

    monkeypatch.setenv("QUEUE_PREFIX", "ma")  # the real queue
    with pytest.raises(DemoIsolationError, match="QUEUE_PREFIX"):
        Settings.from_env()
    monkeypatch.setenv("QUEUE_PREFIX", "demo-staging")
    assert Settings.from_env().prefix == "demo-staging"

    # Real mode always gets the real client, whatever the model is called.
    monkeypatch.setenv("DEMO_MODE", "0")
    monkeypatch.setenv("GEMINI_API_KEY", "a-real-key")
    monkeypatch.setenv("GEMINI_MODEL", "demo")
    monkeypatch.setenv("QUEUE_PREFIX", "ma")
    real = Settings.from_env()
    assert not real.demo_mode and type(build_client(real)).__name__ == "GeminiClient"

    # Hand-built settings cannot slip past the guard either.
    for bad in (replace(real, demo_mode=True), replace(real, demo_mode=True, api_key="")):
        with pytest.raises(DemoIsolationError):
            build_client(bad)


def test_demo_never_seeds_a_database_with_real_accounts():
    engine, sessions = sqlite_sessions()
    with sessions.begin() as db:
        db.add(User(email="real-customer@example.com", password_hash="x"))
    with pytest.raises(DemoIsolationError, match="real accounts"):
        seed(sessions)
    with sessions() as db:
        assert db.query(User).count() == 1  # nothing was added
    engine.dispose()

    # A demo database keeps working after visitors sign up through the demo.
    engine, sessions = sqlite_sessions()
    seed(sessions)
    with sessions.begin() as db:
        db.add(User(email="visitor@example.com", password_hash="x"))
    assert seed(sessions) == list(DEMO_ACCOUNTS)
    engine.dispose()


def test_banner_documents_every_demo_login():
    banner = demo_banner()
    assert DEMO_PASSWORD in banner and "no Gemini calls" in banner
    for email, _ in DEMO_ACCOUNTS:
        assert email in banner


def test_seed_is_idempotent_and_refuses_outside_demo_mode(monkeypatch, capsys):
    engine, sessions = sqlite_sessions()
    assert seed(sessions) == list(DEMO_ACCOUNTS)
    with sessions() as db:
        versions = {u.email: u.token_version for u in db.query(User)}
    seed(sessions)
    with sessions() as db:
        assert db.query(User).count() == 3 and db.query(WorkspaceMember).count() == 3
        assert {u.email: u.token_version for u in db.query(User)} == versions
    engine.dispose()

    monkeypatch.delenv("DEMO_MODE", raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:3f9c1a7e5b2d8c4f6a0e9b1d@unused/db")
    monkeypatch.setenv("JWT_SECRET", "x" * 40)
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: False)
    assert main(["seed"]) == 2
    assert "DEMO_MODE=1" in capsys.readouterr().err


def test_seed_command_creates_a_sqlite_demo_database(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("DEMO_MODE", "1")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'demo.db'}")
    monkeypatch.delenv("JWT_SECRET", raising=False)
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: False)
    assert main(["seed"]) == 0
    out = capsys.readouterr().out
    assert DEMO_PASSWORD in out and "demo-admin@example.com" in out


def test_demo_app_end_to_end_with_embedded_worker(make_rt, tmp_path):
    """Sign in with a published demo account, upload, and get a draft back - no worker
    process, no Gemini, no PostgreSQL."""
    runtime = make_rt(client=DemoClient(delay_s=0))
    cfg = ApiSettings(
        database_url=f"sqlite:///{tmp_path / 'demo.sqlite3'}",
        jwt_secret="demo-" + "x" * 40,
        token_ttl_minutes=30,
        media_storage_dir=tmp_path / "media",
        demo_mode=True,
        embedded_worker=True,
        allow_signup=True,
    )
    with TestClient(create_app(api_settings=cfg, runtime=runtime)) as client:
        meta = client.get("/api/v1/meta").json()
        assert meta["demo_mode"] and meta["demo_password"] == DEMO_PASSWORD
        member = next(a["email"] for a in meta["demo_accounts"] if a["role"] == "user")
        token = client.post(
            "/api/v1/auth/login", json={"email": member, "password": meta["demo_password"]}
        ).json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}
        assert client.get("/api/v1/auth/me", headers=headers).json()["workspace"]["name"] == (
            "Demo Studio"
        )

        job_id = client.post(
            "/api/v1/jobs", headers=headers, files={"file": ("demo.png", png(), "image/png")}
        ).json()["job_id"]
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            job = client.get(f"/api/v1/jobs/{job_id}", headers=headers).json()
            if job["status"] == "done":
                break
            time.sleep(0.05)
        assert job["status"] == "done", job
        assert job["result"]["model"] == "demo"
        assert "instagram" in json.loads(job["result"]["text"])

        # The rest of the workflow, each step by the role that does it for real.
        assert client.post(f"/api/v1/jobs/{job_id}/submit", headers=headers).status_code == 200
        manager = next(a["email"] for a in meta["demo_accounts"] if a["role"] == "manager")
        manager_headers = {"Authorization": "Bearer " + client.post(
            "/api/v1/auth/login", json={"email": manager, "password": DEMO_PASSWORD}
        ).json()["access_token"]}
        review = client.get("/api/v1/jobs/review", headers=manager_headers).json()["items"]
        assert job_id in {item["job_id"] for item in review}
        assert client.patch(
            f"/api/v1/jobs/{job_id}/approval", headers=manager_headers,
            json={"status": "approved", "checked": True},
        ).status_code == 200

        when = datetime.now(timezone.utc) + timedelta(days=2)
        post = client.post(
            f"/api/v1/jobs/{job_id}/schedule", headers=headers,
            json={"scheduled_at": when.isoformat(), "timezone": "UTC"},
        )
        assert post.status_code == 201, post.text
        day = post.json()["local_date"]
        calendar = client.get(
            "/api/v1/calendar", headers=headers,
            params={"start": day, "end": day, "timezone": "UTC"},
        ).json()
        assert [p["id"] for p in calendar["days"][0]["posts"]] == [post.json()["id"]]


def test_meta_outside_demo_mode_reveals_nothing(make_rt, tmp_path):
    from tests.api_harness import build_app

    application, engine, _ = build_app(make_rt(), tmp_path)
    with TestClient(application) as client:
        assert client.get("/api/v1/meta").json() == {
            "demo_mode": False, "allow_signup": False, "demo_password": None, "demo_accounts": []
        }
    engine.dispose()
