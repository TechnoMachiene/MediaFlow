"""Reliable Redis queue: ready LIST + processing ZSET (leases) + deferred ZSET + dead LIST.

Crash after claim -> lease expires -> reaper puts job back. Nothing is lost.
"""
from __future__ import annotations

import redis

from app.delayqueue import DelayQueue  # component 2: atomic ZSET -> LIST promotion

CLAIM_LUA = """
local t   = redis.call('TIME')
local now = tonumber(t[1]) + tonumber(t[2]) / 1000000
local id  = redis.call('RPOP', KEYS[1])
if not id then return false end
redis.call('ZADD', KEYS[2], now + tonumber(ARGV[1]), id)
return id
"""

REAP_LUA = """
local t   = redis.call('TIME')
local now = tonumber(t[1]) + tonumber(t[2]) / 1000000
local ids = redis.call('ZRANGE', KEYS[1], '-inf', now, 'BYSCORE', 'LIMIT', 0, tonumber(ARGV[1]))
for _, id in ipairs(ids) do
  redis.call('ZREM', KEYS[1], id)
  redis.call('LPUSH', KEYS[2], id)
end
return #ids
"""

EXTEND_LUA = """
local t   = redis.call('TIME')
local now = tonumber(t[1]) + tonumber(t[2]) / 1000000
return redis.call('ZADD', KEYS[1], 'XX', 'CH', now + tonumber(ARGV[2]), ARGV[1])
"""


class ReliableQueue:
    def __init__(self, r: redis.Redis, prefix: str = "ma", lease_s: int = 900):
        self.r = r
        self.lease_s = lease_s
        self.ready = f"{prefix}:ready"
        self.processing = f"{prefix}:processing"
        self.deferred = f"{prefix}:deferred"
        self.dead = f"{prefix}:dead"
        self._claim = r.register_script(CLAIM_LUA)
        self._reap = r.register_script(REAP_LUA)
        self._extend = r.register_script(EXTEND_LUA)
        self._promoter = DelayQueue(r, delayed=self.deferred, ready=self.ready)

    # producer
    def push(self, job_id: str) -> None:
        self.r.lpush(self.ready, job_id)

    # consumer
    def claim(self) -> str | None:
        out = self._claim(keys=[self.ready, self.processing], args=[self.lease_s])
        if not out:
            return None
        return out.decode() if isinstance(out, bytes) else out

    def ack(self, job_id: str) -> None:
        self.r.zrem(self.processing, job_id)

    def extend(self, job_id: str, lease_s: int | None = None) -> bool:
        return bool(self._extend(keys=[self.processing], args=[job_id, lease_s or self.lease_s]))

    def defer(self, job_id: str, delay_s: float) -> None:
        """Atomically: schedule job for later AND release its lease."""
        sec, usec = self.r.time()
        run_at = sec + usec / 1e6 + max(0.0, delay_s)
        pipe = self.r.pipeline(transaction=True)
        pipe.zadd(self.deferred, {job_id: run_at})
        pipe.zrem(self.processing, job_id)
        pipe.execute()

    def dead_letter(self, job_id: str) -> None:
        pipe = self.r.pipeline(transaction=True)
        pipe.lpush(self.dead, job_id)
        pipe.zrem(self.processing, job_id)
        pipe.execute()

    # maintenance
    def reap(self, batch: int = 100) -> int:
        return int(self._reap(keys=[self.processing, self.ready], args=[batch]))

    def promote_deferred(self, batch: int = 100) -> int:
        return self._promoter.promote_due(batch)

    def depths(self) -> dict[str, int]:
        return {
            "ready": self.r.llen(self.ready),
            "processing": self.r.zcard(self.processing),
            "deferred": self.r.zcard(self.deferred),
            "dead": self.r.llen(self.dead),
        }
