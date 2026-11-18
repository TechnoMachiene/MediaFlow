"""Object storage, expiring media links and per-workspace deletion policies.
See docs/STORAGE.md; each guarantee there has a test here."""

import json
import os
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

import boto3
import pytest
from fastapi.testclient import TestClient
from moto import mock_aws
from sqlalchemy import select, update

from app.api.database import (
    ApiJob,
    AuditEvent,
    DraftComment,
    MediaAsset,
    Publication,
    ScheduledPost,
    Workspace,
)
from app.api.retention import apply_retention, collect, prune_media
from app.storage import LocalStorage, S3Storage
from tests.api_harness import add_member, build_app, login, png, send_for_review, upload
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


@pytest.fixture
def team(make_rt, tmp_path, monkeypatch):
    """Two workspaces, so policies and shared bytes can be compared."""
    monkeypatch.setattr("app.api.routes.JOB_RESULT_WAIT_S", 0)
    runtime = make_rt(client=ScriptedClient())
    application, engine, sessions = build_app(runtime, tmp_path / "media")
    for email, role, ws in (("anna@a.example.com", "admin", "ws-a"),
                            ("alice@a.example.com", "user", "ws-a"),
                            ("ada@a.example.com", "user", "ws-a"),
                            ("max@a.example.com", "manager", "ws-a"),
                            ("bea@b.example.com", "admin", "ws-b")):
        add_member(sessions, email, role, ws)
    with TestClient(application) as client:
        h = {e.split("@")[0]: login(client, e) for e in (
            "anna@a.example.com", "alice@a.example.com", "ada@a.example.com",
            "max@a.example.com", "bea@b.example.com")}
        yield client, runtime, sessions, h
    engine.dispose()


def age(sessions, days, job_id=None):
    """Make jobs (and their stored objects) look `days` old."""
    past = datetime.now(timezone.utc) - timedelta(days=days)
    with sessions.begin() as db:
        jobs = update(ApiJob).values(created_at=past)
        if job_id:
            jobs = jobs.where(ApiJob.id == job_id)
        db.execute(jobs)
        db.execute(update(MediaAsset).values(last_used_at=past, created_at=past))


def run(client, sessions, **kw):
    kw.setdefault("default_days", 30)
    return apply_retention(sessions, client.app.state.storage, **kw)


def drafted(client, runtime, headers, color=(1, 2, 3)):
    job_id = upload(client, headers, png(color)).json()["job_id"]
    runtime.worker.drain()
    return job_id


def media(client, headers, job_id):
    return client.get(f"/api/v1/jobs/{job_id}/media", headers=headers)


# ── Deletion policies ─────────────────────────────────────────────────────────────────────

def test_an_old_original_is_deleted_drafts_survive_and_it_is_audited(app_env):
    client, runtime, sessions, _, owner = app_env
    job_id = drafted(client, runtime, owner)
    storage = client.app.state.storage
    age(sessions, 31)

    out = run(client, sessions)
    assert (out["originals_expired"], out["objects_purged"]) == (1, 1)
    with sessions() as db:
        asset = db.query(MediaAsset).one()
        assert asset.purged_at is not None and not storage.exists(asset.stored_filename)
        event = db.scalars(select(AuditEvent).where(AuditEvent.action == "media.deleted")).one()
    assert (event.actor_email, event.after) == ("system:retention", {"reason": "policy",
                                                                     "days": 30})
    gone = media(client, owner, job_id)
    assert gone.status_code == 410 and "retention policy" in gone.json()["detail"]
    job = client.get(f"/api/v1/jobs/{job_id}", headers=owner).json()
    assert job["status"] == "done" and job["result"]["text"]  # the draft is kept


def test_uploading_the_same_bytes_again_serves_only_the_new_job(app_env):
    """A deletion is a deletion: old jobs stay without their original."""
    client, runtime, sessions, _, owner = app_env
    image = png((50, 60, 70))
    old = drafted(client, runtime, owner, (50, 60, 70))
    age(sessions, 31)
    run(client, sessions)
    new = upload(client, owner, image).json()["job_id"]
    assert media(client, owner, new).content == image
    assert media(client, owner, old).status_code == 410
    assert run(client, sessions)["objects_purged"] == 0  # the new job keeps the bytes


def test_recent_active_upcoming_and_publishing_originals_are_kept(app_env):
    client, runtime, sessions, _, owner = app_env
    recent = drafted(client, runtime, owner, (1, 1, 1))
    queued = drafted(client, runtime, owner, (2, 2, 2))
    upcoming = drafted(client, runtime, owner, (3, 3, 3))
    lately = drafted(client, runtime, owner, (4, 4, 4))
    old = drafted(client, runtime, owner, (5, 5, 5))
    now = datetime.now(timezone.utc)
    with sessions.begin() as db:
        db.execute(update(ApiJob).where(ApiJob.id == queued).values(status="queued"))
        for job_id, when in ((upcoming, now + timedelta(days=3)),  # still ahead
                             (lately, now - timedelta(days=10))):  # date passed 10 days ago
            job = db.get(ApiJob, job_id)
            db.add(ScheduledPost(job_id=job.id, workspace_id=job.workspace_id,
                                 owner_id=job.owner_id, scheduled_at=when, timezone="UTC"))
    for job_id in (queued, upcoming, lately, old):
        age(sessions, 90, job_id)

    assert run(client, sessions)["originals_expired"] == 1  # only `old`
    for job_id, expected in ((recent, 200), (queued, 200), (upcoming, 200), (lately, 200),
                             (old, 410)):
        assert media(client, owner, job_id).status_code == expected, job_id


def test_zero_keeps_originals_but_still_clears_abandoned_partials(app_env):
    client, runtime, sessions, _, owner = app_env
    storage = client.app.state.storage
    drafted(client, runtime, owner)
    age(sessions, 3650)
    leftover = storage.directory / ".upload-crashed.png"
    leftover.write_bytes(b"partial")
    fresh = storage.directory / ".upload-in-progress.png"
    fresh.write_bytes(b"partial")
    two_days_ago = time.time() - 2 * 86400
    os.utime(leftover, (two_days_ago, two_days_ago))

    out = prune_media(sessions, storage, retention_days=0)
    assert out["staged_removed"] == 1 and out["originals_expired"] == 0
    assert not leftover.exists() and fresh.exists()


def test_each_workspace_applies_its_own_policy(team):
    client, runtime, sessions, h = team
    a = drafted(client, runtime, h["alice"], (1, 0, 0))
    b = drafted(client, runtime, h["bea"], (2, 0, 0))
    assert client.put("/api/v1/workspaces/ws-a/retention", headers=h["anna"],
                      json={"originals_days": 7}).status_code == 200
    age(sessions, 10)
    assert run(client, sessions)["originals_expired"] == 1
    assert media(client, h["alice"], a).status_code == 410
    assert media(client, h["bea"], b).status_code == 200  # B still on the 30-day default


def test_shared_bytes_stay_until_the_last_job_lets_go(team):
    """Same file in two tenants: A's policy ends A's access, but B's copy is untouched, and
    the object goes only when B lets go too."""
    client, runtime, sessions, h = team
    a = drafted(client, runtime, h["alice"], (9, 9, 9))
    b = drafted(client, runtime, h["bea"], (9, 9, 9))
    client.put("/api/v1/workspaces/ws-a/retention", headers=h["anna"], json={"originals_days": 7})
    client.put("/api/v1/workspaces/ws-b/retention", headers=h["bea"], json={"originals_days": 0})
    age(sessions, 10)

    out = run(client, sessions)
    assert (out["originals_expired"], out["objects_purged"]) == (1, 0)
    assert media(client, h["alice"], a).status_code == 410
    assert media(client, h["bea"], b).content == png((9, 9, 9))

    assert client.delete(f"/api/v1/jobs/{b}/media", headers=h["bea"]).status_code == 204
    with sessions() as db:
        asset = db.query(MediaAsset).one()
    assert asset.purged_at is not None
    assert not client.app.state.storage.exists(asset.stored_filename)


def test_originals_can_go_as_soon_as_everything_published(team):
    client, runtime, sessions, h = team
    client.put("/api/v1/workspaces/ws-a/retention", headers=h["anna"],
               json={"delete_after_publish": True})
    done = drafted(client, runtime, h["alice"], (1, 1, 0))
    pending = drafted(client, runtime, h["alice"], (2, 2, 0))
    with sessions.begin() as db:
        for job_id, state in ((done, "published"), (pending, "retrying")):
            job = db.get(ApiJob, job_id)
            post = ScheduledPost(job_id=job.id, workspace_id="ws-a", owner_id=job.owner_id,
                                 scheduled_at=datetime.now(timezone.utc) - timedelta(hours=1),
                                 timezone="UTC")
            db.add(post)
            db.flush()
            db.add(Publication(workspace_id="ws-a", scheduled_post_id=post.id,
                               connection_id=_connection(db), status=state,
                               requested_by_email="alice@a.example.com"))
    assert run(client, sessions)["originals_published"] == 1
    gone = media(client, h["alice"], done)
    assert gone.status_code == 410 and "after publishing" in gone.json()["detail"]
    assert media(client, h["alice"], pending).status_code == 200


def _connection(db) -> str:
    from app.api.database import PublishingConnection

    existing = db.scalar(select(PublishingConnection.id))
    if existing:
        return existing
    conn = PublishingConnection(workspace_id="ws-a", provider="mock", channel="instagram",
                                account_id="sbx_1", account_name="Sandbox", status="active",
                                options={}, connected_by_email="anna@a.example.com")
    db.add(conn)
    db.flush()
    return conn.id


def test_rejected_drafts_can_be_deleted_entirely_after_a_while(team):
    client, runtime, sessions, h = team
    client.put("/api/v1/workspaces/ws-a/retention", headers=h["anna"],
               json={"rejected_days": 14})
    old = drafted(client, runtime, h["alice"], (1, 0, 1))
    recent = drafted(client, runtime, h["alice"], (2, 0, 1))
    approved = drafted(client, runtime, h["alice"], (3, 0, 1))
    for job_id, decision in ((old, "rejected"), (recent, "rejected"), (approved, "approved")):
        send_for_review(client, h["alice"], job_id)
        client.patch(f"/api/v1/jobs/{job_id}/approval", headers=h["max"],
                     json={"status": decision, "comment": "notes", "checked": True})
    with sessions.begin() as db:
        db.execute(update(ApiJob).where(ApiJob.id.in_([old, approved])).values(
            reviewed_at=datetime.now(timezone.utc) - timedelta(days=20)))

    preview = client.get("/api/v1/workspaces/ws-a/retention", headers=h["anna"]).json()
    assert preview["preview"]["rejected_deleted"] == 1
    assert run(client, sessions)["rejected_deleted"] == 1
    assert client.get(f"/api/v1/jobs/{old}", headers=h["alice"]).status_code == 404
    for job_id in (recent, approved):
        assert client.get(f"/api/v1/jobs/{job_id}", headers=h["alice"]).status_code == 200
    with sessions() as db:
        assert {c.job_id for c in db.scalars(select(DraftComment))} == {recent, approved}
        purged = db.scalars(select(AuditEvent).where(AuditEvent.action == "job.purged")).one()
    assert purged.target_id == old and purged.actor_email == "system:retention"


def test_the_preview_counts_without_deleting(team):
    client, runtime, sessions, h = team
    job_id = drafted(client, runtime, h["alice"])
    age(sessions, 40)
    view = client.get("/api/v1/workspaces/ws-a/retention", headers=h["anna"]).json()
    assert view["preview"] == {"originals_expired": 1, "originals_published": 0,
                               "rejected_deleted": 0}
    assert media(client, h["alice"], job_id).status_code == 200  # nothing happened
    assert run(client, sessions)["originals_expired"] == 1


def test_an_unsaved_policy_can_be_previewed(team):
    client, runtime, sessions, h = team
    job_id = drafted(client, runtime, h["alice"])
    age(sessions, 10)
    url = "/api/v1/workspaces/ws-a/retention"
    assert client.get(url, headers=h["anna"]).json()["preview"]["originals_expired"] == 0
    what_if = client.post(f"{url}/preview", headers=h["anna"], json={"originals_days": 7})
    assert what_if.json() == {"originals_expired": 1, "originals_published": 0,
                              "rejected_deleted": 0}
    assert media(client, h["alice"], job_id).status_code == 200  # nothing was saved or deleted
    assert client.get(url, headers=h["anna"]).json()["originals_days"] is None
    assert client.post(f"{url}/preview", headers=h["bea"], json={}).status_code == 404


def test_fresh_uploads_are_never_collected(app_env):
    """The bytes are stored a moment before the job row that uses them exists."""
    client, runtime, sessions, _, owner = app_env
    drafted(client, runtime, owner)
    with sessions.begin() as db:
        db.execute(update(ApiJob).values(media_deleted_at=datetime.now(timezone.utc)))
    assert collect(sessions, client.app.state.storage) == 0  # uploaded just now
    with sessions.begin() as db:
        db.execute(update(MediaAsset).values(
            last_used_at=datetime.now(timezone.utc) - timedelta(hours=2)))
    assert collect(sessions, client.app.state.storage) == 1


def test_upload_of_a_missing_file_restores_it(app_env):
    client, runtime, sessions, _, owner = app_env
    image = png((8, 8, 8))
    first = upload(client, owner, image).json()["job_id"]
    storage = client.app.state.storage
    with sessions() as db:
        storage.delete(db.query(MediaAsset).one().stored_filename)  # lost behind our back
    assert media(client, owner, first).status_code == 404
    upload(client, owner, image)
    assert media(client, owner, first).content == image


# ── Policy settings ───────────────────────────────────────────────────────────────────────

def test_admins_set_the_policy_and_changes_are_audited(team):
    client, runtime, sessions, h = team
    url = "/api/v1/workspaces/ws-a/retention"
    view = client.get(url, headers=h["anna"]).json()
    assert view == {**view, "originals_days": None, "effective_originals_days": 30,
                    "default_days": 30, "max_days": 0, "delete_after_publish": False,
                    "rejected_days": None}
    assert client.get(url, headers=h["max"]).status_code == 403
    assert client.get(url, headers=h["bea"]).status_code == 404

    policy = {"originals_days": 0, "delete_after_publish": True, "rejected_days": 30}
    assert client.put(url, headers=h["anna"], json=policy).json()["effective_originals_days"] == 0
    client.put(url, headers=h["anna"], json=policy)  # no change: not audited twice
    with sessions() as db:
        [event] = db.scalars(select(AuditEvent).where(
            AuditEvent.action == "workspace.retention_changed")).all()
    assert event.before["originals_days"] is None and event.after == policy
    assert client.put(url, headers=h["anna"], json={"rejected_days": 0}).status_code == 422


def test_the_operator_ceiling_wins(make_rt, tmp_path):
    runtime = make_rt(client=ScriptedClient())
    application, engine, sessions = build_app(runtime, tmp_path / "media",
                                              media_retention_max_days=60)
    add_member(sessions, "anna@a.example.com", "admin", "ws-a")
    with TestClient(application) as client:
        anna = login(client, "anna@a.example.com")
        url = "/api/v1/workspaces/ws-a/retention"
        for days in (0, 90):
            refused = client.put(url, headers=anna, json={"originals_days": days})
            assert refused.status_code == 422 and "at most 60" in refused.json()["detail"]
        assert client.put(url, headers=anna, json={"originals_days": 45}).json()[
            "effective_originals_days"] == 45
        with sessions.begin() as db:  # set before the ceiling existed: capped, not obeyed
            db.get(Workspace, "ws-a").retention_originals_days = 0
        assert client.get(url, headers=anna).json()["effective_originals_days"] == 60
    engine.dispose()


# ── Deleting by hand ──────────────────────────────────────────────────────────────────────

def test_deleting_an_original_by_hand(team):
    client, runtime, sessions, h = team
    job_id = drafted(client, runtime, h["alice"])
    with sessions.begin() as db:  # uploaded a few minutes ago (not within the last minute)
        db.execute(update(MediaAsset).values(
            last_used_at=datetime.now(timezone.utc) - timedelta(minutes=5)))
    link = client.post(f"/api/v1/jobs/{job_id}/media-link", headers=h["alice"]).json()["url"]
    assert client.delete(f"/api/v1/jobs/{job_id}/media", headers=h["ada"]).status_code == 404
    assert client.delete(f"/api/v1/jobs/{job_id}/media", headers=h["max"]).status_code == 404

    assert client.delete(f"/api/v1/jobs/{job_id}/media", headers=h["alice"]).status_code == 204
    gone = media(client, h["alice"], job_id)
    assert gone.status_code == 410 and "on request" in gone.json()["detail"]
    assert client.get(link).status_code == 410  # an outstanding link ends at once
    assert client.post(f"/api/v1/jobs/{job_id}/media-link",
                       headers=h["alice"]).status_code == 410
    with sessions() as db:
        event = db.scalars(select(AuditEvent).where(AuditEvent.action == "media.deleted")).one()
        assert db.query(MediaAsset).one().purged_at is not None  # nobody else had the bytes
    assert (event.actor_email, event.after) == ("alice@a.example.com", {"reason": "manual"})
    assert client.delete(f"/api/v1/jobs/{job_id}/media", headers=h["alice"]).status_code == 204


def test_an_original_in_use_cannot_be_deleted(team):
    client, runtime, sessions, h = team
    queued = upload(client, h["alice"], png((7, 0, 0))).json()["job_id"]
    assert client.delete(f"/api/v1/jobs/{queued}/media", headers=h["alice"]).status_code == 409
    publishing = drafted(client, runtime, h["alice"], (7, 1, 0))
    with sessions.begin() as db:
        job = db.get(ApiJob, publishing)
        post = ScheduledPost(job_id=job.id, workspace_id="ws-a", owner_id=job.owner_id,
                             scheduled_at=datetime.now(timezone.utc) + timedelta(days=1),
                             timezone="UTC")
        db.add(post)
        db.flush()
        db.add(Publication(workspace_id="ws-a", scheduled_post_id=post.id,
                           connection_id=_connection(db), status="queued",
                           requested_by_email="alice@a.example.com"))
    refused = client.delete(f"/api/v1/jobs/{publishing}/media", headers=h["alice"])
    assert refused.status_code == 409 and "published" in refused.json()["detail"]


# ── Expiring links ────────────────────────────────────────────────────────────────────────

def test_local_links_are_signed_short_lived_and_support_ranges(app_env, monkeypatch):
    client, runtime, sessions, _, owner = app_env
    image = png((20, 30, 40))
    job_id = drafted(client, runtime, owner, (20, 30, 40))
    issued = client.post(f"/api/v1/jobs/{job_id}/media-link", headers=owner).json()
    assert issued["kind"] == "signed" and issued["url"].startswith("/api/v1/media/")
    lifetime = datetime.fromisoformat(issued["expires_at"]) - datetime.now(timezone.utc)
    assert timedelta(minutes=4) < lifetime <= timedelta(minutes=5)

    full = client.get(issued["url"])  # no bearer token needed: the link is the permission
    assert full.status_code == 200 and full.content == image
    assert full.headers["referrer-policy"] == "no-referrer"
    assert full.headers["cache-control"].startswith("private, max-age=")
    assert full.headers["content-disposition"].startswith("inline")
    part = client.get(issued["url"], headers={"Range": "bytes=0-3"})
    assert part.status_code == 206 and part.content == image[:4]

    body, _, sig = issued["url"].rsplit("/", 1)[1].partition(".")
    assert client.get(f"/api/v1/media/{body}.{sig[:-2]}AA").status_code == 403
    assert client.get("/api/v1/media/garbage").status_code == 403
    real_time = time.time
    monkeypatch.setattr("app.api.media_links.time.time", lambda: real_time() + 301)
    expired = client.get(issued["url"])
    assert expired.status_code == 410 and "expired" in expired.json()["detail"]


def test_links_are_only_issued_to_people_who_may_see_the_media(team):
    client, runtime, sessions, h = team
    job_id = drafted(client, runtime, h["alice"])
    for who, expected in (("alice", 200), ("max", 200), ("ada", 404), ("bea", 404)):
        assert client.post(f"/api/v1/jobs/{job_id}/media-link",
                           headers=h[who]).status_code == expected, who


def test_a_link_for_another_job_cannot_be_forged_by_editing_the_token(app_env):
    client, runtime, sessions, _, owner = app_env
    a = drafted(client, runtime, owner, (1, 0, 0))
    b = drafted(client, runtime, owner, (2, 0, 0))
    token = client.post(f"/api/v1/jobs/{a}/media-link", headers=owner).json()["url"].rsplit(
        "/", 1)[1]
    body, _, sig = token.partition(".")
    import base64

    claim = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    claim["j"] = b
    forged = base64.urlsafe_b64encode(json.dumps(claim).encode()).rstrip(b"=").decode()
    assert client.get(f"/api/v1/media/{forged}.{sig}").status_code == 403


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
    assert s3.head_object(Bucket="media", Key=key)["ContentType"] == "image/png"

    ref = storage.ref(key)
    assert ref == f"s3://media/{key}"
    with storage.materialize(ref) as local:
        assert local.endswith(".png") and open(local, "rb").read() == png()
    assert not os.path.exists(local)  # temp copy cleaned up
    with storage.materialize("/legacy/local/path.png") as local:
        assert local == "/legacy/local/path.png"

    storage.delete(key)
    assert not storage.exists(key)


def test_s3_uploads_can_be_encrypted_at_rest(s3, tmp_path):
    staged = tmp_path / "staged.png"
    staged.write_bytes(png())
    key = S3Storage("media", client=s3, sse="AES256").save(staged, "a.png", "cd" * 32)
    assert s3.head_object(Bucket="media", Key=key)["ServerSideEncryption"] == "AES256"


def test_s3_links_are_presigned_for_the_public_host(s3):
    from botocore.config import Config

    v4 = boto3.client("s3", region_name="us-east-1", config=Config(signature_version="s3v4"))
    storage = S3Storage("media", client=v4)
    url = storage.link("originals/x.png", filename='My "Photo".png', content_type="image/png",
                       ttl_s=300)
    query = parse_qs(urlsplit(url).query)
    assert query["X-Amz-Expires"] == ["300"] and "X-Amz-Signature" in query
    assert query["response-content-disposition"] == ['inline; filename="My _Photo_.png"']
    assert query["response-content-type"] == ["image/png"]

    public = S3Storage("media", client=s3, public_endpoint_url="https://media.example.com",
                       region_name="us-east-1")
    assert urlsplit(public.link("k.png", filename="k.png", content_type="image/png",
                                ttl_s=60)).netloc == "media.example.com"


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
        response = media(client, owner, job_id)
        assert response.status_code == 200 and response.content == image
        assert client.post(f"/api/v1/jobs/{job_id}/media-link",
                           headers=owner).json()["kind"] == "presigned"

        with sessions() as db:
            key = db.query(MediaAsset).one().stored_filename
        age(sessions, 40)
        assert prune_media(sessions, storage, retention_days=30)["objects_purged"] == 1
        assert not storage.exists(key)
        assert media(client, owner, job_id).status_code == 410
    engine.dispose()


# ── Readiness checks ──────────────────────────────────────────────────────────────────────

def levels(findings):
    return {f["check"]: f["level"] for f in findings}


def test_storage_check_flags_what_would_undermine_deletion(s3):
    storage = S3Storage("media", client=s3)
    assert levels(storage.check())["versioning"] == "ok"

    s3.put_bucket_versioning(Bucket="media", VersioningConfiguration={"Status": "Enabled"})
    found = {f["check"]: f for f in storage.check()}
    assert found["versioning"]["level"] == "warn"
    assert "recoverable" in found["versioning"]["detail"]
    s3.put_bucket_lifecycle_configuration(Bucket="media", LifecycleConfiguration={"Rules": [{
        "ID": "old-versions", "Status": "Enabled", "Filter": {"Prefix": ""},
        "NoncurrentVersionExpiration": {"NoncurrentDays": 1}}]})
    assert levels(storage.check())["versioning"] == "ok"

    assert levels(storage.check())["public_access"] == "warn"
    s3.put_public_access_block(Bucket="media", PublicAccessBlockConfiguration={
        "BlockPublicAcls": True, "IgnorePublicAcls": True, "BlockPublicPolicy": True,
        "RestrictPublicBuckets": True})
    assert levels(storage.check())["public_access"] == "ok"

    s3.put_bucket_encryption(Bucket="media", ServerSideEncryptionConfiguration={"Rules": [{
        "ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}]})
    assert levels(storage.check())["encryption"] == "ok"
    assert levels(S3Storage("missing", client=s3).check()) == {"bucket": "error"}


def test_a_just_uploaded_original_deleted_by_hand_goes_on_the_next_sweep(team):
    client, runtime, sessions, h = team
    job_id = drafted(client, runtime, h["alice"])  # uploaded this instant
    assert client.delete(f"/api/v1/jobs/{job_id}/media", headers=h["alice"]).status_code == 204
    assert media(client, h["alice"], job_id).status_code == 410  # access ends at once
    with sessions() as db:
        assert db.query(MediaAsset).one().purged_at is None  # bytes wait out the grace
    later = datetime.now(timezone.utc) + timedelta(hours=2)
    assert run(client, sessions, now=later)["objects_purged"] == 1


def test_storage_check_command(tmp_path, monkeypatch, capsys):
    from app.cli import main

    monkeypatch.setenv("MEDIA_STORAGE", "local")
    monkeypatch.setenv("MEDIA_STORAGE_DIR", str(tmp_path))
    assert main(["storage-check"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["level"] == "ok"
    monkeypatch.setenv("MEDIA_STORAGE_DIR", str(tmp_path / "missing"))
    assert main(["storage-check"]) == 1
    assert LocalStorage(tmp_path / "missing").check()[0]["level"] == "error"
