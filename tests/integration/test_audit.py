"""Authorization audit trail. See docs/AUDIT.md."""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.api import admin, audit
from app.api.database import AuditEvent, Base, User, WorkspaceMember, create_database
from tests.api_harness import add_member, build_app, login, png, upload
from tests.helpers import ScriptedClient

pytestmark = pytest.mark.integration


@pytest.fixture
def team(make_rt, tmp_path, monkeypatch):
    monkeypatch.setattr("app.api.routes.JOB_RESULT_WAIT_S", 0)
    runtime = make_rt(client=ScriptedClient())
    application, engine, sessions = build_app(runtime, tmp_path / "media", allow_signup=True)
    ids = {
        "anna": add_member(sessions, "anna@a.example.com", "admin", "ws-a"),
        "max": add_member(sessions, "max@a.example.com", "manager", "ws-a"),
        "alice": add_member(sessions, "alice@a.example.com", "user", "ws-a"),
        "bea": add_member(sessions, "bea@b.example.com", "admin", "ws-b"),
    }
    with TestClient(application) as client:
        yield client, sessions, runtime, ids
    engine.dispose()


def events(sessions, workspace_id="ws-a") -> list[AuditEvent]:
    with sessions() as db:
        return db.scalars(
            select(AuditEvent).where(AuditEvent.workspace_id == workspace_id)
            .order_by(AuditEvent.id)
        ).all()


def actions(sessions, workspace_id="ws-a") -> list[str]:
    return [e.action for e in events(sessions, workspace_id)]


def later(days=1):
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()


def approved_job(client, runtime, alice, max_):
    job_id = upload(client, alice, png()).json()["job_id"]
    runtime.worker.drain()
    client.patch(f"/api/v1/jobs/{job_id}/approval", headers=max_, json={"status": "approved"})
    return job_id


# ── What is recorded ──────────────────────────────────────────────────────────────────────

def test_role_change_records_who_whom_before_and_after(team):
    client, sessions, _, ids = team
    anna = login(client, "anna@a.example.com")
    response = client.patch(f"/api/v1/users/{ids['alice']}/role", headers=anna,
                            json={"role": "manager"})
    assert response.status_code == 200
    [event] = events(sessions)
    assert (event.action, event.actor_id, event.actor_email) == (
        "member.role_changed", ids["anna"], "anna@a.example.com"
    )
    assert (event.target_type, event.target_id, event.target_label) == (
        "user", ids["alice"], "alice@a.example.com"
    )
    assert (event.before, event.after) == ({"role": "user"}, {"role": "manager"})
    # The event joins the logs and traces of the request that made it.
    assert event.request_id == response.headers["X-Request-ID"]


def test_account_changes_by_an_admin_are_each_recorded(team):
    client, sessions, _, ids = team
    anna = login(client, "anna@a.example.com")
    client.patch(f"/api/v1/users/{ids['alice']}", headers=anna, json={
        "email": "alice2@a.example.com", "password": "a-new-long-password", "role": "admin",
    })
    by_action = {e.action: e for e in events(sessions)}
    assert set(by_action) == {"member.email_changed", "member.password_reset",
                              "member.role_changed"}
    assert by_action["member.email_changed"].before == {"email": "alice@a.example.com"}
    assert by_action["member.password_reset"].after is None  # never the password itself


def test_review_decisions_are_recorded_with_the_reviewer(team):
    client, sessions, runtime, ids = team
    alice, max_ = login(client, "alice@a.example.com"), login(client, "max@a.example.com")
    approved = approved_job(client, runtime, alice, max_)
    rejected = upload(client, alice, png((9, 9, 9))).json()["job_id"]
    runtime.worker.drain()
    client.patch(f"/api/v1/jobs/{rejected}/approval", headers=max_, json={"status": "rejected"})

    decisions = [(e.action, e.target_id, e.actor_id, e.after["owner"])
                 for e in events(sessions) if e.action.startswith("job.")]
    assert decisions == [
        ("job.approved", approved, ids["max"], "alice@a.example.com"),
        ("job.rejected", rejected, ids["max"], "alice@a.example.com"),
    ]


def test_calendar_changes_record_the_old_and_new_time(team):
    client, sessions, runtime, ids = team
    alice, max_ = login(client, "alice@a.example.com"), login(client, "max@a.example.com")
    anna = login(client, "anna@a.example.com")
    job_id = approved_job(client, runtime, alice, max_)
    post = client.post(f"/api/v1/jobs/{job_id}/schedule", headers=alice,
                       json={"scheduled_at": later(1), "timezone": "UTC"}).json()
    # An admin moves someone else's post: the trail shows who really did it.
    client.patch(f"/api/v1/scheduled-posts/{post['id']}", headers=anna,
                 json={"scheduled_at": later(3), "timezone": "Europe/Berlin"})
    client.delete(f"/api/v1/scheduled-posts/{post['id']}", headers=anna)
    client.delete(f"/api/v1/scheduled-posts/{post['id']}", headers=anna)  # no-op: not recorded

    trail = [e for e in events(sessions) if e.action.startswith("post.")]
    assert [(e.action, e.actor_id) for e in trail] == [
        ("post.scheduled", ids["alice"]),
        ("post.rescheduled", ids["anna"]),
        ("post.cancelled", ids["anna"]),
    ]
    moved = trail[1]
    assert moved.target_id == post["id"] and moved.target_label == "cat.png"
    assert (moved.before["timezone"], moved.after["timezone"]) == ("UTC", "Europe/Berlin")
    assert moved.before["scheduled_at"] != moved.after["scheduled_at"]


def test_cancelling_a_job_is_recorded(team):
    client, sessions, _, ids = team
    alice = login(client, "alice@a.example.com")
    job_id = upload(client, alice, png()).json()["job_id"]
    assert client.delete(f"/api/v1/jobs/{job_id}", headers=alice).status_code == 200
    [event] = events(sessions)
    assert (event.action, event.target_id, event.before, event.after) == (
        "job.cancelled", job_id, {"status": "queued"}, {"status": "cancelled"}
    )


def test_membership_lifecycle_is_recorded(team):
    client, sessions, _, ids = team
    anna = login(client, "anna@a.example.com")
    client.post("/api/v1/users", headers=anna,
                json={"email": "direct@a.example.com", "password": "a-very-long-password"})
    revoked = client.post("/api/v1/invitations", headers=anna,
                          json={"email": "nope@example.com"}).json()
    client.delete(f"/api/v1/invitations/{revoked['id']}", headers=anna)
    invited = client.post("/api/v1/invitations", headers=anna,
                          json={"email": "bea@b.example.com", "role": "manager"}).json()
    bea = login(client, "bea@b.example.com")
    client.post("/api/v1/invitations/accept", headers=bea, json={"token": invited["token"]})
    client.post("/api/v1/workspaces/ws-a/leave", headers=bea)
    client.patch("/api/v1/workspaces/ws-a", headers=anna, json={"name": "Acme"})

    assert actions(sessions) == [
        "member.created", "invitation.created", "invitation.revoked", "invitation.created",
        "member.joined", "member.left", "workspace.renamed",
    ]
    joined = next(e for e in events(sessions) if e.action == "member.joined")
    assert joined.actor_email == "bea@b.example.com"  # the invitee acted, not the inviter
    assert joined.after == {"role": "manager", "invitation_id": invited["id"],
                            "new_account": False}
    assert events(sessions, "ws-b") == []  # Bea's own workspace hears nothing


def test_history_survives_deleting_the_people_in_it(team):
    client, sessions, _, ids = team
    anna = login(client, "anna@a.example.com")
    client.patch(f"/api/v1/users/{ids['alice']}/role", headers=anna, json={"role": "manager"})
    assert client.delete(f"/api/v1/users/{ids['alice']}", headers=anna).status_code == 204
    with sessions() as db:
        assert db.get(User, ids["alice"]) is None
    removed = events(sessions)[-1]
    assert (removed.action, removed.target_label, removed.before) == (
        "member.removed", "alice@a.example.com", {"role": "manager"}
    )
    assert removed.after == {"jobs_deleted": 0}
    assert len(events(sessions)) == 2


def test_signup_and_new_workspaces_record_their_creation(team):
    client, sessions, _, ids = team
    token = client.post("/api/v1/auth/signup", json={
        "email": "founder@example.com", "password": "a-very-long-password",
        "workspace_name": "Founders",
    }).json()["access_token"]
    created = client.post("/api/v1/workspaces", headers={"Authorization": f"Bearer {token}"},
                          json={"name": "Second"}).json()
    with sessions() as db:
        trail = db.scalars(select(AuditEvent).where(AuditEvent.action == "workspace.created")
                           .order_by(AuditEvent.id)).all()
    assert [(e.target_label, e.actor_email) for e in trail] == [
        ("Founders", "founder@example.com"), ("Second", "founder@example.com"),
    ]
    assert trail[1].workspace_id == created["id"]


def test_admin_cli_changes_are_audited_as_the_system(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'cli.db'}"
    engine, sessions = create_database(url)
    Base.metadata.create_all(engine)
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("DEMO_MODE", "1")
    monkeypatch.setattr("app.api.admin.load_dotenv", lambda *a, **k: False)
    monkeypatch.setattr("getpass.getpass", lambda prompt="": "a-very-long-admin-password")
    assert admin.main(["ops@example.com"]) == 0
    assert admin.main(["ops@example.com"]) == 0  # second run: a password reset
    with sessions() as db:
        trail = db.scalars(select(AuditEvent).order_by(AuditEvent.id)).all()
    engine.dispose()
    assert [(e.action, e.actor_email, e.actor_id) for e in trail] == [
        ("member.created", audit.SYSTEM_ACTOR, None),
        ("member.password_reset", audit.SYSTEM_ACTOR, None),
    ]


# ── What is not recorded ──────────────────────────────────────────────────────────────────

def test_refused_and_no_op_requests_leave_no_trace(team):
    client, sessions, runtime, ids = team
    alice, max_ = login(client, "alice@a.example.com"), login(client, "max@a.example.com")
    anna = login(client, "anna@a.example.com")
    assert client.patch(f"/api/v1/users/{ids['anna']}/role", headers=alice,
                        json={"role": "user"}).status_code == 403
    assert client.patch(f"/api/v1/users/{ids['anna']}/role", headers=anna,
                        json={"role": "user"}).status_code == 409  # last admin
    assert client.patch(f"/api/v1/users/{ids['alice']}/role", headers=anna,
                        json={"role": "user"}).status_code == 200  # already a user
    job_id = upload(client, alice, png()).json()["job_id"]
    assert client.patch(f"/api/v1/jobs/{job_id}/approval", headers=max_,
                        json={"status": "approved"}).status_code == 409  # not done yet
    assert events(sessions) == []


def test_a_change_and_its_audit_event_commit_together(team, monkeypatch):
    """If the audit row can't be written, the change doesn't happen either."""
    client, sessions, _, ids = team
    anna = login(client, "anna@a.example.com")

    def broken(*args, **kwargs):
        raise RuntimeError("audit store unavailable")

    monkeypatch.setattr(audit, "record", broken)
    with pytest.raises(RuntimeError):
        client.patch(f"/api/v1/users/{ids['alice']}/role", headers=anna, json={"role": "admin"})
    with sessions() as db:
        assert db.get(WorkspaceMember, ("ws-a", ids["alice"])).role == "user"
    assert events(sessions) == []


# ── Reading the trail ─────────────────────────────────────────────────────────────────────

def test_only_admins_read_the_trail_and_only_their_workspaces(team):
    client, sessions, _, ids = team
    anna, bea = login(client, "anna@a.example.com"), login(client, "bea@b.example.com")
    client.patch(f"/api/v1/users/{ids['alice']}/role", headers=anna, json={"role": "manager"})
    assert client.get("/api/v1/audit-events", headers=login(
        client, "max@a.example.com")).status_code == 403
    assert client.get("/api/v1/audit-events", headers=bea).json()["items"] == []
    page = client.get("/api/v1/audit-events", headers=anna).json()
    assert [e["action"] for e in page["items"]] == ["member.role_changed"]


def test_trail_filters_and_pages_newest_first(team):
    client, sessions, runtime, ids = team
    anna = login(client, "anna@a.example.com")
    for role in ("manager", "admin", "user", "manager"):
        client.patch(f"/api/v1/users/{ids['alice']}/role", headers=anna, json={"role": role})
    client.post("/api/v1/invitations", headers=anna, json={"email": "x@example.com"})

    first = client.get("/api/v1/audit-events?category=member&limit=3", headers=anna).json()
    assert [e["after"]["role"] for e in first["items"]] == ["manager", "user", "admin"]
    rest = client.get(f"/api/v1/audit-events?category=member&before={first['next_before']}",
                      headers=anna).json()
    assert [e["after"]["role"] for e in rest["items"]] == ["manager"]
    assert rest["next_before"] is None

    by_target = client.get(f"/api/v1/audit-events?target_id={ids['alice']}",
                           headers=anna).json()["items"]
    assert len(by_target) == 4
    by_actor = client.get(f"/api/v1/audit-events?actor_id={ids['anna']}&action="
                          "invitation.created", headers=anna).json()["items"]
    assert [e["target_label"] for e in by_actor] == ["x@example.com"]
    assert client.get("/api/v1/audit-events?action=member.deleted",
                      headers=anna).status_code == 422
    assert client.get("/api/v1/audit-events?category=billing",
                      headers=anna).status_code == 422
