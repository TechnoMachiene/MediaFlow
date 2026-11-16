"""Idempotent job submission: `POST /jobs` with an `Idempotency-Key` header.

A client that never saw the response (timeout, dropped connection, app killed) retries with
the same key and gets the job its first attempt created, never a second job or a second Gemini
call. See "Submitting a job" in docs/JOB_LIFECYCLE.md.
"""

import pytest
import redis
from fastapi.testclient import TestClient
from sqlalchemy import func, select, update

from app.api import routes
from app.api.database import ApiJob
from app.api.reconcile import Reconciler
from tests.api_harness import add_member, build_app, login, png
from tests.helpers import ScriptedClient

pytestmark = pytest.mark.integration


@pytest.fixture
def system(make_rt, tmp_path, monkeypatch):
    monkeypatch.setattr("app.api.routes.JOB_RESULT_WAIT_S", 0)
    gemini = ScriptedClient()
    runtime = make_rt(client=gemini)
    application, engine, sessions = build_app(runtime, tmp_path / "media")
    add_member(sessions, "owner@example.com")
    add_member(sessions, "other@example.com")
    with TestClient(application) as client:
        reconciler = Reconciler(
            sessions, runtime.store, runtime.queue, client.app.state.storage, grace_s=0
        )
        yield client, runtime, sessions, gemini, reconciler, login(client, "owner@example.com")
    engine.dispose()


def post(client, headers, content, key=None, name="cat.png"):
    if key is not None:
        headers = {**headers, "Idempotency-Key": key}
    return client.post(
        "/api/v1/jobs", headers=headers, files={"file": (name, content, "image/png")}
    )


def job_count(sessions) -> int:
    with sessions() as db:
        return db.scalar(select(func.count()).select_from(ApiJob))


def test_retry_with_the_same_key_returns_the_same_job(system):
    client, runtime, sessions, gemini, _, owner = system
    first = post(client, owner, png(), key="k-1")
    assert first.status_code == 202 and "Idempotent-Replayed" not in first.headers

    retry = post(client, owner, png(), key="k-1")
    assert retry.status_code == 202
    assert retry.headers["Idempotent-Replayed"] == "true"
    assert retry.json()["job_id"] == first.json()["job_id"]
    assert job_count(sessions) == 1
    assert runtime.queue.depths()["ready"] == 1

    runtime.worker.drain()
    assert len(gemini.calls) == 1


def test_retry_after_the_job_finished_returns_its_result(system):
    """The first response was lost but the work completed: the retry is answered from it."""
    client, runtime, sessions, gemini, _, owner = system
    job_id = post(client, owner, png(), key="k-done").json()["job_id"]
    runtime.worker.drain()

    retry = post(client, owner, png(), key="k-done")
    assert retry.status_code == 200
    assert retry.json()["job_id"] == job_id and retry.json()["status"] == "done"
    assert retry.json()["result"]["text"].startswith("scripted")
    assert len(gemini.calls) == 1


def test_retry_after_the_api_died_before_dispatch_dispatches_at_once(system):
    """The first attempt committed the row and died before Redis. The retry doesn't wait for
    the reconciler: it hands the job over itself."""
    client, runtime, sessions, gemini, reconciler, owner = system
    real_submit = runtime.store.submit_prepared

    def down(*args, **kwargs):
        raise redis.ConnectionError("Redis is unavailable")

    runtime.store.submit_prepared = down
    job_id = post(client, owner, png(), key="k-crash").json()["job_id"]
    runtime.store.submit_prepared = real_submit
    assert runtime.queue.depths()["ready"] == 0

    retry = post(client, owner, png(), key="k-crash")
    assert retry.json()["job_id"] == job_id and retry.json()["status"] == "queued"
    with sessions() as db:
        assert db.get(ApiJob, job_id).enqueued_at is not None
    assert runtime.queue.depths()["ready"] == 1
    assert reconciler.run_once().get("dispatched", 0) == 0  # nothing left for it to do

    runtime.worker.drain()
    assert len(gemini.calls) == 1


def test_retry_while_redis_is_still_down_is_accepted_and_kept(system, monkeypatch):
    client, runtime, sessions, gemini, reconciler, owner = system

    def down(*args, **kwargs):
        raise redis.ConnectionError("Redis is unavailable")

    monkeypatch.setattr(runtime.store, "submit_prepared", down)
    job_id = post(client, owner, png(), key="k-down").json()["job_id"]
    retry = post(client, owner, png(), key="k-down")
    assert retry.status_code == 202 and retry.json()["job_id"] == job_id
    assert job_count(sessions) == 1

    monkeypatch.undo()
    assert reconciler.run_once()["dispatched"] == 1
    runtime.worker.drain()
    assert len(gemini.calls) == 1


def test_concurrent_requests_with_one_key_create_one_job(system, monkeypatch):
    """Two attempts race: both look the key up before either commits. The unique index lets
    exactly one insert win, and the loser answers with the winner's job."""
    client, runtime, sessions, _, _, owner = system
    winner = post(client, owner, png(), key="k-race").json()["job_id"]

    real_lookup = routes._keyed_job
    calls = {"n": 0}

    def lookup_before_the_winner_committed(*args):
        calls["n"] += 1
        return None if calls["n"] == 1 else real_lookup(*args)

    monkeypatch.setattr(routes, "_keyed_job", lookup_before_the_winner_committed)
    loser = post(client, owner, png(), key="k-race")
    assert calls["n"] == 2  # missed once, then found it after the insert collided
    assert loser.status_code == 202
    assert loser.json()["job_id"] == winner
    assert loser.headers["Idempotent-Replayed"] == "true"
    assert job_count(sessions) == 1


@pytest.mark.parametrize(
    "change", [{"content": png((9, 9, 9))}, {"name": "renamed.png"}], ids=["bytes", "filename"]
)
def test_reusing_a_key_for_a_different_upload_is_rejected(system, change):
    client, runtime, sessions, _, _, owner = system
    post(client, owner, png(), key="k-reuse")
    second = post(client, owner, change.get("content", png()), key="k-reuse",
                  name=change.get("name", "cat.png"))
    assert second.status_code == 422
    assert "different upload" in second.json()["detail"]
    assert job_count(sessions) == 1


def test_keys_are_scoped_to_one_user_in_one_workspace(system):
    client, runtime, sessions, _, _, owner = system
    add_member(sessions, "owner@example.com", workspace_id="ws-b")
    other_user = login(client, "other@example.com")
    owner_in_b = login(client, "owner@example.com", workspace_id="ws-b")

    ids = {post(client, h, png(), key="shared").json()["job_id"]
           for h in (owner, other_user, owner_in_b)}
    assert len(ids) == 3  # nobody can read another user's job by guessing their key


def test_without_a_key_every_upload_is_a_new_job(system):
    client, runtime, sessions, _, _, owner = system
    a, b = post(client, owner, png()).json(), post(client, owner, png()).json()
    assert a["job_id"] != b["job_id"]
    assert job_count(sessions) == 2


@pytest.mark.parametrize("key", ["", "has space", "x" * 256, "naïve"])
def test_malformed_keys_are_rejected(system, key):
    client, _, sessions, _, _, owner = system
    response = client.post(
        "/api/v1/jobs",
        headers={**owner, "Idempotency-Key": key.encode()},  # raw bytes reach the server
        files={"file": ("cat.png", png(), "image/png")},
    )
    assert response.status_code == 422
    assert job_count(sessions) == 0


def test_a_cancelled_job_is_replayed_as_cancelled_not_resubmitted(system):
    client, runtime, sessions, gemini, _, owner = system
    job_id = post(client, owner, png(), key="k-cancel").json()["job_id"]
    assert client.delete(f"/api/v1/jobs/{job_id}", headers=owner).status_code == 200

    retry = post(client, owner, png(), key="k-cancel")
    assert retry.json() == {**retry.json(), "job_id": job_id, "status": "cancelled"}
    runtime.worker.drain()
    assert gemini.calls == []


def test_replay_never_reports_unknown_when_redis_forgot_the_job(system):
    client, runtime, sessions, _, _, owner = system
    job_id = post(client, owner, png(), key="k-forgot").json()["job_id"]
    s = runtime.store
    runtime.r.delete(s._job(job_id), s._submitted(job_id))
    with sessions.begin() as db:
        db.execute(update(ApiJob).where(ApiJob.id == job_id).values(status="queued"))

    retry = post(client, owner, png(), key="k-forgot")
    assert retry.json()["status"] == "queued"
    with sessions() as db:
        assert db.get(ApiJob, job_id).status == "queued"  # still active, so it gets recovered
