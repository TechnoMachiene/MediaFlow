from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.config import ApiSettings
from app.api.database import ApiJob, Base, ScheduledPost, User
from app.api.main import create_app
from app.api.security import hash_password

pytestmark = pytest.mark.unit


class RuntimeStore:
    statuses = {}

    def status(self, _job_id):
        return self.statuses.get(_job_id, {"status": "done"})

    def cancel(self, _job_id):
        return True


@pytest.fixture
def scheduling_api(tmp_path):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    cfg = ApiSettings(
        database_url="sqlite://",
        jwt_secret="api-test-secret-" + "x" * 40,
        token_ttl_minutes=30,
        media_storage_dir=Path(tmp_path),
    )
    with sessions.begin() as db:
        db.add_all(
            [
                User(
                    id="schedule-owner",
                    email="schedule-owner@example.com",
                    password_hash=hash_password("very-long-test-password"),
                    role="user",
                ),
                User(
                    id="other-owner",
                    email="other-owner@example.com",
                    password_hash=hash_password("very-long-test-password"),
                    role="user",
                ),
                User(
                    id="schedule-admin",
                    email="schedule-admin@example.com",
                    password_hash=hash_password("very-long-test-password"),
                    role="admin",
                ),
            ]
        )
        for job_id, owner_id, approval_status in (
            ("schedule-job-1", "schedule-owner", "approved"),
            ("pending-job", "schedule-owner", "pending"),
            ("other-owner-job", "other-owner", "approved"),
            ("queued-job", "schedule-owner", "pending"),
        ):
            db.add(
                ApiJob(
                    id=job_id,
                    owner_id=owner_id,
                    filename=f"{job_id}.png",
                    media_kind="image",
                    status="done",
                    approval_status=approval_status,
                    result=(
                        None
                        if job_id == "queued-job"
                        else {
                            "text": f"Analysis for {job_id}",
                            "kind": "image",
                            "model": "test-model",
                        }
                    ),
                )
            )
    runtime_store = RuntimeStore()
    runtime_store.statuses = {"queued-job": {"status": "queued"}}
    application = create_app(
        api_settings=cfg,
        runtime=SimpleNamespace(store=runtime_store),
        engine=engine,
        session_factory=sessions,
    )
    try:
        with TestClient(application) as client:
            headers = {}
            for user, email in (
                ("owner", "schedule-owner@example.com"),
                ("other", "other-owner@example.com"),
                ("admin", "schedule-admin@example.com"),
            ):
                response = client.post(
                    "/api/v1/auth/login",
                    json={"email": email, "password": "very-long-test-password"},
                )
                assert response.status_code == 200
                headers[user] = {
                    "Authorization": "Bearer " + response.json()["access_token"]
                }
            yield client, sessions, headers
    finally:
        engine.dispose()


def schedule(client, headers, job_id="schedule-job-1", **overrides):
    payload = {
        "scheduled_at": (datetime.now(timezone.utc) + timedelta(days=2)).isoformat(),
        "timezone": "UTC",
    }
    payload.update(overrides)
    return client.post(
        f"/api/v1/jobs/{job_id}/schedule",
        headers=headers,
        json=payload,
    )


def test_schedule_and_calendar_need_no_platform_accounts(scheduling_api):
    client, _, headers = scheduling_api
    scheduled_at = datetime.now(timezone.utc) + timedelta(days=2)
    created = schedule(
        client,
        headers["owner"],
        scheduled_at=scheduled_at.isoformat(),
        timezone="Asia/Karachi",
    )
    assert created.status_code == 201, created.text
    post = created.json()
    assert post["status"] == "scheduled"
    assert post["original_filename"] == "schedule-job-1.png"
    assert post["content"] == "Analysis for schedule-job-1"
    assert post["timezone"] == "Asia/Karachi"

    local_day = post["local_date"]
    calendar = client.get(
        "/api/v1/calendar",
        headers=headers["owner"],
        params={
            "start": local_day,
            "end": local_day,
            "timezone": "Asia/Karachi",
        },
    )
    assert calendar.status_code == 200, calendar.text
    day = next(item for item in calendar.json()["days"] if item["posts"])
    assert day["posts"][0]["id"] == post["id"]

    listed = client.get(
        "/api/v1/scheduled-posts",
        headers=headers["owner"],
        params={
            "start": local_day,
            "end": local_day,
            "timezone": "Asia/Karachi",
        },
    )
    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()["items"]] == [post["id"]]


def test_schedule_checks_approval_owner_future_time_and_duplicates(scheduling_api):
    client, _, headers = scheduling_api
    assert schedule(client, headers["owner"], "pending-job").status_code == 409
    assert schedule(client, headers["other"], "schedule-job-1").status_code == 404
    assert (
        schedule(
            client,
            headers["owner"],
            scheduled_at=(datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(),
        ).status_code
        == 422
    )
    assert (
        schedule(
            client,
            headers["owner"],
            scheduled_at=(datetime.now(timezone.utc) + timedelta(days=2))
            .replace(tzinfo=None)
            .isoformat(),
        ).status_code
        == 422
    )
    assert (
        schedule(
            client,
            headers["owner"],
            timezone="Invalid/Timezone",
        ).status_code
        == 422
    )
    assert schedule(client, headers["owner"]).status_code == 201
    assert schedule(client, headers["owner"]).status_code == 409


def test_users_can_list_only_their_jobs_with_job_and_approval_statuses(scheduling_api):
    client, _, headers = scheduling_api

    owner_jobs = client.get("/api/v1/jobs/mine", headers=headers["owner"])
    assert owner_jobs.status_code == 200
    owner_items = owner_jobs.json()["items"]
    assert {item["job_id"] for item in owner_items} == {
        "schedule-job-1",
        "pending-job",
        "queued-job",
    }
    by_id = {item["job_id"]: item for item in owner_items}
    assert by_id["schedule-job-1"]["status"] == "done"
    assert by_id["schedule-job-1"]["approval_status"] == "approved"
    assert by_id["pending-job"]["approval_status"] == "pending"
    assert by_id["queued-job"]["status"] == "queued"

    other_jobs = client.get("/api/v1/jobs/mine", headers=headers["other"])
    assert other_jobs.status_code == 200
    assert [item["job_id"] for item in other_jobs.json()["items"]] == ["other-owner-job"]

    paginated = client.get(
        "/api/v1/jobs/mine",
        headers=headers["owner"],
        params={"limit": 1, "offset": 1},
    )
    assert paginated.status_code == 200
    assert len(paginated.json()["items"]) == 1
    assert paginated.json()["limit"] == 1
    assert paginated.json()["offset"] == 1


def test_reschedule_cancel_and_calendar_visibility(scheduling_api):
    client, sessions, headers = scheduling_api
    created = schedule(client, headers["owner"])
    assert created.status_code == 201, created.text
    post_id = created.json()["id"]

    other_owner = client.get(
        f"/api/v1/scheduled-posts/{post_id}",
        headers=headers["other"],
    )
    assert other_owner.status_code == 404
    admin_view = client.get(
        f"/api/v1/scheduled-posts/{post_id}",
        headers=headers["admin"],
    )
    assert admin_view.status_code == 200

    rescheduled_at = datetime.now(timezone.utc) + timedelta(days=4)
    rescheduled = client.patch(
        f"/api/v1/scheduled-posts/{post_id}",
        headers=headers["owner"],
        json={
            "scheduled_at": rescheduled_at.isoformat(),
            "timezone": "America/Los_Angeles",
        },
    )
    assert rescheduled.status_code == 200, rescheduled.text
    assert rescheduled.json()["status"] == "scheduled"
    assert rescheduled.json()["timezone"] == "America/Los_Angeles"

    cancelled = client.delete(
        f"/api/v1/scheduled-posts/{post_id}",
        headers=headers["owner"],
    )
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"
    with sessions() as db:
        assert db.get(ScheduledPost, post_id).status == "cancelled"


def test_calendar_rejects_bad_timezone_and_ranges(scheduling_api):
    client, _, headers = scheduling_api
    bad_timezone = client.get(
        "/api/v1/calendar",
        headers=headers["owner"],
        params={"start": "2026-01-01", "end": "2026-01-02", "timezone": "Bad/Zone"},
    )
    assert bad_timezone.status_code == 422
    oversized = client.get(
        "/api/v1/calendar",
        headers=headers["owner"],
        params={"start": "2026-01-01", "end": "2026-04-15", "timezone": "UTC"},
    )
    assert oversized.status_code == 422


def test_social_platform_endpoints_are_not_registered(scheduling_api):
    client, _, headers = scheduling_api
    response = client.get("/api/v1/platform-accounts", headers=headers["admin"])
    assert response.status_code == 404
