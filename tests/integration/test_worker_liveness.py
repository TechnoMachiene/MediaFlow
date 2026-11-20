"""Worker liveness: the API must not hold an upload open for minutes when nobody is consuming."""

import threading
import time

import pytest
from fastapi.testclient import TestClient
from prometheus_client import REGISTRY

from app.obs import metrics
from tests.api_harness import add_member, build_app, login, png, upload
from tests.helpers import ScriptedClient

pytestmark = pytest.mark.integration


def test_heartbeats_expire_and_retire(make_rt):
    rt = make_rt()
    q = rt.queue
    assert q.live_workers() == 0
    q.heartbeat("w1", 30)
    q.heartbeat("w2", 0.2)
    assert q.live_workers() == 2
    time.sleep(0.3)
    assert q.live_workers() == 1  # w2 stopped beating
    q.heartbeat("w1", 30)  # beating again also prunes expired entries
    assert rt.r.zcard(q.workers) == 1
    q.retire("w1")
    assert q.live_workers() == 0


def test_worker_beats_while_idle_holds_a_lease_while_busy_and_retires_on_stop(make_rt, make_image):
    gemini = ScriptedClient(delay_s=0.3)
    rt = make_rt(client=gemini)
    worker, q = rt.worker, rt.queue
    stop = threading.Event()
    thread = threading.Thread(target=worker.run, args=(stop,), kwargs={"poll_s": 0.01})
    thread.start()
    try:
        deadline = time.monotonic() + 5
        while q.live_workers() == 0 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert q.live_workers() == 1
        idle_expiry = rt.r.zscore(q.workers, worker.worker_id)

        rt.store.submit(1, make_image(), "p")
        deadline = time.monotonic() + 5
        while not gemini.calls and time.monotonic() < deadline:
            time.sleep(0.01)
        busy_expiry = rt.r.zscore(q.workers, worker.worker_id)
        assert busy_expiry - idle_expiry > q.lease_s - 60  # claims the whole lease while busy
    finally:
        stop.set()
        thread.join(timeout=5)
    assert q.live_workers() == 0  # left cleanly: nobody waits on a stopped worker


@pytest.fixture
def api(make_rt, tmp_path, monkeypatch):
    monkeypatch.setattr("app.api.routes.JOB_RESULT_WAIT_S", 30)
    runtime = make_rt(client=ScriptedClient())
    application, engine, sessions = build_app(runtime, tmp_path / "media")
    add_member(sessions, "owner@example.com")
    with TestClient(application) as client:
        # These tests are about waiting for the draft, which a client asks for explicitly.
        yield client, runtime, {**login(client, "owner@example.com"), "Prefer": "wait=30"}
    engine.dispose()


def test_upload_returns_at_once_when_no_worker_is_alive(api, capsys):
    client, runtime, owner = api
    started = time.monotonic()
    response = upload(client, owner, png())
    elapsed = time.monotonic() - started
    assert response.status_code == 202 and response.json()["status"] == "queued"
    assert elapsed < 3, f"held the request for {elapsed:.1f}s with no worker"  # not 30 s
    assert runtime.queue.depths()["ready"] == 1  # still safely queued for later
    assert "no live worker" in capsys.readouterr().err


def test_upload_waits_while_a_worker_is_alive(api, monkeypatch):
    client, runtime, owner = api
    monkeypatch.setattr("app.api.routes.JOB_RESULT_WAIT_S", 2.5)
    runtime.queue.heartbeat("busy-elsewhere", 60)  # alive, but not getting to this job
    started = time.monotonic()
    assert upload(client, owner, png()).status_code == 202
    assert time.monotonic() - started >= 2.4  # it did wait for the result


def test_upload_stops_waiting_when_the_worker_dies_mid_wait(api, monkeypatch):
    client, runtime, owner = api
    runtime.queue.heartbeat("about-to-crash", 1.5)
    started = time.monotonic()
    assert upload(client, owner, png()).status_code == 202
    assert time.monotonic() - started < 6  # noticed within a recheck, not after 30 s


def test_live_workers_are_exported(api):
    _, runtime, _ = api
    metrics.watch_queue(runtime)
    runtime.queue.heartbeat("w1", 30)
    runtime.queue.heartbeat("w2", 30)
    assert REGISTRY.get_sample_value("mediaflow_workers_alive") == 2
