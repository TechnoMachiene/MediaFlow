from __future__ import annotations

import io
import threading
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.config import ApiSettings
from app.api.database import ApiJob, Base, User
from app.api.main import create_app
from app.api.persistence import persist_api_result
from app.api.security import hash_password
from app.errors import Permanent
from tests.helpers import ScriptedClient

pytestmark = pytest.mark.integration


def sample_image(color=(1, 2, 3)):
    buffer = io.BytesIO()
    Image.new("RGB", (32, 24), color).save(buffer, "PNG")
    return buffer.getvalue()


@pytest.fixture
def api_client(make_rt, tmp_path, monkeypatch):
    monkeypatch.setattr("app.api.routes.JOB_RESULT_WAIT_S", 0)
    runtime = make_rt(client=ScriptedClient())
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    runtime.worker.on_result = lambda job_id, result: persist_api_result(sessions, job_id, result)
    cfg = ApiSettings(
        database_url="postgresql+psycopg://unused",
        jwt_secret="test-secret-" + "x" * 40,
        token_ttl_minutes=30,
        media_storage_dir=Path(tmp_path / "media"),
    )
    application = create_app(
        api_settings=cfg,
        runtime=runtime,
        engine=engine,
        session_factory=sessions,
    )
    with TestClient(application) as client:
        yield client, runtime
    engine.dispose()


def create_account(client: TestClient, email, role="user"):
    with client.app.state.session_factory.begin() as db:
        user = User(
            email=email, password_hash=hash_password("a-very-long-test-password"), role=role
        )
        db.add(user)
        db.flush()
        user_id = user.id
    response = client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": "a-very-long-test-password"},
    )
    assert response.status_code == 200, response.text
    return user_id, response.json()["access_token"]


def test_login_and_manager_user_creation(api_client):
    client, _ = api_client
    assert client.get("/api/v1/jobs").status_code == 401
    _, manager = create_account(client, "manager@example.com", role="manager")
    manager_headers = {"Authorization": f"Bearer {manager}"}
    assert client.get("/api/v1/jobs", headers={"Authorization": manager}).status_code == 401
    logged_in = client.post(
        "/api/v1/auth/login",
        json={"email": "MANAGER@example.com", "password": "a-very-long-test-password"},
    )
    assert logged_in.status_code == 200
    assert logged_in.json()["access_token"]
    created = client.post(
        "/api/v1/users",
        headers=manager_headers,
        json={
            "email": "person@example.com",
            "password": "a-very-long-test-password",
        },
    )
    assert created.status_code == 201, created.text
    assert created.json()["role"] == "user"
    assert (
        client.post(
            "/api/v1/users",
            headers=manager_headers,
            json={
                "email": "admin2@example.com",
                "password": "a-very-long-test-password",
                "role": "admin",
            },
        ).status_code
        == 403
    )
    assert client.get("/api/v1/users", headers=manager_headers).status_code == 403
    assert client.get("/api/v1/jobs", headers=manager_headers).status_code == 403
    assert client.get("/api/v1/stats", headers=manager_headers).status_code == 403
    upload = client.post(
        "/api/v1/jobs",
        headers=manager_headers,
        files={"file": ("cat.png", sample_image(), "image/png")},
    )
    assert upload.status_code == 202
    job_id = upload.json()["job_id"]
    assert client.get(f"/api/v1/jobs/{job_id}", headers=manager_headers).status_code == 200
    assert client.delete(f"/api/v1/jobs/{job_id}", headers=manager_headers).status_code == 200


def test_regular_user_cannot_create_accounts_or_list_jobs(api_client):
    client, _ = api_client
    _, token = create_account(client, "person@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    assert (
        client.post(
            "/api/v1/users",
            headers=headers,
            json={
                "email": "other@example.com",
                "password": "a-very-long-test-password",
            },
        ).status_code
        == 403
    )
    assert client.get("/api/v1/jobs", headers=headers).status_code == 403
    assert client.get("/api/v1/stats", headers=headers).status_code == 403


def test_upload_status_list_cancel_and_owner_isolation(api_client):
    client, runtime = api_client
    _, alice = create_account(client, "alice@example.com")
    _, bob = create_account(client, "bob@example.com")
    headers = {"Authorization": f"Bearer {alice}"}
    image = sample_image()

    uploaded = client.post(
        "/api/v1/jobs",
        headers=headers,
        files={"file": ("cat.png", image, "image/png")},
    )
    assert uploaded.status_code == 202, uploaded.text
    job_id = uploaded.json()["job_id"]
    assert uploaded.json()["status"] == "queued"

    current = client.get(f"/api/v1/jobs/{job_id}", headers=headers)
    assert current.status_code == 200
    assert current.json()["media_kind"] == "image"
    assert (
        client.get(f"/api/v1/jobs/{job_id}", headers={"Authorization": f"Bearer {bob}"}).status_code
        == 404
    )

    assert client.get("/api/v1/jobs?kind=image&limit=5", headers=headers).status_code == 403

    cancelled = client.delete(f"/api/v1/jobs/{job_id}", headers=headers)
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"
    assert runtime.queue.depths()["ready"] == 0
    assert client.delete(f"/api/v1/jobs/{job_id}", headers=headers).status_code == 409


def test_post_job_waits_and_returns_analysis(api_client, monkeypatch):
    client, runtime = api_client
    _, token = create_account(client, "post-result@example.com")
    monkeypatch.setattr("app.api.routes.JOB_RESULT_WAIT_S", 5)
    stop = threading.Event()
    worker = threading.Thread(target=runtime.worker.run, args=(stop,), kwargs={"poll_s": 0.01})
    worker.start()
    try:
        uploaded = client.post(
            "/api/v1/jobs",
            headers={"Authorization": f"Bearer {token}"},
            files={"file": ("cat.png", sample_image(), "image/png")},
        )
    finally:
        stop.set()
        worker.join(timeout=2)

    assert not worker.is_alive()
    assert uploaded.status_code == 200
    body = uploaded.json()
    assert body["status"] == "done"
    assert "scripted analysis" in body["result"]["text"]


def test_post_job_returns_accepted_if_worker_has_not_finished(api_client, monkeypatch):
    client, _ = api_client
    _, token = create_account(client, "post-timeout@example.com")
    monkeypatch.setattr("app.api.routes.JOB_RESULT_WAIT_S", 0.01)
    uploaded = client.post(
        "/api/v1/jobs",
        headers={"Authorization": f"Bearer {token}"},
        files={"file": ("cat.png", sample_image(), "image/png")},
    )
    assert uploaded.status_code == 202
    assert uploaded.json()["status"] == "queued"
    assert uploaded.json()["result"] is None


def test_post_job_reports_worker_failure_with_error(api_client, monkeypatch):
    client, runtime = api_client
    _, token = create_account(client, "post-failed@example.com")
    runtime.worker.client = ScriptedClient([Permanent("video compression unavailable")])
    monkeypatch.setattr("app.api.routes.JOB_RESULT_WAIT_S", 5)
    stop = threading.Event()
    worker = threading.Thread(target=runtime.worker.run, args=(stop,), kwargs={"poll_s": 0.01})
    worker.start()
    try:
        uploaded = client.post(
            "/api/v1/jobs",
            headers={"Authorization": f"Bearer {token}"},
            files={"file": ("cat.png", sample_image(), "image/png")},
        )
    finally:
        stop.set()
        worker.join(timeout=2)

    assert uploaded.status_code == 502
    assert uploaded.json()["status"] == "failed"
    assert uploaded.json()["result"] is None
    assert uploaded.json()["error"] == "video compression unavailable"


def test_processing_job_cannot_be_cancelled(api_client):
    client, runtime = api_client
    _, token = create_account(client, "person@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    accepted = client.post(
        "/api/v1/jobs",
        headers=headers,
        files={"file": ("cat.png", sample_image(), "image/png")},
    )
    job_id = accepted.json()["job_id"]
    assert runtime.queue.claim() == job_id
    cancelled = client.delete(f"/api/v1/jobs/{job_id}", headers=headers)
    assert cancelled.status_code == 409


def test_identical_uploads_share_result_and_cache(api_client):
    client, runtime = api_client
    _, token = create_account(client, "person@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    image = sample_image()

    def submit():
        return client.post(
            "/api/v1/jobs",
            headers=headers,
            files={"file": ("cat.png", image, "image/png")},
        )

    first = submit()
    waiting = submit()
    assert first.status_code == waiting.status_code == 202
    assert first.json()["status"] == "queued"
    assert waiting.json()["status"] == "waiting"
    assert runtime.queue.depths()["ready"] == 1

    runtime.worker.drain()
    for submitted in (first, waiting):
        with client.app.state.session_factory() as db:
            stored_job = db.get(ApiJob, submitted.json()["job_id"])
            assert stored_job.result is not None
            assert "scripted analysis" in stored_job.result["text"]
    first_result = client.get(f"/api/v1/jobs/{first.json()['job_id']}", headers=headers).json()
    waiting_result = client.get(f"/api/v1/jobs/{waiting.json()['job_id']}", headers=headers).json()
    assert first_result["result"] == waiting_result["result"]

    cached = submit()
    assert cached.status_code == 200
    assert cached.json()["status"] == "done"
    assert cached.json()["cached"] is True


def test_video_upload_and_worker_result(api_client, make_video):
    client, runtime = api_client
    _, token = create_account(client, "person@example.com")
    headers = {"Authorization": f"Bearer {token}"}

    invalid = client.post(
        "/api/v1/jobs",
        headers=headers,
        files={"file": ("bad.png", b"not an image", "image/png")},
    )
    assert invalid.status_code == 422

    video_path = Path(make_video())
    accepted = client.post(
        "/api/v1/jobs",
        headers=headers,
        files={"file": ("clip.mp4", video_path.read_bytes(), "video/mp4")},
    )
    assert accepted.status_code == 202
    job_id = accepted.json()["job_id"]
    runtime.worker.drain()
    with client.app.state.session_factory() as db:
        stored_job = db.get(ApiJob, job_id)
        assert stored_job.result is not None
        assert "scripted analysis" in stored_job.result["text"]
    result = client.get(f"/api/v1/jobs/{job_id}", headers=headers)
    assert result.status_code == 200
    assert result.json()["status"] == "done"
    assert result.json()["media_kind"] == "video"
    assert "scripted analysis" in result.json()["result"]["text"]
    with client.app.state.session_factory() as db:
        stored_job = db.get(ApiJob, job_id)
        assert stored_job.result == result.json()["result"]

    redis_key = f"{runtime.settings.prefix}:result:{runtime.store.get(job_id).cache_key}"
    runtime.r.delete(redis_key)
    persisted_result = client.get(f"/api/v1/jobs/{job_id}", headers=headers)
    assert persisted_result.json()["status"] == "done"
    assert persisted_result.json()["result"] == result.json()["result"]


def test_video_upload_limit_is_separate_from_analysis_limit(api_client):
    client, runtime = api_client
    _, token = create_account(client, "large-video@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    runtime.settings = replace(
        runtime.settings,
        max_video_mb=1,
        max_video_upload_mb=2,
    )
    runtime.store.max_video_mb = 2
    video = b"\x00\x00\x00\x18ftypmp42" + b"x" * (1024 * 1024)

    response = client.post(
        "/api/v1/jobs",
        headers=headers,
        files={"file": ("large.mp4", video, "video/mp4")},
    )
    assert response.status_code == 202, response.text
    assert response.json()["status"] == "queued"


def test_manager_review_lists_and_approval_actions(api_client, make_image):
    client, runtime = api_client
    _, manager = create_account(client, "review-manager@example.com", role="manager")
    _, user = create_account(client, "review-user@example.com")
    manager_headers = {"Authorization": f"Bearer {manager}"}
    user_headers = {"Authorization": f"Bearer {user}"}
    image_path = Path(make_image())

    def submit(headers, path=image_path):
        response = client.post(
            "/api/v1/jobs",
            headers=headers,
            files={"file": (path.name, path.read_bytes(), "image/png")},
        )
        assert response.status_code in {200, 202}
        runtime.worker.drain()
        return response.json()["job_id"]

    approved_id = submit(user_headers)
    review_list = client.get("/api/v1/jobs/review", headers=manager_headers)
    assert review_list.status_code == 200
    review_item = next(
        item for item in review_list.json()["items"] if item["job_id"] == approved_id
    )
    assert review_item["approval_status"] == "pending"
    assert review_item["submitter_email"] == "review-user@example.com"
    assert "scripted analysis" in review_item["result"]["text"]

    media_response = client.get(review_item["media_url"], headers=manager_headers)
    assert media_response.status_code == 200
    assert media_response.headers["content-type"] == "image/png"

    approved = client.patch(
        f"/api/v1/jobs/{approved_id}/approval",
        headers=manager_headers,
        json={"status": "approved"},
    )
    assert approved.status_code == 200
    assert approved.json()["approval_status"] == "approved"
    assert approved.json()["submitter_email"] == "review-user@example.com"
    assert approved_id not in {
        item["job_id"]
        for item in client.get("/api/v1/jobs/review", headers=manager_headers).json()["items"]
    }
    assert approved_id in {
        item["job_id"]
        for item in client.get("/api/v1/jobs/approved", headers=manager_headers).json()["items"]
    }

    rejected_id = submit(user_headers, Path(make_image()))
    rejected = client.patch(
        f"/api/v1/jobs/{rejected_id}/approval",
        headers=manager_headers,
        json={"status": "rejected"},
    )
    assert rejected.status_code == 200
    assert rejected.json()["approval_status"] == "rejected"
    assert rejected_id in {
        item["job_id"]
        for item in client.get("/api/v1/jobs/rejected", headers=manager_headers).json()["items"]
    }
    assert client.get("/api/v1/jobs/review", headers=user_headers).status_code == 403
    assert (
        client.patch(
            f"/api/v1/jobs/{approved_id}/approval",
            headers=user_headers,
            json={"status": "rejected"},
        ).status_code
        == 403
    )
    assert (
        client.patch(
            f"/api/v1/jobs/{approved_id}/approval",
            headers=manager_headers,
            json={"status": "rejected"},
        ).status_code
        == 409
    )


def test_uploads_reuse_identical_media_and_preserve_original_names(api_client, make_image):
    client, runtime = api_client
    _, token = create_account(client, "dedup-media@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    first_path = Path(make_image(name="first.png", color=(20, 40, 60)))
    duplicate_path = Path(make_image(name="duplicate.png", color=(20, 40, 60)))
    media_dir = client.app.state.api_settings.media_storage_dir

    def submit(filename, content):
        response = client.post(
            "/api/v1/jobs",
            headers=headers,
            files={"file": (filename, content, "image/png")},
        )
        assert response.status_code in {200, 202}
        return response.json()["job_id"]

    first_id = submit("campaign.png", first_path.read_bytes())
    duplicate_id = submit("renamed-copy.png", duplicate_path.read_bytes())
    first_job = client.get(f"/api/v1/jobs/{first_id}", headers=headers)
    duplicate_job = client.get(f"/api/v1/jobs/{duplicate_id}", headers=headers)

    assert first_job.json()["original_filename"] == "campaign.png"
    assert duplicate_job.json()["original_filename"] == "renamed-copy.png"
    assert len(list(media_dir.iterdir())) == 1
    stored_name = next(media_dir.iterdir()).name
    assert stored_name == "campaign.png"
    assert client.get(first_job.json()["media_url"], headers=headers).content == (
        client.get(duplicate_job.json()["media_url"], headers=headers).content
    )
    assert runtime.store.get(first_id).path == runtime.store.get(duplicate_id).path


def test_same_original_name_with_different_content_is_not_overwritten(api_client, make_image):
    client, _ = api_client
    _, token = create_account(client, "filename-collision@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    first = Path(make_image(color=(1, 2, 3))).read_bytes()
    second = Path(make_image(color=(4, 5, 6))).read_bytes()
    media_dir = client.app.state.api_settings.media_storage_dir

    job_ids = []
    for content in (first, second):
        response = client.post(
            "/api/v1/jobs",
            headers=headers,
            files={"file": ("same-name.png", content, "image/png")},
        )
        assert response.status_code in {200, 202}
        job_ids.append(response.json()["job_id"])

    filenames = {path.name for path in media_dir.iterdir()}
    assert "same-name.png" in filenames
    assert len(filenames) == 2
    assert any(name.startswith("same-name-") and name.endswith(".png") for name in filenames)
    assert all(
        client.get(f"/api/v1/jobs/{job_id}", headers=headers).json()["original_filename"]
        == "same-name.png"
        for job_id in job_ids
    )


def test_health_and_stats(api_client):
    client, _ = api_client
    admin_id, admin = create_account(client, "admin@example.com", role="admin")
    user_id, _ = create_account(client, "user@example.com", role="user")
    created = client.post(
        "/api/v1/users",
        headers={"Authorization": f"Bearer {admin}"},
        json={
            "email": "manager@example.com",
            "password": "a-very-long-test-password",
            "role": "manager",
        },
    )
    assert created.status_code == 201
    assert created.json()["role"] == "manager"
    admin_headers = {"Authorization": f"Bearer {admin}"}
    assert client.get("/api/v1/users", headers=admin_headers).status_code == 200
    assert (
        client.patch(
            f"/api/v1/users/{user_id}/role",
            headers=admin_headers,
            json={"role": "manager"},
        ).json()["role"]
        == "manager"
    )
    assert (
        client.patch(
            f"/api/v1/users/{admin_id}/role",
            headers=admin_headers,
            json={"role": "user"},
        ).status_code
        == 409
    )

    uploaded = client.post(
        "/api/v1/jobs",
        headers=admin_headers,
        files={"file": ("cat.png", sample_image(), "image/png")},
    )
    job_id = uploaded.json()["job_id"]
    _, outsider = create_account(client, "different@example.com")
    assert (
        client.get(
            f"/api/v1/jobs/{job_id}",
            headers={"Authorization": f"Bearer {outsider}"},
        ).status_code
        == 404
    )
    admin_list = client.get("/api/v1/jobs", headers=admin_headers)
    assert [item["job_id"] for item in admin_list.json()["items"]] == [job_id]
    assert client.delete(f"/api/v1/jobs/{job_id}", headers=admin_headers).status_code == 200

    assert client.get("/health/live").json() == {"status": "alive"}
    assert client.get("/health/ready").json() == {"status": "ready"}
    assert client.get("/api/v1/stats", headers=admin_headers).status_code == 200
