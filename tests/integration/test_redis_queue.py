import threading
import time

import pytest

pytestmark = pytest.mark.integration


def test_fifo_order_and_empty(make_rt):
    q = make_rt().queue
    assert q.claim() is None
    for j in ("a", "b", "c"):
        q.push(j)
    assert [q.claim(), q.claim(), q.claim(), q.claim()] == ["a", "b", "c", None]


def test_ack_releases_lease(make_rt):
    q = make_rt().queue
    q.push("a")
    assert q.claim() == "a" and q.depths()["processing"] == 1
    q.ack("a")
    q.ack("a")  # idempotent
    assert q.depths()["processing"] == 0


def test_reap_returns_expired_lease(make_rt):
    q = make_rt(lease_s=5).queue
    q.lease_s = 1
    q.push("a")
    assert q.claim() == "a"
    assert q.reap() == 0  # lease still valid
    time.sleep(1.2)
    assert q.reap() == 1
    assert q.claim() == "a"


def test_extend_keeps_job_leased(make_rt):
    q = make_rt().queue
    q.lease_s = 1
    q.push("a")
    q.claim()
    assert q.extend("a", lease_s=30) is True
    time.sleep(1.2)
    assert q.reap() == 0
    assert q.extend("never-claimed") is False


def test_defer_then_promote(make_rt):
    q = make_rt().queue
    q.push("a")
    q.claim()
    q.defer("a", 0.3)
    d = q.depths()
    assert (d["ready"], d["processing"], d["deferred"]) == (0, 0, 1)
    assert q.promote_deferred() == 0  # not due yet
    time.sleep(0.5)
    assert q.promote_deferred() == 1
    assert q.claim() == "a"


def test_dead_letter(make_rt):
    q = make_rt().queue
    q.push("a")
    q.claim()
    q.dead_letter("a")
    d = q.depths()
    assert (d["dead"], d["processing"], d["ready"]) == (1, 0, 0)


def test_concurrent_claims_never_duplicate(make_rt):
    q = make_rt().queue
    for i in range(300):
        q.push(f"j{i}")
    got, lock = [], threading.Lock()
    barrier = threading.Barrier(8)

    def run():
        barrier.wait()
        while (j := q.claim()) is not None:
            with lock:
                got.append(j)

    ts = [threading.Thread(target=run) for _ in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len(got) == 300 and len(set(got)) == 300
