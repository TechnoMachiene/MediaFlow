import threading
import time

import pytest

pytestmark = pytest.mark.integration


def test_spacing_for_rpm(make_rt):
    lim = make_rt(rpm=60, burst=1).limiter
    ok1, _ = lim.try_acquire()
    ok2, wait = lim.try_acquire()
    assert ok1 and not ok2 and 0.5 < wait <= 1.0
    time.sleep(wait + 0.05)
    assert lim.try_acquire()[0]


def test_burst(make_rt):
    lim = make_rt(rpm=6, burst=3).limiter
    assert [lim.try_acquire()[0] for _ in range(5)] == [True, True, True, False, False]


def test_concurrent_callers_cannot_exceed_burst(make_rt):
    lim = make_rt(rpm=6, burst=5).limiter
    res, lock = [], threading.Lock()
    barrier = threading.Barrier(20)

    def run():
        barrier.wait()
        ok = lim.try_acquire()[0]
        with lock:
            res.append(ok)

    ts = [threading.Thread(target=run) for _ in range(20)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert sum(res) == 5


def test_daily_cap_blocks_and_reports_reset(make_rt):
    lim = make_rt(rpm=6000, burst=100, rpd=3).limiter
    assert [lim.try_acquire()[0] for _ in range(3)] == [True] * 3
    ok, wait = lim.try_acquire()
    assert not ok and 0 < wait <= 86400
    assert lim.used_today() == 3


def test_concurrent_callers_cannot_exceed_daily_cap(make_rt):
    lim = make_rt(rpm=60000, burst=1000, rpd=10).limiter
    res, lock = [], threading.Lock()
    barrier = threading.Barrier(30)

    def run():
        barrier.wait()
        ok = lim.try_acquire()[0]
        with lock:
            res.append(ok)

    ts = [threading.Thread(target=run) for _ in range(30)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert sum(res) == 10 and lim.used_today() == 10


def test_cooldown_pauses_everyone(make_rt):
    lim = make_rt().limiter
    assert lim.try_acquire()[0]
    lim.cooldown(1.0)
    ok, wait = lim.try_acquire()
    assert not ok and 0 < wait <= 1.0
    time.sleep(1.1)
    assert lim.try_acquire()[0]


def test_models_do_not_share_budget(make_rt):
    a = make_rt(rpm=6, burst=1, model="m-a").limiter
    b = make_rt(rpm=6, burst=1, model="m-b").limiter
    assert a.try_acquire()[0] and b.try_acquire()[0]
