"""Workspace boundary: nothing in one tenant is visible to, or changeable from, another."""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.api.database import ApiJob, User, WorkspaceMember
from tests.api_harness import add_member, build_app, login, png, send_for_review, upload
from tests.helpers import ScriptedClient

pytestmark = pytest.mark.integration


@pytest.fixture
def tenants(make_rt, tmp_path, monkeypatch):
    monkeypatch.setattr("app.api.routes.JOB_RESULT_WAIT_S", 0)
    gemini = ScriptedClient()
    runtime = make_rt(client=gemini)
    application, engine, sessions = build_app(runtime, tmp_path / "media")
    add_member(sessions, "alice@a.example.com", "user", "ws-a")
    add_member(sessions, "anna@a.example.com", "admin", "ws-a")
    add_member(sessions, "bob@b.example.com", "user", "ws-b")
    add_member(sessions, "bea@b.example.com", "admin", "ws-b")
    with TestClient(application) as client:
        yield client, runtime, sessions, gemini
    engine.dispose()


def test_jobs_media_review_and_lists_are_invisible_across_workspaces(tenants):
    client, runtime, _, _ = tenants
    alice, anna = login(client, "alice@a.example.com"), login(client, "anna@a.example.com")
    bob, bea = login(client, "bob@b.example.com"), login(client, "bea@b.example.com")

    job_id = upload(client, alice, png()).json()["job_id"]
    runtime.worker.drain()

    # Workspace B, even its admin, cannot see or touch workspace A's job in any way.
    for headers in (bob, bea):
        assert client.get(f"/api/v1/jobs/{job_id}", headers=headers).status_code == 404
        assert client.get(f"/api/v1/jobs/{job_id}/media", headers=headers).status_code == 404
        assert client.delete(f"/api/v1/jobs/{job_id}", headers=headers).status_code == 404
    assert client.patch(
        f"/api/v1/jobs/{job_id}/approval", headers=bea, json={"status": "approved", "checked": True}
    ).status_code == 404
    for path in ("/jobs", "/jobs/review", "/jobs/approved", "/jobs/mine"):
        assert client.get(f"/api/v1{path}", headers=bea).json()["items"] == []

    # Workspace B can't send it for review either; its uploader can.
    assert client.post(f"/api/v1/jobs/{job_id}/submit", headers=bea).status_code == 404
    send_for_review(client, alice, job_id)

    # Workspace A's admin sees it and can approve it; the owner can schedule it.
    assert [j["job_id"] for j in client.get("/api/v1/jobs/review", headers=anna).json()["items"]
            ] == [job_id]
    assert client.patch(
        f"/api/v1/jobs/{job_id}/approval", headers=anna,
        json={"status": "approved", "checked": True},
    ).status_code == 200
    when = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    post = client.post(
        f"/api/v1/jobs/{job_id}/schedule", headers=alice,
        json={"scheduled_at": when, "timezone": "UTC"},
    ).json()
    day = post["local_date"]
    calendar = {"start": day, "end": day, "timezone": "UTC"}
    assert client.get("/api/v1/calendar", headers=anna, params=calendar).json()["days"][0][
        "posts"
    ]
    assert client.get("/api/v1/calendar", headers=bea, params=calendar).json()["days"][0][
        "posts"
    ] == []
    for method in ("get", "delete"):
        response = getattr(client, method)(f"/api/v1/scheduled-posts/{post['id']}", headers=bea)
        assert response.status_code == 404


def test_identical_media_in_two_workspaces_never_shares_a_result(tenants):
    client, runtime, _, gemini = tenants
    alice, bob = login(client, "alice@a.example.com"), login(client, "bob@b.example.com")
    image = png((9, 9, 9))

    first = upload(client, alice, image).json()
    runtime.worker.drain()
    second = upload(client, bob, image).json()
    # Same bytes, other tenant: queued for its own analysis, not "cached" - B learns nothing.
    assert second["status"] == "queued" and second["cached"] is False
    runtime.worker.drain()
    assert len(gemini.calls) == 2
    assert runtime.store.get(first["job_id"]).cache_key != runtime.store.get(
        second["job_id"]
    ).cache_key

    # Within one workspace dedup still works.
    again = upload(client, alice, image).json()
    assert again["status"] == "done" and again["cached"] is True
    assert len(gemini.calls) == 2


def test_users_api_only_reaches_members_of_the_callers_workspace(tenants):
    client, _, sessions, _ = tenants
    anna, bea = login(client, "anna@a.example.com"), login(client, "bea@b.example.com")
    with sessions() as db:
        alice_id = db.query(User).filter_by(email="alice@a.example.com").one().id

    emails = {u["email"] for u in client.get("/api/v1/users", headers=bea).json()["items"]}
    assert emails == {"bob@b.example.com", "bea@b.example.com"}
    assert client.get(f"/api/v1/users/{alice_id}", headers=bea).status_code == 404
    assert client.patch(
        f"/api/v1/users/{alice_id}", headers=bea, json={"password": "x" * 20}
    ).status_code == 404
    assert client.patch(
        f"/api/v1/users/{alice_id}/role", headers=bea, json={"role": "admin"}
    ).status_code == 404
    assert client.delete(f"/api/v1/users/{alice_id}", headers=bea).status_code == 404

    created = client.post(
        "/api/v1/users", headers=anna,
        json={"email": "new@a.example.com", "password": "a-very-long-test-password"},
    )
    assert created.status_code == 201
    with sessions() as db:
        member = db.query(WorkspaceMember).filter_by(user_id=created.json()["id"]).one()
        assert (member.workspace_id, member.role) == ("ws-a", "user")


def test_member_of_two_workspaces_switches_with_header(tenants):
    client, runtime, sessions, _ = tenants
    add_member(sessions, "alice@a.example.com", "manager", "ws-b")
    headers = login(client, "alice@a.example.com")

    me = client.get("/api/v1/auth/me", headers=headers).json()
    assert me["workspace"]["id"] == "ws-a" and me["role"] == "user"  # oldest membership
    assert {(w["id"], w["role"]) for w in me["workspaces"]} == {
        ("ws-a", "user"), ("ws-b", "manager")
    }
    in_b = {**headers, "X-Workspace-Id": "ws-b"}
    assert client.get("/api/v1/auth/me", headers=in_b).json()["role"] == "manager"
    # Role is per workspace: a manager in B, but only a member in A.
    assert client.get("/api/v1/jobs/review", headers=in_b).status_code == 200
    assert client.get("/api/v1/jobs/review", headers=headers).status_code == 403

    job_b = upload(client, in_b, png((4, 4, 4))).json()["job_id"]
    with sessions() as db:
        assert db.get(ApiJob, job_b).workspace_id == "ws-b"
    assert client.get(f"/api/v1/jobs/{job_b}", headers=headers).status_code == 404
    assert client.get(f"/api/v1/jobs/{job_b}", headers=in_b).status_code == 200

    outsider = {**headers, "X-Workspace-Id": "ws-nope"}
    response = client.get("/api/v1/jobs/mine", headers=outsider)
    assert response.status_code == 403
    assert response.json()["detail"] == "You are not a member of this workspace"


def test_shared_accounts_are_protected_from_either_tenant(tenants):
    client, runtime, sessions, _ = tenants
    shared = add_member(sessions, "shared@example.com", "user", "ws-a")
    add_member(sessions, "shared@example.com", "user", "ws-b")
    anna = login(client, "anna@a.example.com")

    # A's admin must not be able to take over an account that also works in B.
    for change in ({"password": "taken-over-password"}, {"email": "mine@example.com"}):
        response = client.patch(f"/api/v1/users/{shared}", headers=anna, json=change)
        assert response.status_code == 409
    assert client.patch(
        f"/api/v1/users/{shared}", headers=anna, json={"role": "manager"}
    ).json()["role"] == "manager"

    # Removing them from A keeps the account (and their B membership) intact.
    upload(client, login(client, "shared@example.com"), png((7, 7, 7)))
    assert client.delete(f"/api/v1/users/{shared}", headers=anna).status_code == 204
    assert runtime.queue.depths()["ready"] == 0  # their queued job in A was cancelled
    with sessions() as db:
        assert db.get(User, shared) is not None
        assert [m.workspace_id for m in db.query(WorkspaceMember).filter_by(user_id=shared)] == [
            "ws-b"
        ]
        assert db.query(ApiJob).filter_by(owner_id=shared).count() == 0
    assert client.get("/api/v1/auth/me", headers=login(client, "shared@example.com")).json()[
        "workspace"
    ]["id"] == "ws-b"


def test_last_workspace_admin_cannot_be_removed_or_demoted(tenants):
    client, _, sessions, _ = tenants
    anna = login(client, "anna@a.example.com")
    with sessions() as db:
        anna_id = db.query(User).filter_by(email="anna@a.example.com").one().id
    assert client.delete(f"/api/v1/users/{anna_id}", headers=anna).status_code == 409
    assert client.patch(
        f"/api/v1/users/{anna_id}/role", headers=anna, json={"role": "user"}
    ).status_code == 409


def test_stats_are_workspace_scoped_and_fleet_numbers_need_platform_admin(tenants):
    client, runtime, sessions, _ = tenants
    add_member(sessions, "ops@example.com", "admin", "ws-b", platform_role="admin")
    upload(client, login(client, "alice@a.example.com"), png((1, 1, 1)))
    upload(client, login(client, "alice@a.example.com"), png((2, 2, 2)))

    a_stats = client.get("/api/v1/stats", headers=login(client, "anna@a.example.com")).json()
    assert a_stats["ready"] == 2 and a_stats["fleet"] is None
    assert a_stats["gemini_calls_today"] is None
    b_stats = client.get("/api/v1/stats", headers=login(client, "bea@b.example.com")).json()
    assert b_stats["ready"] == 0
    ops = client.get("/api/v1/stats", headers=login(client, "ops@example.com")).json()
    assert ops["ready"] == 0 and ops["fleet"]["ready"] == 2
    assert client.get(
        "/api/v1/stats", headers=login(client, "bob@b.example.com")
    ).status_code == 403


def test_signup_creates_a_new_workspace_only_when_enabled(make_rt, tmp_path):
    for allow in (False, True):
        application, engine, sessions = build_app(
            make_rt(), tmp_path / f"m{allow}", allow_signup=allow
        )
        body = {"email": "founder@example.com", "password": "a-very-long-test-password",
                "workspace_name": "  Acme Social  "}
        try:
            with TestClient(application) as client:
                response = client.post("/api/v1/auth/signup", json=body)
                assert client.get("/api/v1/meta").json()["allow_signup"] is allow
                if not allow:
                    assert response.status_code == 404
                    continue
                assert response.status_code == 201, response.text
                headers = {"Authorization": f"Bearer {response.json()['access_token']}"}
                me = client.get("/api/v1/auth/me", headers=headers).json()
                assert me["workspace"]["name"] == "Acme Social" and me["role"] == "admin"
                assert client.post("/api/v1/auth/signup", json=body).status_code == 409

                second = client.post("/api/v1/workspaces", headers=headers,
                                     json={"name": "Side"})
                assert second.status_code == 201 and second.json()["role"] == "admin"
                assert len(client.get("/api/v1/workspaces", headers=headers).json()) == 2
        finally:
            engine.dispose()  # also on `continue` and on a failed assertion


def test_workspace_creation_is_for_platform_admins_when_signup_is_closed(tenants):
    client, _, sessions, _ = tenants
    add_member(sessions, "ops@example.com", "admin", "ws-a", platform_role="admin")
    assert client.post(
        "/api/v1/workspaces", headers=login(client, "anna@a.example.com"), json={"name": "X"}
    ).status_code == 403
    assert client.post(
        "/api/v1/workspaces", headers=login(client, "ops@example.com"), json={"name": "X"}
    ).status_code == 201


def test_account_without_a_workspace_gets_a_clear_error(tenants):
    client, _, sessions, _ = tenants
    with sessions.begin() as db:
        user = db.query(User).filter_by(email="bob@b.example.com").one()
        db.query(WorkspaceMember).filter_by(user_id=user.id).delete()
    response = client.get("/api/v1/auth/me", headers=login(client, "bob@b.example.com"))
    assert response.status_code == 403
    assert response.json()["detail"] == "Your account does not belong to any workspace"
