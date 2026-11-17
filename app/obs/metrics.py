"""Prometheus metrics. Served on a separate, private port (METRICS_PORT) - never on the public
API port - by every API and worker process. Prometheus scrapes them over the private network.

Label sets are deliberately small and bounded: route *templates* (never raw paths), job
outcomes, error kinds. No user, workspace or job ids ever become labels.

Naming: mediaflow_<thing>_<unit>; *_total are counters, *_seconds histograms are in seconds.
"""

from __future__ import annotations

import logging
import time

from prometheus_client import Counter, Gauge, Histogram, start_http_server
from prometheus_client.core import GaugeMetricFamily
from prometheus_client.registry import REGISTRY

log = logging.getLogger("metrics")

# Buckets for "how long a person waits": 0.1 s up to an hour.
_WAIT = (0.1, 0.5, 1, 2, 5, 10, 30, 60, 120, 300, 600, 1800, 3600)
_FAST = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 300)

# ── API ────────────────────────────────────────────────────────────────────────────────────
HTTP_REQUESTS = Counter(
    "mediaflow_http_requests_total", "HTTP requests handled.", ["method", "route", "status"]
)
HTTP_DURATION = Histogram(
    "mediaflow_http_request_duration_seconds", "HTTP request latency.", ["method", "route"],
    buckets=_FAST,
)
JOBS_ACCEPTED = Counter(
    "mediaflow_jobs_accepted_total", "Uploads accepted, by dispatch result.", ["status"]
)

# ── Worker ─────────────────────────────────────────────────────────────────────────────────
JOB_OUTCOMES = Counter(
    "mediaflow_job_outcomes_total",
    "Results of each worker attempt (done, cached, retry, deferred, failed, dead, ...).",
    ["outcome"],
)
JOB_QUEUE_WAIT = Histogram(
    "mediaflow_job_queue_wait_seconds",
    "Time from upload accepted to a worker first picking the job up.",
    buckets=_WAIT,
)
JOB_END_TO_END = Histogram(
    "mediaflow_job_end_to_end_seconds",
    "Time from upload accepted to the result being stored (the user-facing SLI).",
    buckets=_WAIT,
)
GEMINI_DURATION = Histogram(
    "mediaflow_gemini_request_seconds", "Duration of one analysis call, by result.",
    ["kind", "result"], buckets=_FAST,
)
WORKER_HEARTBEAT = Gauge(
    "mediaflow_worker_heartbeat_timestamp_seconds",
    "Unix time of the worker loop's last iteration; stale means a stuck worker.",
)
RECONCILER_ACTIONS = Counter(
    "mediaflow_reconciler_actions_total",
    "Repairs made by the DB<->Redis reconciler (see docs/JOB_LIFECYCLE.md).", ["action"],
)
PUBLISH_OUTCOMES = Counter(
    "mediaflow_publish_outcomes_total",
    "Publishing delivery outcomes by provider (published, processing, retry, rejected, "
    "authorization, unknown_outcome, ...).", ["provider", "outcome"],
)
RETENTION_PURGED = Counter(
    "mediaflow_retention_purged_total", "Original uploads deleted by the retention policy."
)


class QueueCollector:
    """Live queue state read from Redis at scrape time, so it is never stale and costs
    nothing between scrapes. Registered by the API process only, to avoid duplicates."""

    def __init__(self):
        self.runtime = None

    def collect(self):
        rt = self.runtime
        if rt is None:
            return
        try:
            depths = rt.queue.depths()
            alive = rt.queue.live_workers()
            oldest = _oldest_ready_age(rt)
            calls = rt.limiter.used_today()
            cooldown = max(0, rt.r.pttl(rt.limiter._cooldown_key) or 0) / 1000
        except Exception:  # Redis down: report it, don't fail the whole scrape
            log.warning("queue metrics unavailable", exc_info=True)
            up = GaugeMetricFamily("mediaflow_redis_up", "Redis answered the last scrape.")
            up.add_metric([], 0)
            yield up
            return
        up = GaugeMetricFamily("mediaflow_redis_up", "Redis answered the last scrape.")
        up.add_metric([], 1)
        yield up
        workers = GaugeMetricFamily(
            "mediaflow_workers_alive",
            "Workers with a current heartbeat in Redis (includes embedded workers).",
        )
        workers.add_metric([], alive)
        yield workers
        depth = GaugeMetricFamily(
            "mediaflow_queue_depth", "Jobs in each queue structure.", labels=["state"]
        )
        for state, value in depths.items():
            depth.add_metric([state], value)
        yield depth
        age = GaugeMetricFamily(
            "mediaflow_queue_oldest_ready_age_seconds",
            "How long the next job to be picked up has been waiting since upload (0 if none).",
        )
        age.add_metric([], oldest)
        yield age
        used = GaugeMetricFamily(
            "mediaflow_gemini_calls_today", "Gemini calls counted against today's quota."
        )
        used.add_metric([], calls)
        yield used
        limit = GaugeMetricFamily(
            "mediaflow_gemini_daily_limit", "Configured Gemini requests-per-day (GEMINI_RPD)."
        )
        limit.add_metric([], rt.limiter.rpd)
        yield limit
        cool = GaugeMetricFamily(
            "mediaflow_gemini_cooldown_seconds",
            "Seconds left in a fleet-wide pause after a 429 (0 when calls flow).",
        )
        cool.add_metric([], cooldown)
        yield cool


def _oldest_ready_age(rt) -> float:
    # LPUSH on submit, RPOP on claim: index -1 is the job that has waited longest in line.
    head = rt.r.lindex(rt.queue.ready, -1)
    if head is None:
        return 0.0
    head = head.decode() if isinstance(head, bytes) else head
    created = rt.r.hget(rt.store._job(head), "created_at")
    return max(0.0, time.time() - float(created)) if created else 0.0


_queue_collector: QueueCollector | None = None


def watch_queue(runtime) -> None:
    """Expose live queue gauges for `runtime` (re-pointable; registered once per process)."""
    global _queue_collector
    if _queue_collector is None:
        _queue_collector = QueueCollector()
        REGISTRY.register(_queue_collector)
    _queue_collector.runtime = runtime


def metrics_port() -> int | None:
    """METRICS_PORT from the environment (unset or 0: no metrics endpoint)."""
    import os

    raw = os.environ.get("METRICS_PORT", "").strip()
    try:
        return int(raw) if raw else None
    except ValueError:
        log.warning("METRICS_PORT must be a port number", extra={"value": raw})
        return None


_server_port: int | None = None


def serve(port: int | None) -> None:
    """Start the private /metrics endpoint once per process. Port 0/None disables it."""
    global _server_port
    if not port or _server_port is not None:
        return
    try:
        start_http_server(port, addr="0.0.0.0")
    except OSError as e:  # e.g. a second process on the same host: keep serving traffic
        log.warning("metrics port unavailable", extra={"port": port, "error": str(e)})
        return
    _server_port = port
    log.info("metrics endpoint listening", extra={"port": port})
