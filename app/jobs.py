"""Job records + submit with dedup. All decisions made atomically in Redis (Lua)."""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field

import redis

from app.media import inspect_media, make_cache_key, prompt_hash, sha256_file
from app.obs import context as obs_context
from app.obs import tracing
from app.redis_queue import ReliableQueue
from app.system_prompt import SYSTEM_PROMPT

# One atomic step, so a crash or a lost reply can never leave a half-submitted job (a
# `submitted` marker without a queue entry would make every retry a no-op and strand the job).
# KEYS: result, inflight, job hash, ready list, submitted marker.
# ARGV: job_id, inflight_ttl, now, job_ttl, force ('1'/'0'), then the job hash field/value pairs.
SUBMIT_LUA = """
if ARGV[5] ~= '1' and redis.call('EXISTS', KEYS[5]) == 1
   and redis.call('EXISTS', KEYS[3]) == 1 then
  return 'duplicate'
end
local fields = {}
for i = 6, #ARGV do fields[#fields + 1] = ARGV[i] end
redis.call('HSET', KEYS[3], unpack(fields))
redis.call('EXPIRE', KEYS[3], tonumber(ARGV[4]))
redis.call('SET', KEYS[5], '1', 'EX', tonumber(ARGV[4]))
if redis.call('EXISTS', KEYS[1]) == 1 then
  redis.call('HSET', KEYS[3], 'status', 'done', 'cached', '1', 'updated_at', ARGV[3])
  return 'cached'
end
local won = redis.call('SET', KEYS[2], ARGV[1], 'NX', 'EX', tonumber(ARGV[2]))
if not won and redis.call('GET', KEYS[2]) == ARGV[1] then
  -- re-submitting the in-flight leader itself (recovery after Redis lost its job hash)
  redis.call('EXPIRE', KEYS[2], tonumber(ARGV[2]))
  won = true
end
if won then
  redis.call('HSET', KEYS[3], 'status', 'queued', 'updated_at', ARGV[3])
  redis.call('LPUSH', KEYS[4], ARGV[1])
  return 'queued'
end
redis.call('HSET', KEYS[3], 'status', 'waiting', 'updated_at', ARGV[3])
return 'waiting'
"""

DEL_IF_EQ_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[1] then return redis.call('DEL', KEYS[1]) end
return 0
"""

CANCEL_LUA = """
local status = redis.call('HGET', KEYS[1], 'status')
if not status then return 0 end
if status ~= 'queued' and status ~= 'retrying' and status ~= 'waiting' then return 0 end
if redis.call('ZSCORE', KEYS[3], ARGV[1]) then return 0 end
redis.call('LREM', KEYS[2], 0, ARGV[1])
redis.call('ZREM', KEYS[4], ARGV[1])
if redis.call('GET', KEYS[5]) == ARGV[1] then redis.call('DEL', KEYS[5]) end
redis.call('HSET', KEYS[1], 'status', 'cancelled', 'updated_at', ARGV[2])
return 1
"""


@dataclass(frozen=True)
class JobSpec:
    """What the Gemini client needs."""

    job_id: str
    path: str
    kind: str
    mime: str
    prompt: str
    model: str
    sha256: str


@dataclass(frozen=True)
class Job:
    job_id: str
    user_id: str
    path: str
    kind: str
    mime: str
    prompt: str
    model: str
    sha256: str
    cache_key: str
    status: str
    attempts: int
    # Observability: when the job was accepted, and the request/trace that created it.
    created_at: float = 0.0
    request_id: str = ""
    trace_context: dict = field(default_factory=dict)

    def spec(self) -> JobSpec:
        return JobSpec(
            self.job_id, self.path, self.kind, self.mime, self.prompt, self.model, self.sha256
        )


class JobStore:
    def __init__(
        self,
        r: redis.Redis,
        queue: ReliableQueue,
        *,
        prefix: str = "ma",
        model: str,
        max_image_mb: int,
        max_video_mb: int,
        result_ttl_s: int,
        job_ttl_s: int,
        inflight_ttl_s: int,
    ):
        self.r, self.queue, self.model = r, queue, model
        self.p = prefix
        self.max_image_mb, self.max_video_mb = max_image_mb, max_video_mb
        self.result_ttl_s, self.job_ttl_s = result_ttl_s, job_ttl_s
        self.inflight_ttl_s = inflight_ttl_s
        self._submit = r.register_script(SUBMIT_LUA)
        self._del_if_eq = r.register_script(DEL_IF_EQ_LUA)
        self._cancel = r.register_script(CANCEL_LUA)

    # ---- keys
    def _job(self, jid: str) -> str:
        return f"{self.p}:job:{jid}"

    def _result(self, ck: str) -> str:
        return f"{self.p}:result:{ck}"

    def _inflight(self, ck: str) -> str:
        return f"{self.p}:inflight:{ck}"

    def _submitted(self, jid: str) -> str:
        return f"{self.p}:submitted:{jid}"

    # ---- producer
    def submit(
        self,
        user_id: str | int,
        path: str,
        prompt: str = SYSTEM_PROMPT,
        *,
        job_id: str | None = None,
        scope: str = "",
    ) -> dict:
        """Validate -> hash original bytes -> dedup -> enqueue. Returns {job_id, status, cache_key}.
        status: queued (you will call Gemini) | waiting (same upload in flight) | done (cached)."""
        info = inspect_media(path, max_image_mb=self.max_image_mb, max_video_mb=self.max_video_mb)
        return self.submit_prepared(
            user_id,
            media_ref=info.path,
            kind=info.kind,
            mime=info.mime,
            sha256=sha256_file(path),
            prompt=prompt,
            job_id=job_id,
            scope=scope,
        )

    def submit_prepared(
        self,
        user_id: str | int,
        *,
        media_ref: str,
        kind: str,
        mime: str,
        sha256: str,
        prompt: str = SYSTEM_PROMPT,
        job_id: str | None = None,
        scope: str = "",
        force: bool = False,
    ) -> dict:
        """Enqueue media that the caller already validated and hashed.

        `media_ref` is what the worker receives (a path or an s3:// ref). `scope` partitions
        dedup and the result cache, so identical uploads in different workspaces never share
        a Gemini call or reveal each other ("cached": true). The whole submission (job hash,
        `submitted` marker, dedup and LPUSH) is one Lua script, so it either happened or it
        didn't. Re-submitting a `job_id` that Redis still holds is a no-op that returns its
        current status, which makes dispatch safe to retry; `force=True` is for recovering a
        job whose Redis records were lost."""
        ck = make_cache_key(sha256, prompt, self.model)
        if scope:
            ck = f"{scope}:{ck}"
        jid = job_id or uuid.uuid4().hex
        now = f"{time.time():.3f}"
        fields = {
            "user_id": str(user_id),
            "path": media_ref,
            "kind": kind,
            "mime": mime,
            "prompt": prompt,
            "prompt_hash": prompt_hash(prompt),
            "model": self.model,
            "sha256": sha256,
            "cache_key": ck,
            "status": "new",
            "attempts": 0,
            "cached": 0,
            "error": "",
            "created_at": now,
            "updated_at": now,
            # Carried to the worker so its logs and spans join the originating request.
            "request_id": obs_context.get("request_id") or "",
            "trace": json.dumps(tracing.inject()),
        }
        status = self._submit(
            keys=[
                self._result(ck), self._inflight(ck), self._job(jid), self.queue.ready,
                self._submitted(jid),
            ],
            args=[
                jid, self.inflight_ttl_s, now, self.job_ttl_s, "1" if force else "0",
                *(item for pair in fields.items() for item in pair),
            ],
        )
        status = status.decode() if isinstance(status, bytes) else status
        if status == "duplicate":
            current = self.status(jid)
            return {
                "job_id": jid,
                "status": current["status"],
                "cache_key": ck,
                "cached": current["cached"],
                "duplicate": True,
            }
        return {
            "job_id": jid,
            "status": "done" if status == "cached" else status,
            "cache_key": ck,
            "cached": status == "cached",
        }

    # ---- read
    def get(self, jid: str) -> Job | None:
        h = self.r.hgetall(self._job(jid))
        if not h:
            return None
        d = {k.decode(): v.decode() for k, v in h.items()}
        return Job(
            job_id=jid,
            user_id=d["user_id"],
            path=d["path"],
            kind=d["kind"],
            mime=d["mime"],
            prompt=d["prompt"],
            model=d["model"],
            sha256=d["sha256"],
            cache_key=d["cache_key"],
            status=d["status"],
            attempts=int(d["attempts"]),
            created_at=float(d.get("created_at") or 0),
            request_id=d.get("request_id", ""),
            trace_context=json.loads(d["trace"]) if d.get("trace") else {},
        )

    def result_exists(self, cache_key: str) -> bool:
        return bool(self.r.exists(self._result(cache_key)))

    def get_result(self, cache_key: str) -> dict | None:
        raw = self.r.get(self._result(cache_key))
        return json.loads(raw) if raw is not None else None

    def cancel(self, jid: str) -> bool:
        job = self.get(jid)
        if job is None:
            return False
        return bool(
            self._cancel(
                keys=[
                    self._job(jid),
                    self.queue.ready,
                    self.queue.processing,
                    self.queue.deferred,
                    self._inflight(job.cache_key),
                ],
                args=[jid, f"{time.time():.3f}"],
            )
        )

    def status(self, jid: str) -> dict:
        """Caller-facing view. Waiting jobs resolve lazily from the shared result."""
        h = self.r.hgetall(self._job(jid))
        if not h:
            return {"job_id": jid, "status": "unknown"}
        d = {k.decode(): v.decode() for k, v in h.items()}
        status, ck = d["status"], d["cache_key"]
        out = {
            "job_id": jid,
            "status": status,
            "attempts": int(d["attempts"]),
            "cached": d.get("cached") == "1",
            "error": d.get("error") or None,
            "result": None,
        }
        if status in ("done", "waiting"):
            raw = self.r.get(self._result(ck))
            if raw is not None:
                out["status"], out["result"] = "done", json.loads(raw)
            elif status == "waiting" and not self.r.exists(self._inflight(ck)):
                out["status"] = "failed"
                out["error"] = "original upload failed or expired; resubmit"
        return out

    # ---- worker-side transitions
    def mark(self, jid: str, **fields) -> None:
        fields["updated_at"] = f"{time.time():.3f}"
        self.r.hset(self._job(jid), mapping={k: str(v) for k, v in fields.items()})

    def incr_attempts(self, jid: str) -> int:
        return int(self.r.hincrby(self._job(jid), "attempts", 1))

    def release_inflight(self, job: Job) -> None:
        self._del_if_eq(keys=[self._inflight(job.cache_key)], args=[job.job_id])

    def finish_ok(self, job: Job, result: dict) -> None:
        pipe = self.r.pipeline(transaction=True)
        pipe.set(self._result(job.cache_key), json.dumps(result), ex=self.result_ttl_s)
        pipe.delete(self._inflight(job.cache_key))
        pipe.hset(
            self._job(job.job_id),
            mapping={"status": "done", "error": "", "updated_at": f"{time.time():.3f}"},
        )
        pipe.execute()

    def finish_failed(self, job: Job, status: str, error: str) -> None:
        self.release_inflight(job)
        self.mark(job.job_id, status=status, error=error[:500])

    def finish_cached(self, job: Job) -> None:
        self.release_inflight(job)
        self.mark(job.job_id, status="done", cached=1)
