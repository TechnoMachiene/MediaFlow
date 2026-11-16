"""Draft editor: version history and review comments. See app/api/drafts.py."""

import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.api.database import ApiJob, AuditEvent, DraftComment, DraftVersion
from app.api.persistence import persist_api_result
from tests.api_harness import add_member, build_app, login, png, upload
from tests.helpers import ScriptedClient

pytestmark = pytest.mark.integration

STRUCTURED = {
    "instagram": {"caption": "Golden hour on the pier", "hashtags": ["#sunset", "#pier"]},
    "facebook": {"post_copy": "Our favourite view this week."},
}


class JsonClient(ScriptedClient):
    """Gemini stand-in that answers with the platform-keyed JSON the real prompt asks for."""

    def analyse(self, spec):
        out = super().analyse(spec)
        return {**out, "text": json.dumps(STRUCTURED)}


@pytest.fixture
def team(make_rt, tmp_path, monkeypatch):
    monkeypatch.setattr("app.api.routes.JOB_RESULT_WAIT_S", 0)
    gemini = JsonClient()
    runtime = make_rt(client=gemini)
    application, engine, sessions = build_app(runtime, tmp_path / "media")
    add_member(sessions, "alice@a.example.com", "user", "ws-a")
    add_member(sessions, "ada@a.example.com", "user", "ws-a")
    add_member(sessions, "max@a.example.com", "manager", "ws-a")
    with TestClient(application) as client:
        h = {name: login(client, f"{name}@a.example.com") for name in ("alice", "ada", "max")}
        yield client, sessions, runtime, h, gemini
    engine.dispose()


def drafted(client, runtime, headers, color=(1, 2, 3)) -> str:
    job_id = upload(client, headers, png(color)).json()["job_id"]
    runtime.worker.drain()
    return job_id


def edited(caption: str) -> str:
    return json.dumps({**STRUCTURED, "instagram": {**STRUCTURED["instagram"], "caption": caption}})


def save(client, headers, job_id, base, text=None, restore=None, note=None):
    body = {"base_version": base}
    body.update({k: v for k, v in (("text", text), ("restore", restore), ("note", note)) if v})
    return client.post(f"/api/v1/jobs/{job_id}/versions", headers=headers, json=body)


def versions(client, headers, job_id):
    return client.get(f"/api/v1/jobs/{job_id}/versions", headers=headers).json()


def job(client, headers, job_id):
    return client.get(f"/api/v1/jobs/{job_id}", headers=headers).json()


# ── Versions ──────────────────────────────────────────────────────────────────────────────

def test_an_unedited_draft_is_gemini_version_one_and_costs_nothing(team):
    client, sessions, runtime, h, _ = team
    job_id = drafted(client, runtime, h["alice"])
    [v1] = versions(client, h["alice"], job_id)
    assert (v1["number"], v1["source"], v1["author_email"], v1["current"]) == (1, "ai", None, True)
    assert json.loads(v1["text"]) == STRUCTURED
    with sessions() as db:
        assert db.scalars(select(DraftVersion)).all() == []  # nothing stored until an edit


def test_saving_makes_a_new_version_that_every_screen_shows(team):
    client, sessions, runtime, h, _ = team
    job_id = drafted(client, runtime, h["alice"])
    saved = save(client, h["alice"], job_id, 1, edited("Sunset, but better"), note="Warmer tone")
    assert saved.status_code == 201
    assert saved.json()["version"] == {**saved.json()["version"], "number": 2, "source": "edit",
                                       "author_email": "alice@a.example.com",
                                       "note": "Warmer tone", "current": True}

    view = job(client, h["alice"], job_id)
    assert view["draft_version"] == 2
    assert json.loads(view["result"]["text"])["instagram"]["caption"] == "Sunset, but better"
    history = versions(client, h["alice"], job_id)
    assert [(v["number"], v["source"], v["current"]) for v in history] == [
        (2, "edit", True), (1, "ai", False)
    ]
    assert json.loads(history[1]["text"]) == STRUCTURED  # the original is kept


def test_the_queue_never_overwrites_an_edited_draft(team):
    """Redis still caches Gemini's text for this media. Neither a replayed result, nor a list
    view folding in Redis state, nor identical media uploaded again may undo the edit."""
    client, sessions, runtime, h, _ = team
    job_id = drafted(client, runtime, h["alice"])
    save(client, h["alice"], job_id, 1, edited("Human words"))

    persist_api_result(sessions, job_id, {"text": json.dumps(STRUCTURED), "kind": "image",
                                          "model": "m", "tokens": 0})
    again = drafted(client, runtime, h["alice"])  # identical media: a cache hit, same cache key
    for view in (job(client, h["alice"], job_id),
                 *client.get("/api/v1/jobs/mine", headers=h["alice"]).json()["items"]):
        if view["job_id"] == job_id:
            assert json.loads(view["result"]["text"])["instagram"]["caption"] == "Human words"
    assert json.loads(job(client, h["alice"], again)["result"]["text"]) == STRUCTURED


def test_a_stale_save_is_refused_instead_of_overwriting(team):
    client, sessions, runtime, h, _ = team
    job_id = drafted(client, runtime, h["alice"])
    assert save(client, h["max"], job_id, 1, edited("Manager's version")).status_code == 201

    late = save(client, h["alice"], job_id, 1, edited("Alice's version"))
    assert late.status_code == 409
    assert late.headers["X-Current-Version"] == "2"
    assert "version 2" in late.json()["detail"]
    assert len(versions(client, h["alice"], job_id)) == 2


def test_restoring_an_older_version_adds_a_new_one(team):
    client, sessions, runtime, h, _ = team
    job_id = drafted(client, runtime, h["alice"])
    save(client, h["alice"], job_id, 1, edited("Second"))
    restored = save(client, h["alice"], job_id, 2, restore=1)
    assert restored.status_code == 201
    v3 = restored.json()["version"]
    assert (v3["number"], v3["source"], v3["restored_from"]) == (3, "restore", 1)
    assert json.loads(job(client, h["alice"], job_id)["result"]["text"]) == STRUCTURED
    assert save(client, h["alice"], job_id, 3, restore=9).status_code == 404


@pytest.mark.parametrize("decision", ["approved", "rejected"])
def test_editing_a_decided_draft_sends_it_back_for_review(team, decision):
    client, sessions, runtime, h, _ = team
    job_id = drafted(client, runtime, h["alice"])
    client.patch(f"/api/v1/jobs/{job_id}/approval", headers=h["max"], json={"status": decision})
    saved = save(client, h["alice"], job_id, 1, edited("Revised"))
    assert saved.json()["approval_status"] == "pending"
    assert saved.json()["returned_to_review"] is True
    waiting = client.get("/api/v1/jobs/review", headers=h["max"]).json()["items"]
    assert [j["job_id"] for j in waiting] == [job_id]

    with sessions() as db:
        event = db.scalars(select(AuditEvent).where(AuditEvent.action == "draft.edited")).one()
    assert event.before == {"version": 1, "approval_status": decision}
    assert event.after == {"version": 2, "approval_status": "pending"}


def test_a_draft_on_the_calendar_is_locked(team):
    client, sessions, runtime, h, _ = team
    job_id = drafted(client, runtime, h["alice"])
    client.patch(f"/api/v1/jobs/{job_id}/approval", headers=h["max"], json={"status": "approved"})
    when = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    post = client.post(f"/api/v1/jobs/{job_id}/schedule", headers=h["alice"],
                       json={"scheduled_at": when, "timezone": "UTC"}).json()
    locked = save(client, h["alice"], job_id, 1, edited("Sneaky change"))
    assert locked.status_code == 409 and "calendar" in locked.json()["detail"]

    client.delete(f"/api/v1/scheduled-posts/{post['id']}", headers=h["alice"])
    assert save(client, h["alice"], job_id, 1, edited("Now allowed")).status_code == 201


@pytest.mark.parametrize("body, reason", [
    ({"text": "just prose now"}, "keep it as a JSON object"),
    ({"text": json.dumps(STRUCTURED)}, "Nothing changed"),
    ({"text": "   "}, "can't be empty"),
])
def test_invalid_saves_are_refused(team, body, reason):
    client, sessions, runtime, h, _ = team
    job_id = drafted(client, runtime, h["alice"])
    response = client.post(f"/api/v1/jobs/{job_id}/versions", headers=h["alice"],
                           json={"base_version": 1, **body})
    assert response.status_code == 422 and reason in response.json()["detail"]
    assert job(client, h["alice"], job_id)["draft_version"] == 1


def test_save_needs_exactly_one_of_text_or_restore(team):
    client, sessions, runtime, h, _ = team
    job_id = drafted(client, runtime, h["alice"])
    for body in ({"base_version": 1}, {"base_version": 1, "text": "x", "restore": 1}):
        assert client.post(f"/api/v1/jobs/{job_id}/versions", headers=h["alice"],
                           json=body).status_code == 422


def test_plain_text_drafts_can_be_edited_freely(make_rt, tmp_path, monkeypatch):
    monkeypatch.setattr("app.api.routes.JOB_RESULT_WAIT_S", 0)
    runtime = make_rt(client=ScriptedClient())  # answers with prose, not JSON
    application, engine, sessions = build_app(runtime, tmp_path / "media")
    add_member(sessions, "alice@a.example.com", "user", "ws-a")
    with TestClient(application) as client:
        alice = login(client, "alice@a.example.com")
        job_id = drafted(client, runtime, alice)
        assert save(client, alice, job_id, 1, "Rewritten by hand").status_code == 201
    engine.dispose()


def test_only_the_uploader_and_reviewers_can_work_on_a_draft(team):
    client, sessions, runtime, h, _ = team
    job_id = drafted(client, runtime, h["alice"])
    for path in ("versions", "comments"):
        assert client.get(f"/api/v1/jobs/{job_id}/{path}", headers=h["ada"]).status_code == 404
    assert save(client, h["ada"], job_id, 1, edited("Not mine")).status_code == 404
    assert client.post(f"/api/v1/jobs/{job_id}/comments", headers=h["ada"],
                       json={"body": "hi"}).status_code == 404
    assert save(client, h["max"], job_id, 1, edited("Reviewer fix")).status_code == 201


def test_no_draft_yet_means_nothing_to_edit_or_discuss(team):
    client, sessions, runtime, h, _ = team
    job_id = upload(client, h["alice"], png()).json()["job_id"]  # still queued
    assert versions(client, h["alice"], job_id) == []
    assert save(client, h["alice"], job_id, 1, "early").status_code == 409
    assert client.post(f"/api/v1/jobs/{job_id}/comments", headers=h["alice"],
                       json={"body": "hi"}).status_code == 409


# ── Comments ──────────────────────────────────────────────────────────────────────────────

def comment(client, headers, job_id, body, **extra):
    return client.post(f"/api/v1/jobs/{job_id}/comments", headers=headers,
                       json={"body": body, **extra})


def test_comments_thread_and_remember_the_version_and_field(team):
    client, sessions, runtime, h, _ = team
    job_id = drafted(client, runtime, h["alice"])
    root = comment(client, h["max"], job_id, "Caption is too long", field="instagram.caption")
    assert root.status_code == 201
    root = root.json()
    assert (root["version"], root["field"], root["kind"]) == (1, "instagram.caption", "comment")

    reply = comment(client, h["alice"], job_id, "Shortened it", parent_id=root["id"]).json()
    nested = comment(client, h["max"], job_id, "Thanks", parent_id=reply["id"]).json()
    assert nested["parent_id"] == root["id"]  # replies stay one level deep

    save(client, h["alice"], job_id, 1, edited("Short"))
    later = comment(client, h["max"], job_id, "Better").json()
    assert later["version"] == 2

    thread = client.get(f"/api/v1/jobs/{job_id}/comments", headers=h["alice"]).json()
    assert [(c["body"], c["parent_id"]) for c in thread] == [
        ("Caption is too long", None), ("Shortened it", root["id"]),
        ("Thanks", root["id"]), ("Better", None),
    ]


def test_threads_resolve_and_reopen(team):
    client, sessions, runtime, h, _ = team
    job_id = drafted(client, runtime, h["alice"])
    root = comment(client, h["max"], job_id, "Fix the hashtags").json()
    reply = comment(client, h["alice"], job_id, "Done", parent_id=root["id"]).json()
    url = f"/api/v1/jobs/{job_id}/comments"

    resolved = client.patch(f"{url}/{root['id']}", headers=h["alice"], json={"resolved": True})
    assert resolved.json()["resolved_by_email"] == "alice@a.example.com"
    assert resolved.json()["resolved_at"] is not None
    reopened = client.patch(f"{url}/{root['id']}", headers=h["max"], json={"resolved": False})
    assert reopened.json()["resolved_at"] is None
    assert client.patch(f"{url}/{reply['id']}", headers=h["max"],
                        json={"resolved": True}).status_code == 409


def test_comments_cannot_reach_into_another_job(team):
    client, sessions, runtime, h, _ = team
    first = drafted(client, runtime, h["alice"])
    second = drafted(client, runtime, h["alice"], color=(9, 9, 9))
    root = comment(client, h["max"], first, "On the first job").json()
    assert comment(client, h["max"], second, "reply", parent_id=root["id"]).status_code == 404
    assert client.patch(f"/api/v1/jobs/{second}/comments/{root['id']}", headers=h["max"],
                        json={"resolved": True}).status_code == 404


@pytest.mark.parametrize("body", ["", "   ", "x" * 2001])
def test_empty_or_huge_comments_are_refused(team, body):
    client, sessions, runtime, h, _ = team
    job_id = drafted(client, runtime, h["alice"])
    assert comment(client, h["max"], job_id, body).status_code == 422


def test_a_rejection_reason_lands_in_the_thread(team):
    client, sessions, runtime, h, _ = team
    job_id = drafted(client, runtime, h["alice"])
    client.patch(f"/api/v1/jobs/{job_id}/approval", headers=h["max"],
                 json={"status": "rejected", "comment": "Wrong product name in the caption"})
    [note] = client.get(f"/api/v1/jobs/{job_id}/comments", headers=h["alice"]).json()
    assert (note["kind"], note["body"], note["author_email"]) == (
        "rejected", "Wrong product name in the caption", "max@a.example.com"
    )
    other = drafted(client, runtime, h["alice"], color=(4, 4, 4))
    client.patch(f"/api/v1/jobs/{other}/approval", headers=h["max"], json={"status": "approved"})
    assert client.get(f"/api/v1/jobs/{other}/comments", headers=h["alice"]).json() == []


def test_removing_a_member_removes_their_drafts_history(team):
    client, sessions, runtime, h, _ = team
    job_id = drafted(client, runtime, h["alice"])
    save(client, h["alice"], job_id, 1, edited("Mine"))
    comment(client, h["max"], job_id, "ok")
    add_member(sessions, "root@a.example.com", "admin", "ws-a")
    with sessions() as db:
        alice_id = db.get(ApiJob, job_id).owner_id
    root = login(client, "root@a.example.com")
    assert client.delete(f"/api/v1/users/{alice_id}", headers=root).status_code == 204
    with sessions() as db:
        assert db.scalars(select(DraftVersion)).all() == []
        assert db.scalars(select(DraftComment)).all() == []
