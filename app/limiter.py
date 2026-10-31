"""Global Gemini limiter shared by ALL workers via Redis.

Token bucket (per minute rate) + daily counter (Gemini daily quota resets at midnight Pacific)
+ cooldown switch (after a 429 the whole fleet pauses).
Numbers come from YOUR AI Studio rate-limit page. Never hardcode.
"""
from __future__ import annotations

import random
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import redis

BUCKET_LUA = """
-- KEYS[1]=bucket hash, KEYS[2]=daily counter
-- ARGV: rate/sec, burst, daily limit
-- returns {code, wait}   code 1=allowed, 0=wait for tokens, 2=daily quota used up
local t   = redis.call('TIME')
local now = tonumber(t[1]) + tonumber(t[2]) / 1000000
local rate  = tonumber(ARGV[1])
local cap   = tonumber(ARGV[2])
local limit = tonumber(ARGV[3])
if tonumber(redis.call('GET', KEYS[2]) or '0') >= limit then
  return {2, '0'}
end
local d = redis.call('HMGET', KEYS[1], 'tokens', 'ts')
local tokens = tonumber(d[1]); local ts = tonumber(d[2])
if tokens == nil then tokens = cap; ts = now end
tokens = math.min(cap, tokens + (now - ts) * rate)
local code = 0
local wait = 0
if tokens >= 1 then
  tokens = tokens - 1
  code = 1
  redis.call('INCR', KEYS[2])
  redis.call('EXPIRE', KEYS[2], 172800)
else
  wait = (1 - tokens) / rate
end
redis.call('HSET', KEYS[1], 'tokens', tostring(tokens), 'ts', tostring(now))
redis.call('EXPIRE', KEYS[1], math.ceil(cap / rate) * 2 + 1)
return {code, tostring(wait)}
"""

PACIFIC = ZoneInfo("America/Los_Angeles")


def backoff_s(attempt: int, base: float = 2.0, cap: float = 300.0) -> float:
    """Full-jitter exponential backoff. attempt starts at 1."""
    return random.uniform(base / 2, min(cap, base * (2 ** attempt)))


class GeminiLimiter:
    def __init__(self, r: redis.Redis, model: str, rpm: int, rpd: int, burst: int = 1,
                 prefix: str = "ma"):
        self.r, self.model, self.rpm, self.rpd, self.burst = r, model, rpm, rpd, burst
        self._bucket = r.register_script(BUCKET_LUA)
        base = f"{prefix}:gemini:{model}"
        self._bucket_key = f"{base}:bucket"
        self._cooldown_key = f"{base}:cooldown"
        self._rpd_prefix = f"{base}:rpd"

    def _rpd_key(self) -> str:
        return f"{self._rpd_prefix}:{datetime.now(PACIFIC).strftime('%Y-%m-%d')}"

    @staticmethod
    def seconds_to_reset() -> float:
        now = datetime.now(PACIFIC)
        nxt = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        return (nxt - now).total_seconds()

    def cooldown(self, seconds: float) -> None:
        """Pause every worker for `seconds` (call after a 429)."""
        self.r.set(self._cooldown_key, "1", px=max(1, int(seconds * 1000)))

    def try_acquire(self) -> tuple[bool, float]:
        """(allowed, seconds_to_wait). Atomic across all workers."""
        ttl_ms = self.r.pttl(self._cooldown_key)
        if ttl_ms and ttl_ms > 0:
            return False, ttl_ms / 1000.0
        code, wait = self._bucket(
            keys=[self._bucket_key, self._rpd_key()],
            args=[self.rpm / 60.0, self.burst, self.rpd],
        )
        if code == 1:
            return True, 0.0
        if code == 2:
            return False, self.seconds_to_reset()
        return False, float(wait)

    def used_today(self) -> int:
        return int(self.r.get(self._rpd_key()) or 0)
