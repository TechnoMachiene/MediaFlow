"""Media retention policy and the S3-compatible storage backend."""

import os
import time
from datetime import datetime, timedelta, timezone

import boto3
import pytest
from fastapi.testclient import TestClient
from moto import mock_aws
from sqlalchemy import update

from app.api.database import ApiJob, MediaAsset, ScheduledPost
from app.api.retention import prune_media
from app.storage import S3Storage
from tests.api_harness import add_member, build_app, login, png, upload
from tests.helpers import ScriptedClient

pytestmark = pytest.mark.integration


@pytest.fixture
def app_env(make_rt, tmp_path, monkeypatch):
    monkeypatch.setattr("app.api.routes.JOB_RESULT_WAIT_S", 0)
    gemini = ScriptedClient()
    runtime = make_rt(client=gemini)
    application, engine, sessions = build_app(runtime, tmp_path / "media")
    add_member(sessions, "owner@example.com")
    with TestClient(application) as client:
        yield client, runtime, sessions, gemini, login(client, "owner@example.com")
    engine.dispose()


def age_everything(sessions, days):
    past = datetime.now(timezone.utc) - timedelta(days=days)
    with sessions.begin() as db:
        db.execute(update(MediaAsset).values(last_used_at=past, created_at=past))


def test_idle_original_is_purged_drafts_survive_and_reupload_restores_it(app_env):
    client, runtime, sessions, gemini, owner = app_env
    image = png((50, 60, 70))
    job_id = upload(client, owner, image).json()["job_id"]
    runtime.worker.drain()
    storage = client.app.state.storage
    age_everything(sessions, 31)

    assert prune_media(sessions, storage, retention_days=30)["purged"] == 1
    with sessions() as db:
        asset = db.query(MediaAsset).one()
        assert asset.purged_at is not None
        assert not storage.exists(asset.stored_filename)
    media = client.get(f"/api/v1/jobs/{job_id}/media", headers=owner)
    assert media.status_code == 410
    job = client.get(f"/api/v1/jobs/{job_id}", headers=owner).json()
    assert job["status"] == "done" and job["result"]["text"]  # the draft is kept

    # Same bytes uploaded again: stored again, both jobs can show the media.
    again = upload(client, owner, image).json()["job_id"]
    assert client.get(f"/api/v1/jobs/{job_id}/media", headers=owner).content == image
    assert client.get(f"/api/v1/jobs/{again}/media", headers=owner).content == image
    assert prune_media(sessions, storage, retention_days=30)["purged"] == 0  # fresh again


def test_retention_keeps_recent_active_and_upcoming_media(app_env):
    client, runtime, sessions, gemini, owner = app_env
    storage = client.app.state.storage
    recent = upload(client, owner, png((1, 1, 1))).json()["job_id"]
    runtime.worker.drain()
    assert prune_media(sessions, storage, retention_days=30)["purged"] == 0

    queued = upload(client, owner, png((2, 2, 2))).json()["job_id"]  # never processed
    scheduled = upload(client, owner, png((3, 3, 3))).json()["job_id"]
    runtime.worker.drain()  # processes both
    with sessions.begin() as db:
        db.execute(update(ApiJob).where(ApiJob.id == queued).values(status="queued"))
        job = db.get(ApiJob, scheduled)
        db.add(ScheduledPost(
            job_id=job.id, workspace_id=job.workspace_id, owner_id=job.owner_id,
            scheduled_at=datetime.now(timezone.utc) + timedelta(days=3), timezone="UTC",
        ))
    age_everything(sessions, 90)

    out = prune_media(sessions, storage, retention_days=30)
    assert out == {"purged": 1, "kept": 2, "staged_removed": 0}  # only `recent` (now idle)
    for job_id, expected in ((recent, 410), (queued, 200), (scheduled, 200)):
        response = client.get(f"/api/v1/jobs/{job_id}/media", headers=owner)
        assert response.status_code == expected, job_id


def test_retention_zero_disables_purging_but_still_clears_abandoned_partials(app_env):
    client, runtime, sessions, _, owner = app_env
    storage = client.app.state.storage
    upload(client, owner, png())
    age_everything(sessions, 3650)
    leftover = storage.directory / ".upload-crashed.png"
    leftover.write_bytes(b"partial")
    fresh = storage.directory / ".upload-in-progress.png"
    fresh.write_bytes(b"partial")
    two_days_ago = time.time() - 2 * 86400
    os.utime(leftover, (two_days_ago, two_days_ago))

    assert prune_media(sessions, storage, retention_days=0) == {
        "purged": 0, "kept": 0, "staged_removed": 1
    }
    assert not leftover.exists() and fresh.exists()


def test_upload_of_a_missing_file_restores_it(app_env):
    client, runtime, sessions, _, owner = app_env
    image = png((8, 8, 8))
    first = upload(client, owner, image).json()["job_id"]
    storage = client.app.state.storage
    with sessions() as db:
        storage.delete(db.query(MediaAsset).one().stored_filename)  # lost behind our back
    assert client.get(f"/api/v1/jobs/{first}/media", headers=owner).status_code == 404
    upload(client, owner, image)
    assert client.get(f"/api/v1/jobs/{first}/media", headers=owner).content == image


# ── S3 backend ────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def s3():
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket="media")
        yield client


def test_s3_storage_round_trip(s3, tmp_path):
    storage = S3Storage("media", prefix="/originals/", client=s3)
    staged = tmp_path / "staged.png"
    staged.write_bytes(png())
    key = storage.save(staged, "Holiday Photo.PNG", "ab" * 32)
    assert key == f"originals/{'ab' * 32}.png"  # content-addressed
    assert storage.exists(key) and not storage.exists("originals/missing.png")
    assert b"".join(storage.read_chunks(key)) == png()
    assert storage.local_path(key) is None

    ref = storage.ref(key)
    assert ref == f"s3://media/{key}"
    with storage.materialize(ref) as local:
        assert local.endswith(".png") and open(local, "rb").read() == png()
    assert not os.path.exists(local)  # temp copy cleaned up
    with storage.materialize("/legacy/local/path.png") as local:
        assert local == "/legacy/local/path.png"

    storage.delete(key)
    assert not storage.exists(key)


def test_api_and_worker_run_entirely_on_s3(s3, make_rt, tmp_path, monkeypatch):
    monkeypatch.setattr("app.api.routes.JOB_RESULT_WAIT_S", 0)
    gemini = ScriptedClient()
    runtime = make_rt(client=gemini)
    storage = S3Storage("media", client=s3)
    application, engine, sessions = build_app(runtime, tmp_path / "staging", storage=storage)
    runtime.worker.materialize = storage.materialize
    add_member(sessions, "owner@example.com")
    seen = []
    real = gemini.analyse
    gemini.analyse = lambda spec: (seen.append(os.path.exists(spec.path)), real(spec))[1]

    with TestClient(application) as client:
        owner = login(client, "owner@example.com")
        image = png((30, 40, 50))
        job_id = upload(client, owner, image).json()["job_id"]
        assert runtime.store.get(job_id).path.startswith("s3://media/")
        # Nothing but the (removed) staging file ever touched local disk.
        assert list((tmp_path / "staging").iterdir()) == []
        runtime.worker.drain()
        assert seen == [True]  # the worker got a real local file for Gemini
        assert client.get(f"/api/v1/jobs/{job_id}", headers=owner).json()["status"] == "done"
        response = client.get(f"/api/v1/jobs/{job_id}/media", headers=owner)
        assert response.status_code == 200 and response.content == image

        with sessions() as db:
            key = db.query(MediaAsset).one().stored_filename
        age_everything(sessions, 40)
        assert prune_media(sessions, storage, retention_days=30)["purged"] == 1
        assert not storage.exists(key)
        assert client.get(f"/api/v1/jobs/{job_id}/media", headers=owner).status_code == 410
    engine.dispose()
