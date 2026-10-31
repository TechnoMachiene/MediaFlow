import threading
import time
import uuid

import pytest

from app.delayqueue import DelayQueue

pytestmark = pytest.mark.integration


def _k(x):
    return f'tdq{uuid.uuid4().hex[:6]}:{x}'


def test_promotes_only_due(redis_client):
    q = DelayQueue(redis_client, delayed=_k('d'), ready=_k('r'))
    now = time.time()
    q.schedule("due1", now - 5)
    q.schedule("due2", now - 1)
    q.schedule("later", now + 3600)
    assert q.promote_due() == 2
    assert redis_client.zcard(q.delayed) == 1
    assert redis_client.llen(q.ready) == 2


def test_second_call_promotes_nothing(redis_client):
    q = DelayQueue(redis_client, delayed=_k('d'), ready=_k('r'))
    q.schedule("due1", time.time() - 1)
    assert q.promote_due() == 1
    assert q.promote_due() == 0


def test_batch_limit(redis_client):
    q = DelayQueue(redis_client, delayed=_k('d'), ready=_k('r'))
    now = time.time()
    for i in range(10):
        q.schedule(f"j{i}", now - 10)
    assert q.promote_due(batch=4) == 4
    assert redis_client.llen(q.ready) == 4


def test_concurrent_pollers_no_duplicates(redis_client):
    q = DelayQueue(redis_client, delayed=_k('d'), ready=_k('r'))
    now = time.time()
    for i in range(200):
        q.schedule(f"j{i}", now - 10)
    barrier = threading.Barrier(6)

    def poll():
        barrier.wait()
        while q.promote_due(batch=7):
            pass

    ts = [threading.Thread(target=poll) for _ in range(6)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    items = [x.decode() for x in redis_client.lrange(q.ready, 0, -1)]
    assert len(items) == 200 and len(set(items)) == 200


def test_sleep_hint_capped(redis_client):
    q = DelayQueue(redis_client, delayed=_k('d'), ready=_k('r'))
    assert q.sleep_hint(cap_s=5) == 5
    q.schedule("far", time.time() + 3600)
    assert q.sleep_hint(cap_s=5) == 5
    q.schedule("soon", time.time() + 1)
    assert 0 <= q.sleep_hint(cap_s=5) <= 1.0
