"""Build all components from Settings."""
from __future__ import annotations

from dataclasses import dataclass

import redis

from app.config import Settings
from app.gemini_client import build_client
from app.jobs import JobStore
from app.limiter import GeminiLimiter
from app.redis_queue import ReliableQueue
from app.worker import Worker


@dataclass
class Runtime:
    settings: Settings
    r: redis.Redis
    queue: ReliableQueue
    store: JobStore
    limiter: GeminiLimiter
    worker: Worker


def build_runtime(settings: Settings, *, client=None, r: redis.Redis | None = None,
                  **worker_kw) -> Runtime:
    r = r or redis.Redis.from_url(settings.redis_url)
    queue = ReliableQueue(r, prefix=settings.prefix, lease_s=settings.lease_s)
    store = JobStore(r, queue, prefix=settings.prefix, model=settings.model,
                     max_image_mb=settings.max_image_mb,
                     max_video_mb=settings.max_video_upload_mb,
                     result_ttl_s=settings.result_ttl_s, job_ttl_s=settings.job_ttl_s,
                     inflight_ttl_s=settings.inflight_ttl_s)
    limiter = GeminiLimiter(r, settings.model, settings.rpm, settings.rpd, settings.burst,
                            prefix=settings.prefix)
    worker = Worker(queue=queue, store=store, limiter=limiter,
                    client=client or build_client(settings),
                    max_attempts=settings.max_attempts, **worker_kw)
    return Runtime(settings, r, queue, store, limiter, worker)
