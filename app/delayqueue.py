"""Component 2: atomic promotion of due jobs, ZSET -> LIST."""
from __future__ import annotations

import time

import redis

PROMOTE_LUA = """
local t   = redis.call('TIME')
local now = tonumber(t[1]) + tonumber(t[2]) / 1000000
local ids = redis.call('ZRANGE', KEYS[1], '-inf', now, 'BYSCORE', 'LIMIT', 0, tonumber(ARGV[1]))
for _, id in ipairs(ids) do
  redis.call('ZREM', KEYS[1], id)
  redis.call('LPUSH', KEYS[2], id)
end
return #ids
"""


class DelayQueue:
    def __init__(self, r: redis.Redis, delayed: str = "jobs:delayed", ready: str = "jobs:ready"):
        self.r, self.delayed, self.ready = r, delayed, ready
        self._promote = r.register_script(PROMOTE_LUA)

    def schedule(self, job_id: str, run_at_ts: float) -> None:
        self.r.zadd(self.delayed, {job_id: run_at_ts})

    def promote_due(self, batch: int = 100) -> int:
        return int(self._promote(keys=[self.delayed, self.ready], args=[batch]))

    def sleep_hint(self, cap_s: float = 5.0) -> float:
        head = self.r.zrange(self.delayed, 0, 0, withscores=True)
        if not head:
            return cap_s
        return min(max(0.0, head[0][1] - time.time()), cap_s)
