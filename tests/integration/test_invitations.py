"""Invitations and self-service membership. See docs/TENANCY.md."""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import update

from app.api import workspaces
from app.api.database import User, WorkspaceInvitation, WorkspaceMember
from app.api.roles import SUPER_ADMIN_EMAIL
from tests.api_harness import PASSWORD, add_member, build_app, login, png, upload
from tests.helpers import ScriptedClient

pytestmark = pytest.mark.integration

NEW_PASSWORD = "chosen-by-the-invitee-123"


@pytest.fixture
def tenants(make_rt, tmp_path, monkeypatch):
    monkeypatch.setattr("app.api.routes.JOB_RESULT_WAIT_S", 0)
    runtime = make_rt(client=ScriptedClient())
    # ALLOW_SIGNUP stays off: invitations are how people join a closed installation.
    application, engine, sessions = build_app(runtime, tmp_path / "media")
    add_member(sessions, "anna@a.example.com", "admin", "ws-a")
    add_member(sessions, "max@a.example.com", "manager", "ws-a")
    add_member(sessions, "alice@a.example.com", "user", "ws-a")
    add_member(sessions, "bea@b.example.com", "admin", "ws-b")
    add_member(sessions, "bob@b.example.com", "user", "ws-b")
    with TestClient(application) as client:
        yield client, sessions, runtime
    engine.dispose()


def invite(client, headers, email, role="user"):
    return client.post("/api/v1/invitations", headers=headers, json={"email": email, "role": role})


def accept(client, token, headers=None, password=None):
    body = {"token": token} if password is None else {"token": token, "password": password}
    return client.post("/api/v1/invitations/accept", headers=headers or {}, json=body)


def lookup(client, token):
    return client.post("/api/v1/invitations/lookup", json={"token": token})


def me(client, access_token, workspace_id=None):
    headers = {"Authorization": f"Bearer {access_token}"}
    if workspace_id:
        headers["X-Workspace-Id"] = workspace_id
    return client.get("/api/v1/auth/me", headers=headers).json()


# ── New people ─────────────────────────────────────────────────────────────────────────────

def test_new_person_accepts_by_choosing_their_own_password(tenants):
    client, sessions, _ = tenants
    anna = login(client, "anna@a.example.com")
    created = invite(client, anna, "New.Person@Example.com", "manager")
    assert created.status_code == 201
    body = created.json()
    assert body["email"] == "new.person@example.com" and body["status"] == "pending"
    assert body["accept_path"] == f"/#/invite/{body['token']}"

    preview = lookup(client, body["token"]).json()
    assert preview == {**preview, "workspace_name": "ws-a", "role": "manager",
                       "account_exists": False}

    accepted = accept(client, body["token"], password=NEW_PASSWORD)
    assert accepted.status_code == 200
    assert accepted.json()["workspace"] == {"id": "ws-a", "name": "ws-a", "role": "manager"}
    profile = me(client, accepted.json()["access_token"])
    assert (profile["email"], profile["workspace"]["id"], profile["role"]) == (
        "new.person@example.com", "ws-a", "manager"
    )
    # ...and the password they chose is the one that works.
    assert client.post("/api/v1/auth/login", json={
        "email": "new.person@example.com", "password": NEW_PASSWORD
    }).status_code == 200


def test_new_account_needs_a_valid_password(tenants):
    client, _, _ = tenants
    token = invite(client, login(client, "anna@a.example.com"), "n@example.com").json()["token"]
    assert accept(client, token).status_code == 422
    assert accept(client, token, password="short").status_code == 422
    assert lookup(client, token).status_code == 200  # still usable


# ── Existing accounts: consent by signing in ──────────────────────────────────────────────

def test_existing_account_joins_a_second_workspace_by_signing_in(tenants):
    client, sessions, _ = tenants
    token = invite(client, login(client, "anna@a.example.com"), "bob@b.example.com").json()["token"]
    assert lookup(client, token).json()["account_exists"] is True

    # Without signing in, nobody can claim an existing account's invitation.
    anonymous = accept(client, token, password="attacker-chosen-password")
    assert anonymous.status_code == 409
    bob = login(client, "bob@b.example.com")
    joined = accept(client, token, headers=bob)
    assert joined.status_code == 200 and joined.json()["workspace"]["id"] == "ws-a"

    profile = me(client, joined.json()["access_token"], "ws-a")
    assert {w["id"] for w in profile["workspaces"]} == {"ws-a", "ws-b"}
    assert profile["workspace"]["role"] == "user"
    # Bob's password is untouched.
    assert login(client, "bob@b.example.com")


def test_signed_in_as_someone_else_cannot_accept(tenants):
    client, _, _ = tenants
    token = invite(client, login(client, "anna@a.example.com"), "bob@b.example.com").json()["token"]
    bea = login(client, "bea@b.example.com")
    refused = accept(client, token, headers=bea)
    assert refused.status_code == 403 and "bob@b.example.com" in refused.json()["detail"]
    assert lookup(client, token).status_code == 200  # still pending for Bob


def test_reserved_super_admin_email_cannot_be_created_by_invitation(tenants):
    client, _, _ = tenants
    token = invite(client, login(client, "anna@a.example.com"), SUPER_ADMIN_EMAIL).json()["token"]
    assert accept(client, token, password=NEW_PASSWORD).status_code == 409


# ── Single use, expiry, revocation ────────────────────────────────────────────────────────

def test_a_link_works_once(tenants):
    client, _, _ = tenants
    token = invite(client, login(client, "anna@a.example.com"), "once@example.com").json()["token"]
    assert accept(client, token, password=NEW_PASSWORD).status_code == 200
    assert accept(client, token, password=NEW_PASSWORD).status_code == 410
    assert lookup(client, token).status_code == 410


def test_an_accept_that_loses_the_race_changes_nothing(tenants, monkeypatch):
    """Two accepts both pass the pending check; the other one commits first. The guarded
    UPDATE matches no row for the loser, so it adds no membership and gets 410."""
    client, sessions, _ = tenants
    token = invite(client, login(client, "anna@a.example.com"), "bob@b.example.com").json()["token"]
    bob = login(client, "bob@b.example.com")
    real = workspaces._by_token

    def checked_then_overtaken(db, token, **kwargs):
        inv = real(db, token, **kwargs)
        with sessions.begin() as other:  # the winning request commits in between
            other.execute(
                update(WorkspaceInvitation)
                .where(WorkspaceInvitation.id == inv.id)
                .values(accepted_at=datetime.now(timezone.utc))
            )
        return inv

    monkeypatch.setattr(workspaces, "_by_token", checked_then_overtaken)
    assert accept(client, token, headers=bob).status_code == 410
    with sessions() as db:
        bob_id = db.query(User).filter_by(email="bob@b.example.com").one().id
        assert db.get(WorkspaceMember, ("ws-a", bob_id)) is None


def test_expired_and_unknown_links(tenants):
    client, sessions, _ = tenants
    created = invite(client, login(client, "anna@a.example.com"), "late@example.com").json()
    with sessions.begin() as db:
        db.execute(
            update(WorkspaceInvitation)
            .values(expires_at=datetime.now(timezone.utc) - timedelta(minutes=1))
        )
    assert lookup(client, created["token"]).status_code == 410
    assert accept(client, created["token"], password=NEW_PASSWORD).status_code == 410
    assert lookup(client, "x" * 43).status_code == 404


def test_expiry_follows_the_configured_ttl(make_rt, tmp_path):
    runtime = make_rt(client=ScriptedClient())
    application, engine, sessions = build_app(
        runtime, tmp_path / "media", invitation_ttl_hours=2
    )
    add_member(sessions, "anna@a.example.com", "admin", "ws-a")
    with TestClient(application) as client:
        created = invite(client, login(client, "anna@a.example.com"), "t@example.com").json()
    engine.dispose()
    lifetime = datetime.fromisoformat(created["expires_at"]) - datetime.fromisoformat(
        created["created_at"]
    )
    assert lifetime == timedelta(hours=2)


def test_revoked_links_stop_working_and_leave_the_pending_list(tenants):
    client, _, _ = tenants
    anna = login(client, "anna@a.example.com")
    created = invite(client, anna, "gone@example.com").json()
    assert [i["email"] for i in client.get("/api/v1/invitations", headers=anna).json()] == [
        "gone@example.com"
    ]
    assert client.delete(f"/api/v1/invitations/{created['id']}", headers=anna).status_code == 204
    assert accept(client, created["token"], password=NEW_PASSWORD).status_code == 410
    assert client.get("/api/v1/invitations", headers=anna).json() == []


def test_inviting_again_replaces_the_earlier_link(tenants):
    client, _, _ = tenants
    anna = login(client, "anna@a.example.com")
    first = invite(client, anna, "again@example.com").json()
    second = invite(client, anna, "again@example.com", "manager").json()
    assert lookup(client, first["token"]).status_code == 410
    assert lookup(client, second["token"]).json()["role"] == "manager"
    assert len(client.get("/api/v1/invitations", headers=anna).json()) == 1


def test_accepted_invitation_cannot_be_revoked(tenants):
    client, _, _ = tenants
    anna = login(client, "anna@a.example.com")
    created = invite(client, anna, "kept@example.com").json()
    accept(client, created["token"], password=NEW_PASSWORD)
    assert client.delete(f"/api/v1/invitations/{created['id']}", headers=anna).status_code == 409


def test_tokens_are_stored_only_as_hashes(tenants):
    client, sessions, _ = tenants
    token = invite(client, login(client, "anna@a.example.com"), "h@example.com").json()["token"]
    with sessions() as db:
        row = db.query(WorkspaceInvitation).one()
        assert token not in {str(value) for value in vars(row).values()}
        assert len(row.token_sha256) == 64


# ── Who may invite ────────────────────────────────────────────────────────────────────────

def test_roles_limit_who_can_invite_whom(tenants):
    client, _, _ = tenants
    anna, max_, alice = (login(client, f"{n}@a.example.com") for n in ("anna", "max", "alice"))
    assert invite(client, alice, "x@example.com").status_code == 403
    assert client.get("/api/v1/invitations", headers=alice).status_code == 403
    assert invite(client, max_, "m@example.com", "manager").status_code == 201
    assert invite(client, max_, "a@example.com", "admin").status_code == 403

    admin_invite = invite(client, anna, "boss@example.com", "admin").json()
    assert client.delete(
        f"/api/v1/invitations/{admin_invite['id']}", headers=max_
    ).status_code == 403


def test_inviting_a_current_member_is_refused(tenants):
    client, _, _ = tenants
    assert invite(
        client, login(client, "anna@a.example.com"), "ALICE@a.example.com"
    ).status_code == 409


def test_invitations_are_invisible_across_workspaces(tenants):
    client, _, _ = tenants
    created = invite(client, login(client, "anna@a.example.com"), "p@example.com").json()
    bea = login(client, "bea@b.example.com")
    assert client.get("/api/v1/invitations", headers=bea).json() == []
    assert client.delete(f"/api/v1/invitations/{created['id']}", headers=bea).status_code == 404
    assert lookup(client, created["token"]).status_code == 200


def test_creating_an_account_that_exists_points_to_invitations(tenants):
    client, _, _ = tenants
    response = client.post("/api/v1/users", headers=login(client, "anna@a.example.com"),
                           json={"email": "bob@b.example.com", "password": PASSWORD})
    assert response.status_code == 409 and "Invite them" in response.json()["detail"]


# ── Leaving and renaming ──────────────────────────────────────────────────────────────────

def joined_both(client, email="bob@b.example.com"):
    token = invite(client, login(client, "anna@a.example.com"), email).json()["token"]
    accept(client, token, headers=login(client, email))


def test_leaving_keeps_the_work_with_the_workspace(tenants):
    client, sessions, runtime = tenants
    joined_both(client)
    bob_in_a = login(client, "bob@b.example.com", "ws-a")
    job_id = upload(client, bob_in_a, png()).json()["job_id"]
    runtime.worker.drain()

    assert client.post("/api/v1/workspaces/ws-a/leave", headers=bob_in_a).status_code == 204
    assert client.get("/api/v1/auth/me", headers=bob_in_a).status_code == 403
    assert [w["id"] for w in client.get("/api/v1/workspaces", headers=bob_in_a).json()] == [
        "ws-b"
    ]
    anna = login(client, "anna@a.example.com")
    assert client.get(f"/api/v1/jobs/{job_id}", headers=anna).status_code == 200


def test_cannot_leave_your_only_workspace_or_as_its_last_admin(tenants):
    client, _, _ = tenants
    alice = login(client, "alice@a.example.com")
    assert client.post("/api/v1/workspaces/ws-a/leave", headers=alice).status_code == 409

    joined_both(client, "bea@b.example.com")  # Bea is in both, and B's only admin
    bea = login(client, "bea@b.example.com")
    refused = client.post("/api/v1/workspaces/ws-b/leave", headers=bea)
    assert refused.status_code == 409 and "only admin" in refused.json()["detail"]
    assert client.post("/api/v1/workspaces/ws-a/leave", headers=bea).status_code == 204


def test_only_a_workspaces_admin_can_rename_it(tenants):
    client, sessions, _ = tenants
    rename = {"name": "  Acme Social  "}
    max_ = login(client, "max@a.example.com")
    assert client.patch("/api/v1/workspaces/ws-a", headers=max_, json=rename).status_code == 403
    bea = login(client, "bea@b.example.com")
    assert client.patch("/api/v1/workspaces/ws-a", headers=bea, json=rename).status_code == 404

    anna = login(client, "anna@a.example.com")
    renamed = client.patch("/api/v1/workspaces/ws-a", headers=anna, json=rename)
    assert renamed.json() == {"id": "ws-a", "name": "Acme Social", "role": "admin"}
    assert client.get("/api/v1/auth/me", headers=anna).json()["workspace"]["name"] == "Acme Social"


def test_deleting_the_inviter_keeps_their_invitations_working(tenants):
    client, sessions, _ = tenants
    max_ = login(client, "max@a.example.com")
    token = invite(client, max_, "kept@example.com").json()["token"]
    with sessions() as db:
        max_id = db.query(User).filter_by(email="max@a.example.com").one().id
    anna = login(client, "anna@a.example.com")
    assert client.delete(f"/api/v1/users/{max_id}", headers=anna).status_code == 204
    assert accept(client, token, password=NEW_PASSWORD).status_code == 200
