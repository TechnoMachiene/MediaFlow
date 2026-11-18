"""Publishing behind the provider interface, end to end against the sandbox provider.
See docs/PUBLISHING.md; each failure path there has a test here."""

import json
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from app.api.database import (
    ApiJob,
    AuditEvent,
    Publication,
    PublishingAuthState,
    PublishingConnection,
    SandboxPost,
    SandboxRequest,
    ScheduledPost,
)
from app.publishing.base import ProviderInfo
from app.publishing.crypto import Cipher
from app.publishing.delivery import Deliverer
from app.publishing.mock import MockProvider
from tests.api_harness import add_member, build_app, login, png, send_for_review, upload
from tests.helpers import ScriptedClient

pytestmark = pytest.mark.integration

DRAFT = {
    "instagram": {"caption": "Golden hour on the pier", "hashtags": ["#sunset", "#pier"]},
    "facebook": {"post_copy": "Our favourite view this week."},
}


class JsonClient(ScriptedClient):
    def analyse(self, spec):
        return {**super().analyse(spec), "text": json.dumps(DRAFT)}


class Clock:
    def __init__(self):
        self.t = datetime.now(timezone.utc)

    def __call__(self):
        return self.t

    def advance(self, **kw):
        self.t += timedelta(**kw)


@pytest.fixture
def world(make_rt, tmp_path, monkeypatch):
    monkeypatch.setattr("app.api.routes.JOB_RESULT_WAIT_S", 0)
    runtime = make_rt(client=JsonClient())
    application, engine, sessions = build_app(runtime, tmp_path / "media")
    add_member(sessions, "anna@a.example.com", "admin", "ws-a")
    add_member(sessions, "max@a.example.com", "manager", "ws-a")
    add_member(sessions, "alice@a.example.com", "user", "ws-a")
    add_member(sessions, "ada@a.example.com", "user", "ws-a")
    add_member(sessions, "bea@b.example.com", "admin", "ws-b")
    with TestClient(application) as client:
        clock = Clock()
        state = client.app.state
        w = type("W", (), {})()
        w.client, w.sessions, w.runtime, w.clock = client, sessions, runtime, clock
        w.h = {n: login(client, f"{n}@{'b' if n == 'bea' else 'a'}.example.com")
               for n in ("anna", "max", "alice", "ada", "bea")}
        w.deliverer = Deliverer(sessions, state.providers, state.cipher, now=clock,
                                backoff_base_s=10, lease_s=60, check_every_s=30)
        yield w
    engine.dispose()


def connect(w, behavior="succeed", channel="instagram", who="anna"):
    start = w.client.post("/api/v1/publishing/connections/authorize", headers=w.h[who],
                          json={"provider": "mock", "channel": channel,
                                "options": {"behavior": behavior}})
    assert start.status_code == 200, start.text
    query = parse_qs(urlsplit(start.json()["authorize_url"].replace("/#", "")).query)
    state, challenge = start.json()["state"], query["challenge"][0]
    consent = {"state": state, "challenge": challenge, "allow": True}
    code = w.client.post("/api/v1/publishing/sandbox/authorize", headers=w.h[who],
                         json=consent).json()["code"]
    done = w.client.post("/api/v1/publishing/oauth/callback", headers=w.h[who],
                         json={"state": state, "code": code})
    assert done.status_code == 200, done.text
    return done.json()


def scheduled_post(w, days=1) -> str:
    job_id = upload(w.client, w.h["alice"], png((days, 2, 3))).json()["job_id"]
    w.runtime.worker.drain()
    send_for_review(w.client, w.h["alice"], job_id)
    w.client.patch(f"/api/v1/jobs/{job_id}/approval", headers=w.h["max"],
                   json={"status": "approved", "checked": True})
    when = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()
    post = w.client.post(f"/api/v1/jobs/{job_id}/schedule", headers=w.h["alice"],
                         json={"scheduled_at": when, "timezone": "UTC"})
    assert post.status_code == 201, post.text
    return post.json()["id"]


def publish(w, post_id, connection_id, who="alice"):
    response = w.client.post(f"/api/v1/scheduled-posts/{post_id}/publications",
                             headers=w.h[who], json={"connection_id": connection_id})
    assert response.status_code == 201, response.text
    return response.json()


def pub(w, publication_id) -> Publication:
    with w.sessions() as db:
        return db.get(Publication, publication_id)


def due(w, days=1, **extra):
    """Move the clock past the post's time (plus anything extra)."""
    w.clock.t = datetime.now(timezone.utc) + timedelta(days=days, minutes=1, **extra)


def sandbox_requests(w):
    with w.sessions() as db:
        return [r.outcome for r in db.scalars(select(SandboxRequest).where(
            SandboxRequest.kind == "publish").order_by(SandboxRequest.id))]


# ── Connecting (OAuth + PKCE against the sandbox) ──────────────────────────────────────────

def test_connecting_stores_encrypted_credentials_and_audits(world):
    w = world
    conn = connect(w)
    assert (conn["provider"], conn["sandbox"], conn["status"], conn["channel"]) == (
        "mock", True, "active", "instagram")
    with w.sessions() as db:
        row = db.get(PublishingConnection, conn["id"])
        assert "sbx-at-" not in row.credentials  # encrypted at rest
        creds = w.client.app.state.cipher.open_credentials(row.credentials)
        assert creds.access_token.startswith("sbx-at-") and creds.refresh_token
        event = db.scalars(select(AuditEvent).where(
            AuditEvent.action == "connection.connected")).one()
        assert event.after["account"] == conn["account_name"]
    listed = w.client.get("/api/v1/publishing/connections", headers=w.h["alice"]).json()
    assert [c["id"] for c in listed] == [conn["id"]]
    assert "credentials" not in listed[0]


def start(w, who="anna"):
    response = w.client.post("/api/v1/publishing/connections/authorize", headers=w.h[who],
                             json={"provider": "mock", "channel": "instagram"})
    return response


def test_authorization_state_is_single_use_bound_and_expiring(world):
    w = world
    assert start(w, "max").status_code == 403  # admins only
    s = start(w).json()
    challenge = parse_qs(urlsplit(s["authorize_url"].replace("/#", "")).query)["challenge"][0]
    code = w.client.post("/api/v1/publishing/sandbox/authorize", headers=w.h["anna"],
                         json={"state": s["state"], "challenge": challenge,
                               "allow": True}).json()["code"]
    callback = {"state": s["state"], "code": code}
    # Another workspace's admin can't finish it, even with the state and the code.
    assert w.client.post("/api/v1/publishing/oauth/callback", headers=w.h["bea"],
                         json=callback).status_code == 404
    assert w.client.post("/api/v1/publishing/oauth/callback", headers=w.h["anna"],
                         json=callback).status_code == 200
    assert w.client.post("/api/v1/publishing/oauth/callback", headers=w.h["anna"],
                         json=callback).status_code == 410  # used

    late = start(w).json()
    with w.sessions.begin() as db:
        db.execute(update(PublishingAuthState).where(PublishingAuthState.used_at.is_(None))
                   .values(expires_at=datetime.now(timezone.utc) - timedelta(seconds=1)))
    assert w.client.post("/api/v1/publishing/oauth/callback", headers=w.h["anna"],
                         json={"state": late["state"], "code": code}).status_code == 410


def test_a_code_issued_for_another_challenge_fails_pkce(world):
    w = world
    s = start(w).json()
    stolen = w.client.post("/api/v1/publishing/sandbox/authorize", headers=w.h["anna"],
                           json={"state": s["state"], "challenge": "A" * 43,
                                 "allow": True}).json()["code"]
    refused = w.client.post("/api/v1/publishing/oauth/callback", headers=w.h["anna"],
                            json={"state": s["state"], "code": stolen})
    assert refused.status_code == 400 and "PKCE" in refused.json()["detail"]
    assert w.client.get("/api/v1/publishing/connections", headers=w.h["anna"]).json() == []


def test_declining_consent_connects_nothing(world):
    w = world
    s = start(w).json()
    denied = w.client.post("/api/v1/publishing/oauth/callback", headers=w.h["anna"],
                           json={"state": s["state"], "error": "access_denied"})
    assert denied.status_code == 400
    assert w.client.get("/api/v1/publishing/connections", headers=w.h["anna"]).json() == []


def test_disconnecting_wipes_credentials_and_cancels_pending_posts(world):
    w = world
    conn = connect(w)
    p = publish(w, scheduled_post(w), conn["id"])
    gone = w.client.delete(f"/api/v1/publishing/connections/{conn['id']}", headers=w.h["anna"])
    assert gone.json()["status"] == "disconnected"
    with w.sessions() as db:
        assert db.get(PublishingConnection, conn["id"]).credentials is None
    assert (pub(w, p["id"]).status, pub(w, p["id"]).error_code) == ("cancelled", "connection")
    assert w.client.get("/api/v1/publishing/connections", headers=w.h["anna"]).json() == []


# ── Delivery ──────────────────────────────────────────────────────────────────────────────

def test_a_post_publishes_at_its_time_with_the_draft_for_its_channel(world):
    w = world
    conn = connect(w)
    p = publish(w, scheduled_post(w), conn["id"])
    assert p["status"] == "queued"
    assert w.deliverer.run_once() == {}  # not due yet
    due(w)
    assert w.deliverer.run_once() == {"published": 1}

    done = pub(w, p["id"])
    assert (done.status, done.attempts) == ("published", 1)
    assert done.external_id.startswith("sbxpost_") and done.published_at
    with w.sessions() as db:
        [post] = db.scalars(select(SandboxPost)).all()
    assert (post.body, post.hashtags, post.channel) == (
        "Golden hour on the pier", ["#sunset", "#pier"], "instagram")


def test_transient_failures_retry_with_backoff_then_succeed(world):
    w = world
    p = publish(w, scheduled_post(w), connect(w, "flaky")["id"])
    due(w)
    assert w.deliverer.run_once() == {"retry": 1}
    first = pub(w, p["id"])
    assert (first.status, first.error_code) == ("retrying", "unavailable")
    wait = (first.next_attempt_at.replace(tzinfo=timezone.utc) - w.clock.t).total_seconds()
    assert 8 <= wait <= 12  # base 10s, jittered
    assert w.deliverer.run_once() == {}  # not before the backoff
    w.clock.advance(seconds=13)
    assert w.deliverer.run_once() == {"retry": 1}
    w.clock.advance(seconds=25)
    assert w.deliverer.run_once() == {"published": 1}
    assert pub(w, p["id"]).attempts == 3
    assert sandbox_requests(w) == ["503", "503", "published"]


def test_rate_limits_wait_at_least_retry_after(world):
    w = world
    w.deliverer.backoff_base_s = 0.1
    p = publish(w, scheduled_post(w), connect(w, "rate_limited")["id"])
    due(w)
    assert w.deliverer.run_once() == {"retry": 1}
    after = pub(w, p["id"]).next_attempt_at.replace(tzinfo=timezone.utc)
    assert (after - w.clock.t).total_seconds() >= 2.0
    w.clock.advance(seconds=3)
    assert w.deliverer.run_once() == {"published": 1}


def test_asynchronous_platforms_are_polled_until_published(world):
    w = world
    p = publish(w, scheduled_post(w), connect(w, "processing")["id"])
    due(w)
    assert w.deliverer.run_once() == {"processing": 1}
    assert pub(w, p["id"]).status == "processing"
    assert w.deliverer.run_once() == {}
    w.clock.advance(seconds=31)
    assert w.deliverer.run_once() == {"published": 1}
    with w.sessions() as db:
        assert db.scalars(select(SandboxPost)).one().state == "published"


def test_rejected_content_fails_without_retrying(world):
    w = world
    p = publish(w, scheduled_post(w), connect(w, "reject")["id"])
    due(w)
    assert w.deliverer.run_once() == {"failed": 1}
    failed = pub(w, p["id"])
    assert (failed.status, failed.error_code) == ("failed", "rejected")
    assert failed.next_attempt_at is None
    w.clock.advance(hours=1)
    assert w.deliverer.run_once() == {}


def test_lost_authorization_marks_the_account_for_reconnection(world):
    w = world
    conn = connect(w, "expire")
    p = publish(w, scheduled_post(w), conn["id"])
    due(w)
    assert w.deliverer.run_once() == {"authorization": 1}
    assert (pub(w, p["id"]).status, pub(w, p["id"]).error_code) == ("failed", "authorization")
    [listed] = w.client.get("/api/v1/publishing/connections", headers=w.h["anna"]).json()
    assert listed["status"] == "needs_reauth" and "refresh" in listed["last_error"]
    retry = w.client.post(f"/api/v1/publications/{p['id']}/retry", headers=w.h["alice"])
    assert retry.status_code == 409 and "Reconnect" in retry.json()["detail"]


def test_expiring_tokens_are_refreshed_before_publishing(world):
    w = world
    conn = connect(w)
    p = publish(w, scheduled_post(w), conn["id"])
    due(w)  # a day later: the 1-hour token has long expired
    assert w.deliverer.run_once() == {"published": 1}
    with w.sessions() as db:
        row = db.get(PublishingConnection, conn["id"])
        kinds = [r.kind for r in db.scalars(select(SandboxRequest).order_by(SandboxRequest.id))]
    assert kinds == ["refresh", "publish"]
    assert row.token_expires_at.replace(tzinfo=timezone.utc) > datetime.now(timezone.utc)
    assert pub(w, p["id"]).status == "published"


def test_retries_stop_after_max_attempts(world):
    w = world
    w.deliverer.max_attempts = 2
    p = publish(w, scheduled_post(w), connect(w, "flaky")["id"])
    due(w)
    w.deliverer.run_once()
    w.clock.advance(minutes=5)
    assert w.deliverer.run_once() == {"failed": 1}
    assert (pub(w, p["id"]).status, pub(w, p["id"]).error_code) == ("failed", "unavailable")


def test_unreadable_credentials_mean_reconnect(world):
    w = world
    conn = connect(w)
    p = publish(w, scheduled_post(w), conn["id"])
    w.deliverer.cipher = Cipher(jwt_secret="a-different-secret-" + "y" * 30)  # key rotated
    due(w)
    assert w.deliverer.run_once() == {"authorization": 1}
    assert pub(w, p["id"]).error_code == "authorization"


# ── Crash safety ──────────────────────────────────────────────────────────────────────────

def test_crash_after_the_platform_accepted_never_double_posts(world, monkeypatch):
    """The sandbox published, then the worker died before recording it. The lease expires,
    the next attempt re-sends the same idempotency key, and the platform returns the post it
    already has."""
    w = world
    p = publish(w, scheduled_post(w), connect(w)["id"])
    due(w)

    def worker_dies(*args, **kwargs):
        raise SystemExit("worker killed")

    monkeypatch.setattr(w.deliverer, "_record_receipt", worker_dies)
    with pytest.raises(SystemExit):
        w.deliverer.run_once()
    assert pub(w, p["id"]).status == "publishing"
    monkeypatch.undo()

    assert w.deliverer.run_once() == {}  # lease still held
    w.clock.advance(seconds=61)
    assert w.deliverer.run_once() == {"published": 1}
    with w.sessions() as db:
        assert len(db.scalars(select(SandboxPost)).all()) == 1
    assert sandbox_requests(w) == ["published", "duplicate"]


class NotIdempotent(MockProvider):
    info = ProviderInfo(name="mock", display_name="Sandbox", sandbox=True,
                        channels=MockProvider.info.channels, idempotent=False,
                        options=MockProvider.info.options)


def test_unknown_outcome_on_a_non_idempotent_provider_asks_a_person(world, monkeypatch):
    w = world
    w.deliverer.providers = {"mock": NotIdempotent(w.sessions)}
    p = publish(w, scheduled_post(w), connect(w)["id"])
    due(w)
    monkeypatch.setattr(w.deliverer, "_record_receipt",
                        lambda *a, **k: (_ for _ in ()).throw(SystemExit("killed")))
    with pytest.raises(SystemExit):
        w.deliverer.run_once()
    monkeypatch.undo()
    w.clock.advance(seconds=61)
    assert w.deliverer.run_once() == {"unknown_outcome": 1}
    stuck = pub(w, p["id"])
    assert stuck.status == "unknown" and "Check the account" in stuck.last_error
    assert sandbox_requests(w) == ["published"]  # never sent twice on its own

    assert w.client.post(f"/api/v1/publications/{p['id']}/retry",
                         headers=w.h["alice"]).json()["status"] == "queued"


def test_a_worker_that_lost_its_lease_cannot_overwrite_the_outcome(world):
    w = world
    p = publish(w, scheduled_post(w), connect(w)["id"])
    due(w)
    slow, _ = w.deliverer._claim(p["id"])  # worker A claims, then stalls
    w.clock.advance(seconds=61)
    assert w.deliverer.run_once() == {"published": 1}  # worker B takes over and finishes

    from app.publishing.base import ProviderUnavailable
    assert w.deliverer._record_failure(slow, ProviderUnavailable("late")) == "stale"
    assert pub(w, p["id"]).status == "published"


# ── Checked again at send time ────────────────────────────────────────────────────────────

def test_cancelling_or_rescheduling_the_post_carries_over(world):
    w = world
    conn = connect(w)
    post_id = scheduled_post(w)
    p = publish(w, post_id, conn["id"])
    later = (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()
    w.client.patch(f"/api/v1/scheduled-posts/{post_id}", headers=w.h["alice"],
                   json={"scheduled_at": later, "timezone": "UTC"})
    due(w)
    assert w.deliverer.run_once() == {}  # moved with the post
    w.client.delete(f"/api/v1/scheduled-posts/{post_id}", headers=w.h["alice"])
    assert (pub(w, p["id"]).status, pub(w, p["id"]).error_code) == ("cancelled", "post_cancelled")


def test_a_draft_that_lost_approval_is_not_sent(world):
    w = world
    post_id = scheduled_post(w)
    p = publish(w, post_id, connect(w)["id"])
    with w.sessions.begin() as db:
        db.execute(update(ApiJob).values(approval_status="pending"))
    due(w)
    assert w.deliverer.run_once() == {"not_approved": 1}
    assert pub(w, p["id"]).status == "failed"
    with w.sessions() as db:
        assert db.scalars(select(SandboxPost)).all() == []


# ── Requests and permissions ──────────────────────────────────────────────────────────────

def test_requesting_needs_an_approved_scheduled_post_and_matching_content(world):
    w = world
    post_id = scheduled_post(w)
    tiktok = connect(w, channel="tiktok")  # this draft has nothing for TikTok
    refused = w.client.post(f"/api/v1/scheduled-posts/{post_id}/publications",
                            headers=w.h["alice"], json={"connection_id": tiktok["id"]})
    assert refused.status_code == 422 and "tiktok" in refused.json()["detail"]

    conn = connect(w)
    p = publish(w, post_id, conn["id"])
    again = w.client.post(f"/api/v1/scheduled-posts/{post_id}/publications",
                          headers=w.h["alice"], json={"connection_id": conn["id"]})
    assert again.status_code == 409
    assert w.client.delete(f"/api/v1/publications/{p['id']}",
                           headers=w.h["alice"]).json()["status"] == "cancelled"
    assert publish(w, post_id, conn["id"])["id"] == p["id"]  # re-requested, same delivery

    with w.sessions() as db:
        actions = [e.action for e in db.scalars(select(AuditEvent).where(
            AuditEvent.action.like("publication.%")).order_by(AuditEvent.id))]
    assert actions == ["publication.requested", "publication.cancelled", "publication.requested"]


def test_only_the_posts_owner_or_an_admin_can_publish_it(world):
    w = world
    conn = connect(w)
    post_id = scheduled_post(w)
    for who in ("ada", "max"):  # another member; a manager who isn't an admin
        assert w.client.post(f"/api/v1/scheduled-posts/{post_id}/publications",
                             headers=w.h[who], json={"connection_id": conn["id"]}
                             ).status_code == 404
    p = publish(w, post_id, conn["id"], who="anna")
    assert w.client.delete(f"/api/v1/publications/{p['id']}",
                           headers=w.h["ada"]).status_code == 404
    assert w.client.get(f"/api/v1/scheduled-posts/{post_id}/publications",
                        headers=w.h["alice"]).json()[0]["id"] == p["id"]


def test_published_or_in_flight_posts_cannot_be_cancelled_or_retried(world):
    w = world
    p = publish(w, scheduled_post(w), connect(w)["id"])
    due(w)
    w.deliverer.run_once()
    assert w.client.delete(f"/api/v1/publications/{p['id']}",
                           headers=w.h["alice"]).status_code == 409
    assert w.client.post(f"/api/v1/publications/{p['id']}/retry",
                         headers=w.h["alice"]).status_code == 409


def test_the_sandbox_feed_shows_this_workspaces_platform_side_only(world):
    w = world
    p = publish(w, scheduled_post(w), connect(w, "flaky")["id"])
    due(w)
    for _ in range(3):
        w.deliverer.run_once()
        w.clock.advance(minutes=5)
    feed = w.client.get("/api/v1/publishing/sandbox", headers=w.h["alice"]).json()
    assert [x["body"] for x in feed["posts"]] == ["Golden hour on the pier"]
    assert [r["outcome"] for r in feed["requests"] if r["kind"] == "publish"] == [
        "published", "503", "503"]
    assert pub(w, p["id"]).status == "published"
    assert w.client.get("/api/v1/publishing/sandbox", headers=w.h["bea"]).json() == {
        "posts": [], "requests": []}


def test_publishing_can_be_switched_off(make_rt, tmp_path):
    runtime = make_rt(client=JsonClient())
    application, engine, sessions = build_app(runtime, tmp_path / "media",
                                              publishing_providers=())
    add_member(sessions, "anna@a.example.com", "admin", "ws-a")
    with TestClient(application) as client:
        anna = login(client, "anna@a.example.com")
        assert client.get("/api/v1/publishing/providers", headers=anna).json() == []
        assert client.post("/api/v1/publishing/connections/authorize", headers=anna,
                           json={"provider": "mock", "channel": "instagram"}).status_code == 404
    engine.dispose()


# ── Wiring ────────────────────────────────────────────────────────────────────────────────

def test_the_worker_runs_the_delivery_loop(world):
    from app.api.worker_setup import configure_worker

    w = world
    state = w.client.app.state
    p = publish(w, scheduled_post(w), connect(w)["id"])  # (drains the queue worker)
    configure_worker(w.runtime, w.sessions, state.storage, retention_days=30,
                     publishing=(("mock",), state.cipher))
    with w.sessions.begin() as db:  # make it due now, by the real clock the worker uses
        db.get(Publication, p["id"]).next_attempt_at = datetime.now(timezone.utc)
    w.runtime.worker.maintenance()
    assert pub(w, p["id"]).status == "published"


def test_the_worker_derives_the_same_key_as_the_api(world, monkeypatch):
    from app.cli import _publishing

    w = world
    conn = connect(w)
    monkeypatch.setenv("JWT_SECRET", w.client.app.state.api_settings.jwt_secret)
    monkeypatch.delenv("PUBLISHING_KEY", raising=False)
    names, cipher = _publishing()
    with w.sessions() as db:
        sealed = db.get(PublishingConnection, conn["id"]).credentials
    assert names == ("mock",) and cipher.open_credentials(sealed).access_token
    monkeypatch.setenv("PUBLISHING_PROVIDERS", "none")
    assert _publishing() is None


def test_the_calendar_tells_posted_from_planned(world):
    w = world

    def delivery(post_id):
        got = w.client.get(f"/api/v1/scheduled-posts/{post_id}", headers=w.h["alice"]).json()
        return got["delivery"], got["published_url"]

    def back_date(post_id, days=1):
        with w.sessions.begin() as db:
            db.execute(update(ScheduledPost).where(ScheduledPost.id == post_id).values(
                scheduled_at=datetime.now(timezone.utc) - timedelta(days=days)))

    planned = scheduled_post(w, days=1)
    assert delivery(planned) == ("upcoming", None)
    back_date(planned)
    assert delivery(planned) == ("past", None)  # time passed, no automatic delivery

    ok = connect(w, "succeed")
    live = scheduled_post(w, days=2)
    pub = w.client.post(f"/api/v1/scheduled-posts/{live}/publications", headers=w.h["alice"],
                        json={"connection_id": ok["id"]}).json()
    assert delivery(live) == ("upcoming", None)  # auto-publishing set, not due yet
    back_date(live)
    assert delivery(live) == ("sending", None)
    w.clock.advance(days=3)
    w.deliverer.run_once()
    state, url = delivery(live)
    assert state == "published" and url

    bad = connect(w, "reject")
    refused = scheduled_post(w, days=3)
    w.client.post(f"/api/v1/scheduled-posts/{refused}/publications", headers=w.h["alice"],
                  json={"connection_id": bad["id"]})
    w.clock.advance(days=5)
    w.deliverer.run_once()
    assert delivery(refused) == ("failed", None)

    w.client.delete(f"/api/v1/scheduled-posts/{planned}", headers=w.h["alice"])
    assert delivery(planned) == ("cancelled", None)

    # The month view carries the same, in one go.
    day = datetime.now(timezone.utc).date()
    month = w.client.get("/api/v1/calendar", headers=w.h["alice"], params={
        "start": str(day - timedelta(days=2)), "end": str(day + timedelta(days=4)),
        "timezone": "UTC"}).json()
    states = {p["id"]: p["delivery"] for d in month["days"] for p in d["posts"]}
    assert states[live] == "published" and states[refused] == "failed"
    assert pub["id"]
