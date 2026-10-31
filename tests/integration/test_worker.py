import threading
import time

import pytest

from app.errors import Permanent, RateLimited, Transient
from tests.helpers import ScriptedClient

pytestmark = pytest.mark.integration
NOW = dict(sleep=lambda s: None, backoff=lambda a: 0.0)


def rt_with(make_rt, plan=None, **over):
    fc = ScriptedClient(plan=plan)
    return make_rt(client=fc, worker_kw=NOW, **over), fc


def test_idle_and_missing(make_rt):
    rt, _ = rt_with(make_rt)
    assert rt.worker.process_one() == "idle"
    rt.queue.push("ghost-id")
    assert rt.worker.process_one() == "missing"
    assert rt.queue.depths()["processing"] == 0


def test_happy_path(make_rt, make_image, redis_client):
    rt, fc = rt_with(make_rt)
    out = rt.store.submit(1, make_image(), "d")
    assert rt.worker.process_one() == "done"
    view = rt.store.status(out["job_id"])
    assert view["status"] == "done" and "scripted analysis" in view["result"]["text"]
    assert view["attempts"] == 1 and len(fc.calls) == 1
    d = rt.queue.depths()
    assert (d["ready"], d["processing"], d["deferred"], d["dead"]) == (0, 0, 0, 0)
    assert redis_client.exists(f"{rt.settings.prefix}:inflight:{out['cache_key']}") == 0


def test_video_job(make_rt, make_video):
    rt, _ = rt_with(make_rt)
    out = rt.store.submit(1, make_video(), "d")
    assert rt.worker.process_one() == "done"
    assert rt.store.status(out["job_id"])["result"]["kind"] == "video"


def test_rate_limited_then_success(make_rt, make_image):
    rt, fc = rt_with(make_rt, plan=[RateLimited("429")])
    out = rt.store.submit(1, make_image(), "d")
    assert rt.worker.process_one() == "retry"
    assert rt.queue.depths()["deferred"] == 1
    assert rt.store.status(out["job_id"])["status"] == "retrying"
    time.sleep(0.05)
    rt.worker.maintenance()
    assert rt.worker.process_one() == "done"
    assert rt.store.status(out["job_id"])["attempts"] == 2 and len(fc.calls) == 2


def test_429_triggers_fleet_cooldown(make_rt, make_image):
    rt, _ = rt_with(make_rt, plan=[RateLimited("429")])
    rt.worker.backoff = lambda a: 30.0
    rt.store.submit(1, make_image(), "d")
    assert rt.worker.process_one() == "retry"
    ok, wait = rt.limiter.try_acquire()
    assert not ok and 20 < wait <= 30


def test_transient_exhausts_to_dead(make_rt, make_image, redis_client):
    rt, fc = rt_with(make_rt, plan=[Transient("5xx")] * 3, max_attempts=3)
    out = rt.store.submit(1, make_image(), "d")
    results = []
    for _ in range(3):
        results.append(rt.worker.process_one())
        time.sleep(0.02)
        rt.worker.maintenance()
    assert results == ["retry", "retry", "dead"]
    d = rt.queue.depths()
    assert (d["dead"], d["processing"], d["ready"], d["deferred"]) == (1, 0, 0, 0)
    view = rt.store.status(out["job_id"])
    assert view["status"] == "dead" and "gave up" in view["error"]
    assert redis_client.exists(f"{rt.settings.prefix}:inflight:{out['cache_key']}") == 0


def test_dead_job_can_be_resubmitted(make_rt, make_image):
    rt, _ = rt_with(make_rt, plan=[Transient("x")] * 1, max_attempts=1)
    p = make_image()
    rt.store.submit(1, p, "d")
    assert rt.worker.process_one() == "dead"
    assert rt.store.submit(1, p, "d")["status"] == "queued"
    assert rt.worker.process_one() == "done"


def test_permanent_fails_fast(make_rt, make_image):
    rt, fc = rt_with(make_rt, plan=[Permanent("bad request")])
    out = rt.store.submit(1, make_image(), "d")
    assert rt.worker.process_one() == "failed"
    view = rt.store.status(out["job_id"])
    assert view["status"] == "failed" and view["error"] == "bad request" and view["attempts"] == 1
    assert rt.queue.depths()["processing"] == 0 and len(fc.calls) == 1


def test_unexpected_exception_is_retried(make_rt, make_image):
    rt, _ = rt_with(make_rt, plan=[RuntimeError("bug")])
    rt.store.submit(1, make_image(), "d")
    assert rt.worker.process_one() == "retry"


def test_followers_share_one_gemini_call(make_rt, make_image):
    rt, fc = rt_with(make_rt)
    p = make_image()
    ids = [rt.store.submit(u, p, "d")["job_id"] for u in range(5)]
    assert rt.worker.process_one() == "done" and rt.worker.process_one() == "idle"
    views = [rt.store.status(i) for i in ids]
    assert all(v["status"] == "done" for v in views)
    assert len({v["result"]["text"] for v in views}) == 1 and len(fc.calls) == 1


def test_crash_after_claim_is_recovered(make_rt, make_image):
    rt, fc = rt_with(make_rt)
    rt.queue.lease_s = 1
    out = rt.store.submit(1, make_image(), "d")
    assert rt.queue.claim() == out["job_id"]  # worker "dies" here, never acks
    assert rt.worker.process_one() == "idle"
    time.sleep(1.2)
    rt.worker.maintenance()
    assert rt.worker.process_one() == "done"
    assert len(fc.calls) == 1


def test_replay_after_result_saved_does_not_call_gemini_again(make_rt, make_image):
    rt, fc = rt_with(make_rt)
    rt.queue.lease_s = 1
    out = rt.store.submit(1, make_image(), "d")
    jid = rt.queue.claim()
    rt.store.finish_ok(rt.store.get(jid), {"text": "saved"})  # crash before ack
    time.sleep(1.2)
    rt.worker.maintenance()
    assert rt.worker.process_one() == "cached"
    assert fc.calls == [] and rt.store.status(out["job_id"])["result"] == {"text": "saved"}
    assert rt.queue.depths()["processing"] == 0


def test_long_wait_defers_instead_of_blocking(make_rt, make_image):
    rt, fc = rt_with(make_rt)
    out = rt.store.submit(1, make_image(), "d")
    rt.limiter.cooldown(60)
    assert rt.worker.process_one() == "deferred"
    d = rt.queue.depths()
    assert (d["deferred"], d["processing"], d["ready"]) == (1, 0, 0)
    assert fc.calls == [] and rt.store.status(out["job_id"])["status"] == "queued"
    assert rt.store.status(out["job_id"])["attempts"] == 0  # limiter waits are not attempts


def test_daily_cap_defers_second_job(make_rt, make_image):
    rt, fc = rt_with(make_rt, rpd=1)
    rt.store.submit(1, make_image(), "d")
    rt.store.submit(1, make_image(), "d")
    assert rt.worker.process_one() == "done"
    assert rt.worker.process_one() == "deferred"
    assert len(fc.calls) == 1 and rt.queue.depths()["deferred"] == 1


def test_short_wait_blocks_then_proceeds(make_rt, make_image):
    fc = ScriptedClient()
    rt = make_rt(client=fc, rpm=600, burst=1,
                 worker_kw=dict(max_inline_wait_s=5.0, backoff=lambda a: 0.0))
    rt.store.submit(1, make_image(), "d")
    rt.store.submit(1, make_image(), "d")
    assert rt.worker.process_one() == "done"
    assert rt.worker.process_one() == "done"  # waited ~0.1s inside the limiter
    assert len(fc.calls) == 2


def test_run_loop_processes_until_stopped(make_rt, make_image):
    rt, _ = rt_with(make_rt)
    out = rt.store.submit(1, make_image(), "d")
    stop = threading.Event()
    t = threading.Thread(target=rt.worker.run, args=(stop, 0.05))
    t.start()
    deadline = time.time() + 5
    while time.time() < deadline and rt.store.status(out["job_id"])["status"] != "done":
        time.sleep(0.05)
    stop.set()
    t.join(timeout=3)
    assert rt.store.status(out["job_id"])["status"] == "done" and not t.is_alive()
