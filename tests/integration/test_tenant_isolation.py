"""Proof that nobody can cross a workspace boundary, route by route.

Workspace A holds jobs in every state, a calendar entry, a pending invitation and audit
history. Three attackers live in workspace B: its admin, a platform admin who is a member of B
only, and a plain member. For every route that takes a resource id, each attacker sends A's
real ids with a payload that is valid for the route, once normally and once with
`X-Workspace-Id: ws-a`. Every attempt must be refused (403/404), must not leak A's data in the
response, and must leave A's database rows, accounts and Redis state exactly as they were.
Then A's legitimate user sends the *same* request and it succeeds, which proves the refusal came
from the tenant boundary, not from a bad payload.

`test_every_route_is_classified` fails when a route is added without being placed here, so the
proof can't silently fall behind the API.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.api.database import (
    ApiJob,
    AuditEvent,
    DraftComment,
    DraftVersion,
    MediaAsset,
    Publication,
    PublishingConnection,
    ScheduledPost,
    User,
    Workspace,
    WorkspaceInvitation,
    WorkspaceMember,
)
from tests.api_harness import add_member, build_app, login, png, send_for_review, upload
from tests.helpers import ScriptedClient

pytestmark = pytest.mark.integration

LATER = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
SCHEDULE = {"scheduled_at": LATER, "timezone": "UTC"}
A_TABLES = (ApiJob, ScheduledPost, WorkspaceMember, WorkspaceInvitation, AuditEvent,
            DraftVersion, DraftComment, PublishingConnection, Publication)


@dataclass
class World:
    client: TestClient
    sessions: object
    runtime: object
    ids: dict[str, str] = field(default_factory=dict)
    headers: dict[str, dict] = field(default_factory=dict)
    account_names: list[str] = field(default_factory=list)

    def as_(self, who: str, workspace: str | None = None) -> dict:
        headers = dict(self.headers[who])
        if workspace:
            headers["X-Workspace-Id"] = workspace
        return headers

    def markers(self) -> set[str]:
        """Strings that only workspace A's data contains."""
        return {
            *(self.ids[k] for k in ("approved", "pending", "unsent", "queued", "post", "invitation",
                                    "comment", "anna", "alice", "carl", "conn", "conn_spare",
                                    "pub_queued", "pub_failed")),
            *self.account_names,
            "anna@a.example.com", "alice@a.example.com", "carl@a.example.com",
            "invitee@example.com", "secret-a.png", "secret-comment",
        }


@pytest.fixture
def world(make_rt, tmp_path, monkeypatch):
    monkeypatch.setattr("app.api.routes.JOB_RESULT_WAIT_S", 0)
    runtime = make_rt(client=ScriptedClient())
    application, engine, sessions = build_app(runtime, tmp_path / "media")
    w = World(None, sessions, runtime)
    w.ids["anna"] = add_member(sessions, "anna@a.example.com", "admin", "ws-a")
    w.ids["alice"] = add_member(sessions, "alice@a.example.com", "user", "ws-a")
    w.ids["carl"] = add_member(sessions, "carl@a.example.com", "user", "ws-a")
    add_member(sessions, "carl@a.example.com", "user", "ws-c")  # can leave A
    add_member(sessions, "bea@b.example.com", "admin", "ws-b")
    add_member(sessions, "bob@b.example.com", "user", "ws-b")
    add_member(sessions, "pat@b.example.com", "user", "ws-b", platform_role="admin")

    with TestClient(application) as client:
        w.client = client
        for who in ("anna", "alice", "carl", "bea", "bob", "pat"):
            domain = "a" if who in ("anna", "alice", "carl") else "b"
            w.headers[who] = login(client, f"{who}@{domain}.example.com")
        alice, anna = w.as_("alice", "ws-a"), w.as_("anna")

        w.ids["approved"] = upload(client, alice, png((1, 0, 0)), name="secret-a.png").json()[
            "job_id"]
        w.ids["pending"] = upload(client, alice, png((2, 0, 0)), name="secret-a.png").json()[
            "job_id"]
        w.ids["unsent"] = upload(client, alice, png((5, 0, 0)), name="secret-a.png").json()[
            "job_id"]  # drafted but not yet sent for review by its uploader
        runtime.worker.drain()
        for key in ("approved", "pending"):
            send_for_review(client, alice, w.ids[key])
        client.patch(f"/api/v1/jobs/{w.ids['approved']}/approval", headers=anna,
                     json={"status": "approved", "checked": True})
        w.ids["post"] = client.post(f"/api/v1/jobs/{w.ids['approved']}/schedule",
                                    headers=alice, json=SCHEDULE).json()["id"]
        w.ids["schedulable"] = upload(client, alice, png((3, 0, 0)),
                                      name="secret-a.png").json()["job_id"]
        runtime.worker.drain()
        send_for_review(client, alice, w.ids["schedulable"])
        client.patch(f"/api/v1/jobs/{w.ids['schedulable']}/approval", headers=anna,
                     json={"status": "approved", "checked": True})
        w.ids["queued"] = upload(client, alice, png((4, 0, 0)), name="secret-a.png").json()[
            "job_id"]  # never drained: still cancellable
        w.ids["invitation"] = client.post("/api/v1/invitations", headers=anna,
                                          json={"email": "invitee@example.com"}).json()["id"]
        w.ids["comment"] = client.post(f"/api/v1/jobs/{w.ids['pending']}/comments",
                                       headers=anna, json={"body": "secret-comment"}).json()["id"]
        w.ids["workspace"] = "ws-a"
        # Publishing: three sandbox accounts in A, two deliveries of A's scheduled post.
        for key in ("conn", "conn_spare", "conn_failed"):
            conn = sandbox_connection(client, anna)
            w.ids[key] = conn["id"]
            w.account_names.append(conn["account_name"])
        for key, conn in (("pub_queued", "conn"), ("pub_failed", "conn_failed")):
            w.ids[key] = client.post(
                f"/api/v1/scheduled-posts/{w.ids['post']}/publications", headers=alice,
                json={"connection_id": w.ids[conn]}).json()["id"]
        with sessions.begin() as db:
            db.get(Publication, w.ids["pub_failed"]).status = "failed"
        yield w
    engine.dispose()


def sandbox_connection(client, admin) -> dict:
    start = client.post("/api/v1/publishing/connections/authorize", headers=admin,
                        json={"provider": "mock", "channel": "instagram"}).json()
    challenge = start["authorize_url"].split("challenge=")[1]
    code = client.post("/api/v1/publishing/sandbox/authorize", headers=admin, json={
        "state": start["state"], "challenge": challenge, "allow": True}).json()["code"]
    return client.post("/api/v1/publishing/oauth/callback", headers=admin,
                       json={"state": start["state"], "code": code}).json()


def snapshot(w: World) -> dict:
    """Everything workspace A owns, as plain data."""
    out = {}
    with w.sessions() as db:
        for model in A_TABLES:
            rows = db.scalars(select(model).where(model.workspace_id == "ws-a")).all()
            out[model.__tablename__] = sorted(
                repr({c.name: getattr(r, c.name) for c in model.__table__.columns}) for r in rows
            )
        ws = db.get(Workspace, "ws-a")
        out["workspace"] = repr({c.name: getattr(ws, c.name) for c in Workspace.__table__.columns})
        shas = select(ApiJob.media_sha256).where(ApiJob.workspace_id == "ws-a")
        out["objects"] = sorted(
            (a.sha256, a.stored_filename, a.purged_at)
            for a in db.scalars(select(MediaAsset).where(MediaAsset.sha256.in_(shas))))
        out["accounts"] = sorted(
            (u.email, u.password_hash, u.token_version, u.role)
            for u in db.scalars(select(User).where(
                User.id.in_([w.ids["anna"], w.ids["alice"], w.ids["carl"]])))
        )
    out["redis"] = {k: w.runtime.store.status(w.ids[k])["status"]
                    for k in ("approved", "pending", "unsent", "schedulable", "queued")}
    return out


# ── Routes that act on one resource, by id ─────────────────────────────────────────────────
# (method, path, {path param: world id key}, body, who legitimately may do it, expected status)

@dataclass(frozen=True)
class Scoped:
    method: str
    path: str
    params: dict
    body: object  # a dict, None, or a function of the world (for bodies that carry A's ids)
    owner: str
    ok: int = 200

    def json(self, w: World):
        return self.body(w) if callable(self.body) else self.body

    def url(self, w: World) -> str:
        return "/api/v1" + self.path.format(**{p: w.ids[k] for p, k in self.params.items()})


SCOPED = [
    Scoped("GET", "/users/{user_id}", {"user_id": "alice"}, None, "anna"),
    Scoped("PATCH", "/users/{user_id}/role", {"user_id": "alice"}, {"role": "admin"}, "anna"),
    Scoped("PATCH", "/users/{user_id}", {"user_id": "alice"},
           {"email": "taken@b.example.com", "password": "attacker-password-1", "role": "admin"},
           "anna"),
    Scoped("DELETE", "/users/{user_id}", {"user_id": "alice"}, None, "anna", 204),
    Scoped("GET", "/jobs/{job_id}", {"job_id": "approved"}, None, "alice"),
    Scoped("GET", "/jobs/{job_id}/media", {"job_id": "approved"}, None, "alice"),
    Scoped("PATCH", "/jobs/{job_id}/approval", {"job_id": "pending"},
           {"status": "approved", "checked": True}, "anna"),
    Scoped("POST", "/jobs/{job_id}/submit", {"job_id": "unsent"}, None, "alice"),
    Scoped("DELETE", "/jobs/{job_id}", {"job_id": "queued"}, None, "alice"),
    Scoped("POST", "/jobs/{job_id}/schedule", {"job_id": "schedulable"}, SCHEDULE, "alice", 201),
    Scoped("GET", "/jobs/{job_id}/versions", {"job_id": "pending"}, None, "alice"),
    Scoped("POST", "/jobs/{job_id}/versions", {"job_id": "pending"},
           {"base_version": 1, "text": "rewritten by an outsider"}, "alice", 201),
    Scoped("GET", "/jobs/{job_id}/comments", {"job_id": "pending"}, None, "alice"),
    Scoped("POST", "/jobs/{job_id}/comments", {"job_id": "pending"}, {"body": "hello"},
           "alice", 201),
    Scoped("PATCH", "/jobs/{job_id}/comments/{comment_id}",
           {"job_id": "pending", "comment_id": "comment"}, {"resolved": True}, "alice"),
    Scoped("GET", "/scheduled-posts/{post_id}", {"post_id": "post"}, None, "alice"),
    Scoped("PATCH", "/scheduled-posts/{post_id}", {"post_id": "post"}, SCHEDULE, "alice"),
    Scoped("DELETE", "/scheduled-posts/{post_id}", {"post_id": "post"}, None, "alice"),
    Scoped("DELETE", "/invitations/{invitation_id}", {"invitation_id": "invitation"}, None,
           "anna", 204),
    Scoped("PATCH", "/workspaces/{workspace_id}", {"workspace_id": "workspace"},
           {"name": "Taken over"}, "anna"),
    Scoped("POST", "/workspaces/{workspace_id}/leave", {"workspace_id": "workspace"}, None,
           "carl", 204),
    Scoped("POST", "/jobs/{job_id}/media-link", {"job_id": "approved"}, None, "alice"),
    Scoped("DELETE", "/jobs/{job_id}/media", {"job_id": "pending"}, None, "alice", 204),
    Scoped("GET", "/workspaces/{workspace_id}/retention", {"workspace_id": "workspace"}, None,
           "anna"),
    Scoped("POST", "/workspaces/{workspace_id}/retention/preview",
           {"workspace_id": "workspace"}, {"originals_days": 1}, "anna"),
    Scoped("PUT", "/workspaces/{workspace_id}/retention", {"workspace_id": "workspace"},
           {"originals_days": 1, "delete_after_publish": True}, "anna"),
    Scoped("DELETE", "/publishing/connections/{connection_id}", {"connection_id": "conn"},
           None, "anna"),
    Scoped("GET", "/scheduled-posts/{post_id}/publications", {"post_id": "post"}, None,
           "alice"),
    Scoped("POST", "/scheduled-posts/{post_id}/publications", {"post_id": "post"},
           lambda w: {"connection_id": w.ids["conn_spare"]}, "alice", 201),
    Scoped("POST", "/publications/{publication_id}/retry", {"publication_id": "pub_failed"},
           None, "alice"),
    Scoped("DELETE", "/publications/{publication_id}", {"publication_id": "pub_queued"}, None,
           "alice"),
]

# GET routes without ids that return tenant data, with the query they need.
DAY = date.today()
RANGE = f"start={DAY}&end={DAY + timedelta(days=7)}&timezone=UTC"
LISTS = {
    "/users": "", "/jobs": "", "/jobs/mine": "", "/jobs/review": "", "/jobs/approved": "",
    "/jobs/rejected": "", "/stats": "", "/scheduled-posts": RANGE, "/calendar": RANGE,
    "/invitations": "", "/audit-events": "", "/auth/me": "", "/workspaces": "",
    "/publishing/providers": "", "/publishing/connections": "", "/publishing/sandbox": "",
    # Aggregates; the installation view is for platform admins and has no content or ids.
    "/analytics": "", "/analytics/workspaces": "",
    # The caller's own notifications in the current workspace, and their email preference.
    "/notifications": "", "/notifications/preferences": "",
}

# Routes with no resource id: public, account-level, or creating inside the caller's own
# workspace. Each is covered below or elsewhere (see the comment).
UNSCOPED = {
    ("GET", "/meta"),                    # public, no tenant data
    ("POST", "/auth/login"),             # public
    # The caller's own rows only (user and workspace): tests/integration/test_notifications.py
    ("POST", "/notifications/read"),
    ("PUT", "/notifications/preferences"),
    ("POST", "/auth/signup"),            # creates a brand-new workspace
    ("POST", "/workspaces"),             # creates a brand-new workspace
    # These three create inside the caller's workspace; a spoofed header is refused in
    # test_creating_never_lands_in_a_workspace_you_are_not_in.
    ("POST", "/jobs"),
    ("POST", "/users"),
    ("POST", "/invitations"),
    ("POST", "/invitations/lookup"),     # token-gated: tests/integration/test_invitations.py
    ("POST", "/invitations/accept"),     # token + email gated: test_invitations.py
    ("POST", "/publishing/connections/authorize"),  # own workspace; spoof test below
    # OAuth state-gated: the state is bound to the admin and workspace that started it
    # (test_publishing.py::test_authorization_state_is_single_use_bound_and_expiring).
    ("POST", "/publishing/oauth/callback"),
    ("POST", "/publishing/sandbox/authorize"),
}

ATTACKERS = ("bea", "pat", "bob")  # B's admin, a platform admin, B's member


def test_every_route_is_classified(world):
    # The OpenAPI schema lists every route, however routers are nested (FastAPI keeps
    # included routers as objects, so app.routes alone is not the full list).
    routes = {
        (method.upper(), path.removeprefix("/api/v1"))
        for path, operations in world.client.app.openapi()["paths"].items()
        if path.startswith("/api/v1")
        for method in operations
    }
    assert len(routes) >= 60  # sanity: the enumeration really found the API
    classified = (
        {(s.method, s.path) for s in SCOPED} | {("GET", p) for p in LISTS} | UNSCOPED
    )
    assert routes - classified == set(), "classify new routes in test_tenant_isolation.py"
    assert classified - routes == set(), "stale entries in test_tenant_isolation.py"
    for method, path in routes:
        if "{" in path:
            assert (method, path) in {(s.method, s.path) for s in SCOPED}


@pytest.mark.parametrize("route", SCOPED, ids=lambda s: f"{s.method} {s.path}")
def test_no_one_outside_a_workspace_can_touch_its_resources(world, route):
    w = world
    before = snapshot(w)
    leaked = w.markers() - {w.ids[k] for k in route.params.values()}  # the id we sent is known
    for attacker in ATTACKERS:
        for workspace in (None, "ws-a"):  # as themselves, then claiming to be in A
            response = w.client.request(
                route.method, route.url(w), headers=w.as_(attacker, workspace),
                json=route.json(w)
            )
            label = f"{attacker} {workspace or ''}"
            assert response.status_code in (403, 404), (label, response.status_code,
                                                         response.text)
            assert not any(m in response.text for m in leaked), (label, response.text)
            assert snapshot(w) == before, f"{label} changed workspace A"

    # Positive control: the same request from A's legitimate user succeeds.
    legit = w.client.request(
        route.method, route.url(w), headers=w.as_(route.owner, "ws-a"), json=route.json(w)
    )
    assert legit.status_code == route.ok, legit.text


@pytest.mark.parametrize("path", sorted(LISTS))
def test_lists_never_show_another_workspaces_data(world, path):
    w = world
    url = f"/api/v1{path}" + (f"?{LISTS[path]}" if LISTS[path] else "")
    for attacker in ATTACKERS:
        response = w.client.get(url, headers=w.as_(attacker))
        assert response.status_code in (200, 403), response.text
        assert not any(m in response.text for m in w.markers()), (attacker, response.text)
        spoofed = w.client.get(url, headers=w.as_(attacker, "ws-a"))
        if path == "/workspaces":  # lists your own memberships; ignores the header
            assert "ws-a" not in spoofed.text
        else:
            assert spoofed.status_code == 403, (attacker, spoofed.text)


def test_lists_do_show_the_data_to_its_own_workspace(world):
    """Control for the test above: the markers really are in A's own lists."""
    w = world
    seen = "".join(
        w.client.get(f"/api/v1{p}" + (f"?{q}" if q else ""), headers=w.as_("anna")).text
        for p, q in LISTS.items()
    ) + w.client.get(f"/api/v1/jobs/{w.ids['pending']}/comments", headers=w.as_("anna")).text
    # (comments are listed per job, not in a workspace-wide list)
    assert {m for m in w.markers() if m not in seen} == set()


@pytest.mark.parametrize("attacker", ATTACKERS)
def test_creating_never_lands_in_a_workspace_you_are_not_in(world, attacker):
    w = world
    before = snapshot(w)
    spoofed = w.as_(attacker, "ws-a")
    attempts = [
        w.client.post("/api/v1/jobs", headers=spoofed,
                      files={"file": ("x.png", png((7, 7, 7)), "image/png")}),
        w.client.post("/api/v1/users", headers=spoofed,
                      json={"email": "plant@b.example.com", "password": "a-long-password-1"}),
        w.client.post("/api/v1/invitations", headers=spoofed,
                      json={"email": "plant@b.example.com", "role": "admin"}),
        w.client.post("/api/v1/publishing/connections/authorize", headers=spoofed,
                      json={"provider": "mock", "channel": "instagram"}),
    ]
    assert [r.status_code for r in attempts] == [403, 403, 403, 403]
    assert snapshot(w) == before


def test_removed_members_lose_access_at_once(world):
    w = world
    carl = w.as_("carl", "ws-a")
    assert w.client.get("/api/v1/jobs/mine", headers=carl).status_code == 200
    assert w.client.delete(f"/api/v1/users/{w.ids['carl']}",
                           headers=w.as_("anna")).status_code == 204
    # Same token, still valid for Carl's other workspace, but no longer for A.
    assert w.client.get("/api/v1/jobs/mine", headers=carl).status_code == 403
    assert w.client.get("/api/v1/jobs/mine",
                        headers=w.as_("carl", "ws-c")).status_code == 200


def test_unknown_and_malformed_workspace_headers_are_refused(world):
    w = world
    for value in ("ws-nope", "x" * 36):
        assert w.client.get("/api/v1/jobs/mine",
                            headers=w.as_("alice", value)).status_code == 403
    assert w.client.get("/api/v1/jobs/mine",
                        headers=w.as_("alice", "x" * 37)).status_code == 422
