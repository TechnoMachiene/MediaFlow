"""DB <-> Redis lifecycle: every crash point in docs/JOB_LIFECYCLE.md has a test here.

Each test drives the real API, Redis queue, worker and SQLite database, injects one failure at
one step, and checks that the job still reaches a correct terminal state in BOTH stores.
"""

import threading

import pytest
import redis
from fastapi.testclient import TestClient
from sqlalchemy import event, update
from sqlalchemy.exc import OperationalError

from app.api import routes
from app.api.database import ApiJob, MediaAsset
from app.api.persistence import persist_api_result, persist_api_status
from app.api.reconcile import LOST, MEDIA_GONE, Reconciler
from app.errors import Permanent, Transient
from tests.api_harness import add_member, build_app, login, png, upload
from tests.helpers import ScriptedClient

pytestmark = pytest.mark.integration


@pytest.fixture
def system(make_rt, tmp_path, monkeypatch):
    monkeypatch.setattr("app.api.routes.JOB_RESULT_WAIT_S", 0)
    gemini = ScriptedClient()
    runtime = make_rt(client=gemini)
    application, engine, sessions = build_app(runtime, tmp_path / "media")
    add_member(sessions, "owner@example.com")
    with TestClient(application) as client:
        reconciler = Reconciler(
            sessions, runtime.store, runtime.queue, client.app.state.storage, grace_s=0
        )
        yield client, runtime, sessions, gemini, reconciler, login(client, "owner@example.com")
    engine.dispose()


def row(sessions, job_id) -> ApiJob:
    with sessions() as db:
        return db.get(ApiJob, job_id)


def wipe_redis_job(runtime, job_id, *, inflight=False):
    """Simulate Redis losing a job's records (flush, failover, eviction). By default the
    in-flight dedup key survives, which is the harder partial-loss case."""
    s = runtime.store
    job = s.get(job_id)
    runtime.r.delete(s._job(job_id), s._submitted(job_id))
    if inflight and job is not None:
        runtime.r.delete(s._inflight(job.cache_key))
    runtime.r.lrem(runtime.queue.ready, 0, job_id)


def test_happy_path_keeps_both_stores_in_step(system):
    client, runtime, sessions, gemini, _, owner = system
    job_id = upload(client, owner, png()).json()["job_id"]
    stored = row(sessions, job_id)
    assert stored.enqueued_at is not None and stored.status == "queued"
    assert runtime.store.status(job_id)["status"] == "queued"

    runtime.worker.drain()
    stored = row(sessions, job_id)
    assert stored.status == "done" and stored.result["text"].startswith("scripted")
    assert len(gemini.calls) == 1


# ── Crash point 1: API commits the row, then dies before talking to Redis ─────────────────

def crash_before_dispatch(client, runtime, sessions, owner, content) -> str:
    """Leave exactly the state a killed API process leaves after step 1: the row committed
    with enqueued_at NULL, and nothing in Redis."""
    job_id = upload(client, owner, content).json()["job_id"]
    wipe_redis_job(runtime, job_id, inflight=True)
    with sessions.begin() as db:
        db.execute(update(ApiJob).where(ApiJob.id == job_id).values(enqueued_at=None))
    return job_id


def test_row_committed_but_never_dispatched_is_dispatched_by_reconciler(system):
    client, runtime, sessions, gemini, reconciler, owner = system
    job_id = crash_before_dispatch(client, runtime, sessions, owner, png())
    assert runtime.queue.depths()["ready"] == 0

    assert reconciler.run_once()["dispatched"] == 1
    assert row(sessions, job_id).enqueued_at is not None
    runtime.worker.drain()
    assert row(sessions, job_id).status == "done"
    assert len(gemini.calls) == 1


def redis_unreachable(*args, **kwargs):
    raise redis.ConnectionError("Redis is unavailable")


def test_redis_down_during_upload_keeps_the_job_in_the_outbox(system, monkeypatch):
    client, runtime, sessions, gemini, reconciler, owner = system
    real_submit = runtime.store.submit_prepared
    monkeypatch.setattr(runtime.store, "submit_prepared", redis_unreachable)
    response = upload(client, owner, png())
    # Committed means accepted: the client is told so and gets the id to poll.
    assert response.status_code == 202
    job_id = response.json()["job_id"]
    assert response.json()["status"] == "queued"
    stored = row(sessions, job_id)
    assert stored.enqueued_at is None and stored.status == "queued"
    assert runtime.store.status(job_id)["status"] == "unknown"

    monkeypatch.setattr(runtime.store, "submit_prepared", real_submit)  # Redis is back
    assert reconciler.run_once()["dispatched"] == 1
    runtime.worker.drain()
    assert row(sessions, job_id).status == "done"
    assert len(gemini.calls) == 1


def test_redis_reply_lost_after_it_accepted_the_job_never_double_enqueues(system, monkeypatch):
    """Redis ran the submit, but the connection dropped before the API saw the reply."""
    client, runtime, sessions, gemini, reconciler, owner = system
    real_submit = runtime.store.submit_prepared

    def accepted_then_lost(*args, **kwargs):
        real_submit(*args, **kwargs)
        raise redis.TimeoutError("Timeout reading from socket")

    monkeypatch.setattr(runtime.store, "submit_prepared", accepted_then_lost)
    response = upload(client, owner, png())
    assert response.status_code == 202
    job_id = response.json()["job_id"]
    assert row(sessions, job_id).enqueued_at is None
    assert runtime.queue.depths()["ready"] == 1  # Redis has it

    monkeypatch.setattr(runtime.store, "submit_prepared", real_submit)
    assert reconciler.run_once()["dispatched"] == 1  # records enqueued_at; pushes nothing
    assert runtime.queue.depths()["ready"] == 1
    runtime.worker.drain()
    assert row(sessions, job_id).status == "done"
    assert len(gemini.calls) == 1


def test_enqueued_at_commit_fails_after_redis_accepted(system, monkeypatch):
    """The database write *after* the queue write fails: a real failed COMMIT."""
    client, runtime, sessions, gemini, reconciler, owner = system
    real_dispatch = routes.dispatch_job

    def dispatch_then_db_dies(db, *args, **kwargs):
        out = real_dispatch(db, *args, **kwargs)

        def boom(session):
            raise OperationalError("COMMIT", {}, Exception("server closed the connection"))

        event.listen(db, "before_commit", boom, once=True)
        return out

    monkeypatch.setattr(routes, "dispatch_job", dispatch_then_db_dies)
    response = upload(client, owner, png())
    assert response.status_code == 202
    job_id = response.json()["job_id"]
    assert row(sessions, job_id).enqueued_at is None
    assert runtime.store.status(job_id)["status"] == "queued"

    monkeypatch.setattr(routes, "dispatch_job", real_dispatch)
    assert reconciler.run_once()["dispatched"] == 1
    assert runtime.queue.depths()["ready"] == 1
    runtime.worker.drain()
    assert row(sessions, job_id).status == "done"
    assert len(gemini.calls) == 1


def test_submitted_marker_without_job_hash_is_dispatched_not_stranded(system):
    """Partial Redis loss: the per-job `submitted` marker survived, the job hash didn't. A
    re-dispatch must enqueue the job, not report "unknown" and leave the row inactive."""
    client, runtime, sessions, gemini, reconciler, owner = system
    job_id = upload(client, owner, png()).json()["job_id"]
    s = runtime.store
    runtime.r.delete(s._job(job_id), s._inflight(s.get(job_id).cache_key))
    runtime.r.lrem(runtime.queue.ready, 0, job_id)
    assert runtime.r.exists(s._submitted(job_id))
    with sessions.begin() as db:
        db.execute(update(ApiJob).where(ApiJob.id == job_id).values(enqueued_at=None))

    assert reconciler.run_once()["dispatched"] == 1
    assert row(sessions, job_id).status == "queued"
    assert runtime.queue.depths()["ready"] == 1
    runtime.worker.drain()
    assert row(sessions, job_id).status == "done"


def test_job_accepted_while_redis_was_down_can_be_cancelled(system, monkeypatch):
    client, runtime, sessions, gemini, reconciler, owner = system
    monkeypatch.setattr(runtime.store, "submit_prepared", redis_unreachable)
    job_id = upload(client, owner, png()).json()["job_id"]
    monkeypatch.undo()

    response = client.delete(f"/api/v1/jobs/{job_id}", headers=owner)
    assert response.status_code == 200 and response.json()["status"] == "cancelled"
    assert reconciler.run_once() == {}  # a cancelled row is never dispatched
    runtime.worker.drain()
    assert row(sessions, job_id).status == "cancelled"
    assert gemini.calls == []


def test_cancel_reached_redis_but_the_db_write_was_lost(system):
    client, runtime, sessions, gemini, reconciler, owner = system
    job_id = upload(client, owner, png()).json()["job_id"]
    assert runtime.store.cancel(job_id)  # the API died before its commit
    assert row(sessions, job_id).status == "queued"

    assert reconciler.run_once() == {"synced": 1}
    assert row(sessions, job_id).status == "cancelled"


def test_late_status_hooks_cannot_move_a_job_out_of_a_final_state(system):
    client, runtime, sessions, gemini, _, owner = system
    done = upload(client, owner, png((1, 0, 0))).json()["job_id"]
    runtime.worker.drain()
    gave_up = upload(client, owner, png((2, 0, 0))).json()["job_id"]
    with sessions.begin() as db:
        db.execute(update(ApiJob).where(ApiJob.id == gave_up).values(status="dead"))

    # A worker whose lease expired wakes up and reports on jobs that already ended.
    for status in ("processing", "retrying", "queued", "failed"):
        persist_api_status(sessions, done, status, "stale")
        persist_api_status(sessions, gave_up, status, "stale")
    assert row(sessions, done).status == "done"
    assert row(sessions, gave_up).status == "dead"

    # ...but a real result from a redelivered attempt still beats "dead".
    persist_api_result(sessions, gave_up, {"text": "late but real"})
    assert row(sessions, gave_up).status == "done"


def test_dispatch_is_idempotent_so_a_retried_dispatch_never_double_enqueues(system):
    client, runtime, sessions, gemini, reconciler, owner = system
    job_id = upload(client, owner, png()).json()["job_id"]
    # Pretend the API's "enqueued_at" write was lost after Redis accepted the job.
    with sessions.begin() as db:
        db.execute(update(ApiJob).where(ApiJob.id == job_id).values(enqueued_at=None))

    assert reconciler.run_once()["dispatched"] == 1
    assert runtime.queue.depths()["ready"] == 1
    runtime.worker.drain()
    assert len(gemini.calls) == 1 and row(sessions, job_id).status == "done"


# ── Crash point 2: worker finished in Redis but the DB write failed ───────────────────────

def test_result_write_failure_is_retried_without_a_second_gemini_call(system):
    client, runtime, sessions, gemini, _, owner = system
    job_id = upload(client, owner, png()).json()["job_id"]
    real = runtime.worker.on_result
    failures = {"left": 1}

    def flaky_db(jid, result):
        if failures["left"]:
            failures["left"] -= 1
            raise ConnectionError("database restarting")
        real(jid, result)

    runtime.worker.on_result = flaky_db
    assert runtime.worker.process_one() == "persist_retry"
    assert row(sessions, job_id).status != "done"
    runtime.worker.drain()
    assert row(sessions, job_id).status == "done"
    assert len(gemini.calls) == 1  # the replay came from the Redis result cache


def test_status_hook_failure_never_blocks_processing(system):
    client, runtime, sessions, gemini, _, owner = system
    job_id = upload(client, owner, png()).json()["job_id"]

    def broken(*args):
        raise ConnectionError("database down")

    runtime.worker.on_status = broken
    runtime.worker.drain()
    assert row(sessions, job_id).status == "done"


def test_failures_reach_the_database_without_anyone_reading_the_job(system):
    client, runtime, sessions, gemini, _, owner = system
    gemini.plan = [Permanent("blocked content")]
    failed = upload(client, owner, png((1, 1, 1))).json()["job_id"]
    runtime.worker.drain()
    stored = row(sessions, failed)
    assert (stored.status, stored.error) == ("failed", "blocked content")

    runtime.worker.max_attempts = 1
    gemini.plan = [Transient("gemini 503")]
    dead = upload(client, owner, png((2, 2, 2))).json()["job_id"]
    runtime.worker.drain()
    stored = row(sessions, dead)
    assert stored.status == "dead" and "gemini 503" in stored.error


def test_terminal_state_missed_by_the_hooks_is_copied_by_reconciler(system):
    client, runtime, sessions, gemini, reconciler, owner = system
    runtime.worker.on_status = None
    gemini.plan = [Permanent("unsupported codec")]
    job_id = upload(client, owner, png()).json()["job_id"]
    runtime.worker.drain()
    assert row(sessions, job_id).status == "queued"  # DB never heard about the failure

    assert reconciler.run_once() == {"synced": 1}
    stored = row(sessions, job_id)
    assert (stored.status, stored.error) == ("failed", "unsupported codec")


def test_result_missed_by_the_db_is_copied_by_reconciler(system):
    client, runtime, sessions, gemini, reconciler, owner = system
    runtime.worker.on_result = None
    job_id = upload(client, owner, png()).json()["job_id"]
    runtime.worker.drain()
    assert row(sessions, job_id).result is None

    assert reconciler.run_once() == {"synced": 1}
    assert row(sessions, job_id).result["text"].startswith("scripted")


# ── Crash point 3: Redis loses data ───────────────────────────────────────────────────────

def test_job_forgotten_by_redis_is_resubmitted(system):
    client, runtime, sessions, gemini, reconciler, owner = system
    job_id = upload(client, owner, png()).json()["job_id"]
    wipe_redis_job(runtime, job_id)
    assert runtime.store.status(job_id)["status"] == "unknown"
    # The API reports the durable state instead of inventing "expired".
    assert client.get(f"/api/v1/jobs/{job_id}", headers=owner).json()["status"] == "queued"

    assert reconciler.run_once() == {"recovered": 1}
    assert row(sessions, job_id).recoveries == 1
    runtime.worker.drain()
    assert row(sessions, job_id).status == "done"
    assert len(gemini.calls) == 1


def test_queue_entry_lost_while_job_hash_survived_is_requeued(system):
    client, runtime, sessions, gemini, reconciler, owner = system
    job_id = upload(client, owner, png()).json()["job_id"]
    runtime.r.lrem(runtime.queue.ready, 0, job_id)  # e.g. AOF lost the last LPUSH
    assert runtime.queue.locate(job_id) is None

    assert reconciler.run_once() == {"requeued": 1}
    assert runtime.queue.locate(job_id) == "ready"
    assert reconciler.run_once() == {"unchanged": 1}  # never pushed twice
    assert runtime.queue.depths()["ready"] == 1
    runtime.worker.drain()
    assert row(sessions, job_id).status == "done"


def test_recovery_is_bounded(system):
    client, runtime, sessions, gemini, reconciler, owner = system
    job_id = upload(client, owner, png()).json()["job_id"]
    for _ in range(reconciler.max_recoveries):
        wipe_redis_job(runtime, job_id)
        assert reconciler.run_once() == {"recovered": 1}
    wipe_redis_job(runtime, job_id)
    assert reconciler.run_once() == {"abandoned": 1}
    stored = row(sessions, job_id)
    assert (stored.status, stored.error) == ("failed", LOST)


def test_job_whose_original_is_gone_fails_cleanly_instead_of_looping(system):
    client, runtime, sessions, gemini, reconciler, owner = system
    job_id = upload(client, owner, png()).json()["job_id"]
    wipe_redis_job(runtime, job_id)
    with sessions.begin() as db:
        db.execute(update(MediaAsset).values(purged_at=db.get(ApiJob, job_id).created_at))

    assert reconciler.run_once() == {"failed": 1}
    stored = row(sessions, job_id)
    assert (stored.status, stored.error) == ("failed", MEDIA_GONE)


def test_worker_crash_mid_job_is_reaped_and_finished(system):
    client, runtime, sessions, gemini, _, owner = system
    job_id = upload(client, owner, png()).json()["job_id"]
    assert runtime.queue.claim() == job_id  # a worker claims it, then the process dies
    runtime.r.zadd(runtime.queue.processing, {job_id: 0})  # lease already expired
    runtime.worker.drain()
    assert row(sessions, job_id).status == "done"


# ── Concurrency ───────────────────────────────────────────────────────────────────────────

def test_only_one_reconciler_runs_a_sweep_at_a_time(system):
    client, runtime, sessions, _, reconciler, owner = system
    other = Reconciler(sessions, runtime.store, runtime.queue, client.app.state.storage)
    runtime.r.set(reconciler._lock_key, "held-by-another-worker", ex=30)
    assert other.run_once() == {"skipped": 1}
    runtime.r.delete(reconciler._lock_key)
    assert other.run_once() == {}


def test_concurrent_reconcilers_dispatch_each_job_once(system):
    client, runtime, sessions, gemini, _, owner = system
    for color in range(5):
        crash_before_dispatch(client, runtime, sessions, owner, png((color, 0, 0)))

    sweepers = [
        Reconciler(sessions, runtime.store, runtime.queue, client.app.state.storage, grace_s=0)
        for _ in range(4)
    ]
    threads = [threading.Thread(target=s.run_once) for s in sweepers]
    [t.start() for t in threads]
    [t.join(timeout=30) for t in threads]
    sweepers[0].run_once()  # whichever sweeps were skipped by the lock, one more finishes up
    assert runtime.queue.depths()["ready"] == 5
    runtime.worker.drain()
    assert len(gemini.calls) == 5


def test_worker_runs_reconciler_on_its_loop(system, monkeypatch):
    client, runtime, sessions, gemini, reconciler, owner = system
    calls = []
    runtime.worker.every(3600, lambda: calls.append(reconciler.run_once()))
    runtime.worker.every(3600, lambda: 1 / 0)  # a broken task must not stop the worker
    runtime.worker.maintenance()
    runtime.worker.maintenance()  # not due again yet
    assert calls == [{}]
