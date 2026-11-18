"""Notifications (in-app and email), the approval safeguards, and real-platform publishing
through the delivery loop with Meta's Graph API scripted."""

import io
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import select

from app.api.database import AuditEvent, Notification, Publication
from app.api.media_links import BROWSER, LinkClaim, MediaLinkSigner
from app.api.notifications import EmailOutbox, PermanentEmailError, publication_outcome
from app.publishing.delivery import Deliverer
from app.publishing.media import MediaResolver
from app.publishing.meta import MetaConfig, MetaProvider
from tests.api_harness import add_member, build_app, login, png, send_for_review, upload
from tests.helpers import ScriptedClient

pytestmark = pytest.mark.integration

DRAFT = {"facebook": {"caption": "25th of 27 floors at MGC Jewel."},
         "instagram": {"caption": "Floor 25 at MGC Jewel.", "hashtags": ["#MGCDevelopments"]}}
GAPPY = {"facebook": {"caption": "Plot size: [NEEDS: confirmed kanal]."}}


class JsonClient(ScriptedClient):
    def __init__(self):
        super().__init__()
        self.draft = DRAFT

    def analyse(self, spec):
        return {**super().analyse(spec), "text": json.dumps(self.draft)}


class FakeMailer:
    def __init__(self, fail=None):
        self.sent, self.fail = [], fail

    def send(self, to, subject, text):
        if self.fail:
            raise self.fail
        self.sent.append((to, subject, text))


@pytest.fixture
def team(make_rt, tmp_path, monkeypatch):
    monkeypatch.setattr("app.api.routes.JOB_RESULT_WAIT_S", 0)
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://media.example.com")
    gemini = JsonClient()
    runtime = make_rt(client=gemini)
    application, engine, sessions = build_app(runtime, tmp_path / "media")
    add_member(sessions, "anna@a.example.com", "admin", "ws-a")
    add_member(sessions, "max@a.example.com", "manager", "ws-a")
    add_member(sessions, "alice@a.example.com", "user", "ws-a")
    add_member(sessions, "bea@b.example.com", "admin", "ws-b")
    with TestClient(application) as client:
        h = {n: login(client, f"{n}@{'b' if n == 'bea' else 'a'}.example.com")
             for n in ("anna", "max", "alice", "bea")}
        yield client, sessions, runtime, h, gemini
    engine.dispose()


def drafted(client, runtime, headers, color=(1, 2, 3)):
    job_id = upload(client, headers, png(color)).json()["job_id"]
    runtime.worker.drain()
    return job_id


def inbox(client, headers):
    return client.get("/api/v1/notifications", headers=headers).json()


def approve(client, headers, job_id, **extra):
    return client.patch(f"/api/v1/jobs/{job_id}/approval", headers=headers,
                        json={"status": "approved", **extra})


# ── Who hears about what ─────────────────────────────────────────────────────────────────

def test_sending_for_review_tells_the_reviewers_but_not_the_sender(team):
    client, sessions, runtime, h, _ = team
    job_id = drafted(client, runtime, h["alice"])
    send_for_review(client, h["alice"], job_id)
    for reviewer in ("anna", "max"):
        got = inbox(client, h[reviewer])
        assert got["unread"] == 1
        [n] = got["items"]
        assert (n["kind"], n["job_id"], n["route"]) == ("draft.submitted", job_id, "review")
        assert "cat.png" in n["title"] and "price" in n["body"]
    assert inbox(client, h["alice"])["items"] == []
    assert inbox(client, h["bea"])["items"] == []  # another workspace hears nothing


def test_decisions_tell_the_uploader_with_the_reason(team):
    client, sessions, runtime, h, _ = team
    ok, no = drafted(client, runtime, h["alice"]), drafted(client, runtime, h["alice"], (9, 9, 9))
    for job_id in (ok, no):
        send_for_review(client, h["alice"], job_id)
    assert approve(client, h["max"], ok, checked=True).status_code == 200
    client.patch(f"/api/v1/jobs/{no}/approval", headers=h["max"],
                 json={"status": "rejected", "comment": "Wrong floor count"})
    items = {n["kind"]: n for n in inbox(client, h["alice"])["items"]}
    assert "calendar" in items["job.approved"]["body"]
    assert '"Wrong floor count"' in items["job.rejected"]["body"]
    assert items["job.rejected"]["title"].startswith("Changes needed")


def test_notifications_are_marked_read_one_by_one_or_all_and_only_your_own(team):
    client, sessions, runtime, h, _ = team
    for color in ((1, 1, 1), (2, 2, 2)):
        send_for_review(client, h["alice"], drafted(client, runtime, h["alice"], color))
    first, second = inbox(client, h["max"])["items"]
    # Someone else's id does nothing to yours.
    client.post("/api/v1/notifications/read", headers=h["anna"], json={"ids": [first["id"]]})
    assert inbox(client, h["max"])["unread"] == 2
    client.post("/api/v1/notifications/read", headers=h["max"], json={"ids": [first["id"]]})
    assert inbox(client, h["max"])["unread"] == 1
    client.post("/api/v1/notifications/read", headers=h["max"], json={"all": True})
    assert inbox(client, h["max"])["unread"] == 0
    assert inbox(client, h["anna"])["unread"] == 2


# ── Email ────────────────────────────────────────────────────────────────────────────────

def test_email_copies_are_sent_once_with_a_link_and_retried_on_failure(team):
    client, sessions, runtime, h, _ = team
    send_for_review(client, h["alice"], drafted(client, runtime, h["alice"]))
    broken = EmailOutbox(sessions, FakeMailer(fail=OSError("connection refused")))
    assert broken.run_once() == {"sent": 0, "retry": 2, "failed": 0}
    with sessions() as db:
        rows = db.scalars(select(Notification)).all()
    assert {r.email_status for r in rows} == {"pending"}
    assert all("connection refused" in r.email_error for r in rows)

    mailer = FakeMailer()
    later = lambda: datetime.now(timezone.utc) + timedelta(hours=1)  # noqa: E731
    assert EmailOutbox(sessions, mailer, now=later).run_once()["sent"] == 2
    assert {to for to, _, _ in mailer.sent} == {"anna@a.example.com", "max@a.example.com"}
    to, subject, text = mailer.sent[0]
    assert subject.startswith("New draft to review")
    assert "https://media.example.com/#/review?job=" in text and "Email me" in text
    assert EmailOutbox(sessions, mailer, now=later).run_once()["sent"] == 0  # never twice


def test_a_refused_address_fails_without_retrying(team):
    client, sessions, runtime, h, _ = team
    send_for_review(client, h["alice"], drafted(client, runtime, h["alice"]))
    out = EmailOutbox(sessions, FakeMailer(fail=PermanentEmailError("refused"))).run_once()
    assert out == {"sent": 0, "retry": 0, "failed": 2}


def test_people_can_turn_their_email_copies_off(team):
    client, sessions, runtime, h, _ = team
    prefs = client.get("/api/v1/notifications/preferences", headers=h["max"]).json()
    assert prefs == {"email": True, "email_available": True}
    off = client.put("/api/v1/notifications/preferences", headers=h["max"], json={"email": False})
    assert off.json()["email"] is False
    send_for_review(client, h["alice"], drafted(client, runtime, h["alice"]))
    with sessions() as db:
        status = {n.email: n.email_status for n in db.scalars(select(Notification))}
    assert status == {"max@a.example.com": "off", "anna@a.example.com": "pending"}
    assert inbox(client, h["max"])["unread"] == 1  # still told in the app


def test_without_smtp_notifications_stay_in_the_app(team, monkeypatch):
    client, sessions, runtime, h, _ = team
    monkeypatch.delenv("SMTP_HOST")
    send_for_review(client, h["alice"], drafted(client, runtime, h["alice"]))
    with sessions() as db:
        assert {n.email_status for n in db.scalars(select(Notification))} == {"off"}
    assert client.get("/api/v1/notifications/preferences",
                      headers=h["max"]).json()["email_available"] is False


def test_invitations_are_emailed_and_the_link_is_erased_once_sent(team):
    client, sessions, runtime, h, _ = team
    made = client.post("/api/v1/invitations", headers=h["anna"],
                       json={"email": "new@example.com", "role": "user"}).json()
    assert made["emailed"] is True
    with sessions() as db:
        row = db.scalar(select(Notification).where(Notification.kind == "invitation"))
        assert row.user_id is None and made["token"] not in row.secret  # sealed
    mailer = FakeMailer()
    cipher = client.app.state.cipher
    EmailOutbox(sessions, mailer, cipher=cipher).run_once()
    [(to, subject, text)] = mailer.sent
    assert to == "new@example.com" and "invited" in subject.lower()
    assert f"https://media.example.com/#/invite/{made['token']}" in text
    with sessions() as db:
        row = db.scalar(select(Notification).where(Notification.kind == "invitation"))
        assert row.secret is None and row.email_status == "sent"


# ── Approval safeguards ──────────────────────────────────────────────────────────────────

def test_approving_needs_the_facts_checked_and_no_gaps(team):
    client, sessions, runtime, h, gemini = team
    gemini.draft = GAPPY
    job_id = drafted(client, runtime, h["alice"])
    view = client.get(f"/api/v1/jobs/{job_id}", headers=h["alice"]).json()
    assert view["checks"] == [{"level": "block", "field": "facebook.caption",
                               "message": "Fill in [NEEDS: confirmed kanal] before approving"}]
    send_for_review(client, h["alice"], job_id)  # the uploader may send it; a reviewer may fill
    gap = approve(client, h["max"], job_id, checked=True)
    assert gap.status_code == 409 and "[NEEDS: confirmed kanal]" in gap.json()["detail"]

    fixed = json.dumps({"facebook": {"caption": "Plot size: 12 kanal."}})
    client.post(f"/api/v1/jobs/{job_id}/versions", headers=h["max"],
                json={"base_version": 1, "text": fixed})
    unchecked = approve(client, h["max"], job_id)
    assert unchecked.status_code == 422 and "checked" in unchecked.json()["detail"]
    assert approve(client, h["max"], job_id, checked=True).status_code == 200
    with sessions() as db:
        event = db.scalar(select(AuditEvent).where(AuditEvent.action == "job.approved"))
    assert event.after["facts_checked"] is True
    # Rejecting never needs the confirmation.
    other = drafted(client, runtime, h["alice"], (7, 7, 7))
    send_for_review(client, h["alice"], other)
    assert client.patch(f"/api/v1/jobs/{other}/approval", headers=h["max"],
                        json={"status": "rejected"}).status_code == 200


# ── Media for Instagram ──────────────────────────────────────────────────────────────────

def _job_key(client, sessions, job_id):
    resolver = MediaResolver(client.app.state.storage, client.app.state.publish_signer,
                             "https://media.example.com")
    from app.api.database import ApiJob

    with sessions() as db:
        return resolver.for_job(db, db.get(ApiJob, job_id))


def test_instagram_fetches_a_jpeg_padded_to_an_allowed_shape(team):
    client, sessions, runtime, h, _ = team
    tall = io.BytesIO()
    Image.new("RGB", (100, 400), "red").save(tall, "PNG")  # 1:4, taller than 4:5
    job_id = upload(client, h["alice"], tall.getvalue()).json()["job_id"]
    runtime.worker.drain()
    media = _job_key(client, sessions, job_id)
    url = media.public_url()
    assert url.startswith("https://media.example.com/api/v1/publish-media/")
    got = client.get(url.removeprefix("https://media.example.com"))  # no session needed
    assert got.status_code == 200 and got.headers["content-type"] == "image/jpeg"
    with Image.open(io.BytesIO(got.content)) as im:
        assert im.format == "JPEG" and abs(im.width / im.height - 0.8) < 0.01

    # A browser link's token is no good here, and vice versa.
    browser = MediaLinkSigner(client.app.state.api_settings.jwt_secret, BROWSER).sign(
        LinkClaim(media.job_id, media.workspace_id, media.key, 2**31))
    assert client.get(f"/api/v1/publish-media/{browser}").status_code == 403
    client.delete(f"/api/v1/jobs/{job_id}/media", headers=h["alice"])
    assert client.get(url.removeprefix("https://media.example.com")).status_code == 410


# ── Meta through the delivery loop ───────────────────────────────────────────────────────

def test_a_post_reaches_a_facebook_page_and_its_owner_hears_about_it(team, monkeypatch):
    client, sessions, runtime, h, _ = team
    calls = []

    def graph(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.method == "GET":
            return httpx.Response(200, json={"id": "1111", "name": "MGC Developments"})
        return httpx.Response(200, json={"id": "5", "post_id": "1111_7"})

    meta = MetaProvider(MetaConfig(page_id="1111", page_token="tok"),
                        transport=httpx.MockTransport(graph))
    client.app.state.providers["meta"] = meta

    # Only the installation's operator may connect it, not a workspace admin.
    denied = client.post("/api/v1/publishing/connections/authorize", headers=h["anna"],
                         json={"provider": "meta", "channel": "facebook"})
    assert denied.status_code == 403
    assert "meta" not in {p["name"] for p in client.get(
        "/api/v1/publishing/providers", headers=h["anna"]).json()}
    add_member(sessions, "op@a.example.com", "admin", "ws-a", platform_role="admin")
    op = login(client, "op@a.example.com")
    start = client.post("/api/v1/publishing/connections/authorize", headers=op,
                        json={"provider": "meta", "channel": "facebook"}).json()
    assert start["authorize_url"].startswith("/#/publishing/callback?")
    conn = client.post("/api/v1/publishing/oauth/callback", headers=op,
                       json={"state": start["state"], "code": "configured"})
    assert conn.status_code == 200, conn.text
    assert conn.json()["account_name"] == "MGC Developments"

    job_id = drafted(client, runtime, h["alice"])
    send_for_review(client, h["alice"], job_id)
    approve(client, h["max"], job_id, checked=True)
    when = datetime.now(timezone.utc) + timedelta(minutes=1)
    post = client.post(f"/api/v1/jobs/{job_id}/schedule", headers=h["alice"],
                       json={"scheduled_at": when.isoformat(), "timezone": "UTC"}).json()
    asked = client.post(f"/api/v1/scheduled-posts/{post['id']}/publications", headers=h["alice"],
                        json={"connection_id": conn.json()["id"]})
    assert asked.status_code == 201, asked.text

    state = client.app.state
    deliverer = Deliverer(
        sessions, state.providers, state.cipher, now=lambda: when + timedelta(seconds=1),
        media=MediaResolver(state.storage, state.publish_signer, "https://media.example.com"),
        notify=publication_outcome,
    )
    assert deliverer.run_once() == {"published": 1}
    photo = calls[-1]
    assert photo.url.path == "/v24.0/1111/photos" and b"25th of 27 floors" in photo.content
    with sessions() as db:
        pub = db.scalar(select(Publication))
    assert (pub.status, pub.external_url) == ("published", "https://www.facebook.com/1111_7")
    live = [n for n in inbox(client, h["alice"])["items"] if n["kind"] == "publication.published"]
    assert live and live[0]["title"] == "Live on MGC Developments: cat.png"


def test_a_failed_post_tells_the_owner_and_the_admins(team):
    client, sessions, runtime, h, _ = team
    from tests.integration.test_publishing import connect

    w = type("W", (), {"client": client, "h": h})()
    conn = connect(w, behavior="reject", channel="facebook")
    job_id = drafted(client, runtime, h["alice"])
    send_for_review(client, h["alice"], job_id)
    approve(client, h["max"], job_id, checked=True)
    when = datetime.now(timezone.utc) + timedelta(minutes=1)
    post = client.post(f"/api/v1/jobs/{job_id}/schedule", headers=h["alice"],
                       json={"scheduled_at": when.isoformat(), "timezone": "UTC"}).json()
    client.post(f"/api/v1/scheduled-posts/{post['id']}/publications", headers=h["alice"],
                json={"connection_id": conn["id"]})
    state = client.app.state
    Deliverer(sessions, state.providers, state.cipher, now=lambda: when + timedelta(seconds=1),
              notify=publication_outcome).run_once()
    for who in ("alice", "anna"):
        kinds = [n["kind"] for n in inbox(client, h[who])["items"]]
        assert "publication.failed" in kinds, who
    assert "publication.failed" not in [n["kind"] for n in inbox(client, h["max"])["items"]]


def test_a_provider_that_posts_media_stops_when_the_original_is_gone(team):
    client, sessions, runtime, h, _ = team
    from app.publishing.mock import MockProvider

    class NeedsMedia(MockProvider):
        info = type(MockProvider.info)(**{**MockProvider.info.__dict__, "needs_media": True})

    state = client.app.state
    state.providers["mock"] = NeedsMedia(sessions)
    from tests.integration.test_publishing import connect

    conn = connect(type("W", (), {"client": client, "h": h})(), channel="facebook")
    job_id = drafted(client, runtime, h["alice"])
    send_for_review(client, h["alice"], job_id)
    approve(client, h["max"], job_id, checked=True)
    when = datetime.now(timezone.utc) + timedelta(minutes=1)
    post = client.post(f"/api/v1/jobs/{job_id}/schedule", headers=h["alice"],
                       json={"scheduled_at": when.isoformat(), "timezone": "UTC"}).json()
    assert client.delete(f"/api/v1/jobs/{job_id}/media", headers=h["alice"]).status_code == 204
    client.post(f"/api/v1/scheduled-posts/{post['id']}/publications", headers=h["alice"],
                json={"connection_id": conn["id"]})
    out = Deliverer(sessions, state.providers, state.cipher,
                    now=lambda: when + timedelta(seconds=1),
                    media=MediaResolver(state.storage, state.publish_signer, "")).run_once()
    assert out == {"no_media": 1}


def test_a_reviewer_can_open_what_they_were_told_about_but_not_unsent_drafts(team):
    client, sessions, runtime, h, _ = team
    sent = drafted(client, runtime, h["alice"])
    unsent = drafted(client, runtime, h["alice"], (6, 6, 6))
    send_for_review(client, h["alice"], sent)
    [n] = inbox(client, h["max"])["items"]
    opened = client.get(f"/api/v1/jobs/{n['job_id']}", headers=h["max"])
    assert opened.status_code == 200 and opened.json()["submitter_email"] == "alice@a.example.com"
    assert client.get(f"/api/v1/jobs/{unsent}", headers=h["max"]).status_code == 404
    assert client.get(f"/api/v1/jobs/{unsent}", headers=h["anna"]).status_code == 200  # admin
    assert client.get(f"/api/v1/jobs/{sent}", headers=h["bea"]).status_code == 404  # other ws
