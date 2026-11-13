"""Observability: correlation ids end to end, structured logs, traces across the queue, metrics."""

import json
import logging
import socket
import time
import urllib.request

import pytest
from fastapi.testclient import TestClient
from opentelemetry import trace
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from prometheus_client import REGISTRY, generate_latest

from app.errors import Permanent
from app.obs import context, metrics
from app.obs.logs import JsonFormatter, TextFormatter, configure_logging
from app.obs.tracing import configure_tracing
from tests.api_harness import add_member, build_app, login, png, upload
from tests.helpers import ScriptedClient

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def spans():
    configure_tracing("mediaflow-test")  # idempotent; the app would install the same provider
    exporter = InMemorySpanExporter()
    trace.get_tracer_provider().add_span_processor(SimpleSpanProcessor(exporter))
    return exporter


@pytest.fixture
def system(make_rt, tmp_path, monkeypatch, spans):
    monkeypatch.setattr("app.api.routes.JOB_RESULT_WAIT_S", 0)
    monkeypatch.setenv("LOG_FORMAT", "json")
    gemini = ScriptedClient()
    runtime = make_rt(client=gemini)
    application, engine, sessions = build_app(runtime, tmp_path / "media")
    add_member(sessions, "owner@example.com")
    spans.clear()
    with TestClient(application) as client:
        yield client, runtime, gemini, login(client, "owner@example.com")
    engine.dispose()


def json_lines(text: str) -> list[dict]:
    out = []
    for line in text.splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            pass
    return out


def sample(name, **labels):
    return REGISTRY.get_sample_value(name, labels) or 0.0


def test_request_id_is_generated_echoed_or_sanitised(system):
    client, *_ = system
    generated = client.get("/health/live").headers["x-request-id"]
    assert len(generated) == 32
    assert client.get("/health/live", headers={"X-Request-ID": "lb-7f3a:42"}).headers[
        "x-request-id"
    ] == "lb-7f3a:42"
    # Anything that could forge or break a log line is replaced, never echoed.
    hostile = client.get("/health/live", headers={"X-Request-ID": "x\nlevel=error"})
    assert hostile.headers["x-request-id"] != "x\nlevel=error"
    assert len(hostile.headers["x-request-id"]) == 32


def test_one_request_id_and_trace_follow_a_job_through_the_queue(system, spans, capsys):
    client, runtime, gemini, owner = system
    response = client.post(
        "/api/v1/jobs", headers={**owner, "X-Request-ID": "req-upload-1"},
        files={"file": ("cat.png", png(), "image/png")},
    )
    job_id = response.json()["job_id"]
    assert response.headers["x-request-id"] == "req-upload-1"
    stored = runtime.store.get(job_id)
    assert stored.request_id == "req-upload-1" and "traceparent" in stored.trace_context

    runtime.worker.drain()
    logs = json_lines(capsys.readouterr().err)

    access = next(
        e for e in logs if e["logger"] == "http.access" and e["route"] == "/api/v1/jobs"
    )
    assert access["request_id"] == "req-upload-1" and access["status"] == 202
    assert access["user_id"] and access["workspace_id"] == "ws-a"
    assert "@" not in json.dumps(access)  # ids only, never an email
    accepted = next(e for e in logs if e["msg"] == "job accepted")
    finished = next(e for e in logs if e["msg"] == "job done")
    for line in (accepted, finished):
        assert line["job_id"] == job_id and line["request_id"] == "req-upload-1"
    assert finished["service"] and finished["outcome"] == "done" and finished["duration_ms"] >= 0

    # The worker's spans are part of the same trace as the HTTP request that created the job.
    by_name = {}
    for s in spans.get_finished_spans():
        by_name.setdefault(s.name, []).append(s)
    process = next(s for s in by_name["job.process"] if s.attributes["mediaflow.job_id"] == job_id)
    http_trace_ids = {s.context.trace_id for s in spans.get_finished_spans()
                      if s.attributes.get("http.route") == "/api/v1/jobs"}
    assert process.context.trace_id in http_trace_ids
    assert finished["trace_id"] == format(process.context.trace_id, "032x")
    analyse = next(s for s in by_name["gemini.analyse"]
                   if s.context.trace_id == process.context.trace_id)
    assert analyse.parent.span_id == process.context.span_id
    assert process.attributes["mediaflow.outcome"] == "done"


def test_failures_are_visible_in_logs_traces_and_metrics(system, spans, capsys):
    client, runtime, gemini, owner = system
    before = sample("mediaflow_job_outcomes_total", outcome="failed")
    calls_before = sample("mediaflow_gemini_request_seconds_count", kind="image",
                          result="permanent")
    gemini.plan = [Permanent("blocked content")]
    job_id = upload(client, owner, png((3, 3, 3))).json()["job_id"]
    runtime.worker.drain()

    assert sample("mediaflow_job_outcomes_total", outcome="failed") == before + 1
    assert sample("mediaflow_gemini_request_seconds_count", kind="image",
                  result="permanent") == calls_before + 1
    line = next(e for e in json_lines(capsys.readouterr().err) if e["msg"] == "job failed")
    assert line["level"] == "warning" and line["job_id"] == job_id
    process = next(s for s in spans.get_finished_spans() if s.name == "job.process"
                   and s.attributes["mediaflow.job_id"] == job_id)
    assert process.status.status_code.name == "ERROR"
    assert any(e.name == "exception" for s in spans.get_finished_spans()
               if s.name == "gemini.analyse" for e in s.events)


def test_job_timing_and_http_metrics(system):
    client, runtime, gemini, owner = system
    waits = sample("mediaflow_job_queue_wait_seconds_count")
    e2e = sample("mediaflow_job_end_to_end_seconds_count")
    gets = sample("mediaflow_http_requests_total", method="GET",
                  route="/api/v1/jobs/{job_id}", status="200")
    ids = [upload(client, owner, png((i, 9, 9))).json()["job_id"] for i in range(2)]
    runtime.worker.drain()
    for job_id in ids:
        client.get(f"/api/v1/jobs/{job_id}", headers=owner)

    assert sample("mediaflow_job_queue_wait_seconds_count") == waits + 2
    assert sample("mediaflow_job_end_to_end_seconds_count") == e2e + 2
    # Two different job ids, one label value: the route template keeps cardinality bounded.
    assert sample("mediaflow_http_requests_total", method="GET",
                  route="/api/v1/jobs/{job_id}", status="200") == gets + 2
    exposition = generate_latest().decode()
    assert ids[0] not in exposition and "owner@example.com" not in exposition


def test_queue_gauges_report_backlog_and_age(system):
    client, runtime, gemini, owner = system
    metrics.watch_queue(runtime)
    assert sample("mediaflow_queue_oldest_ready_age_seconds") == 0
    job_id = upload(client, owner, png()).json()["job_id"]
    runtime.r.hset(runtime.store._job(job_id), "created_at", f"{time.time() - 120:.3f}")

    assert sample("mediaflow_queue_depth", state="ready") == 1
    assert 119 <= sample("mediaflow_queue_oldest_ready_age_seconds") < 180
    assert sample("mediaflow_redis_up") == 1
    runtime.worker.drain()
    assert sample("mediaflow_queue_depth", state="ready") == 0
    assert sample("mediaflow_queue_oldest_ready_age_seconds") == 0
    assert sample("mediaflow_gemini_calls_today") >= 1


def test_redis_outage_is_reported_not_raised(system, monkeypatch):
    _, runtime, *_ = system
    metrics.watch_queue(runtime)

    def down():
        raise ConnectionError("redis down")

    monkeypatch.setattr(runtime.queue, "depths", down)
    assert sample("mediaflow_redis_up") == 0
    assert sample("mediaflow_queue_depth", state="ready") == 0  # absent, not an exception


def test_worker_heartbeat_and_reconciler_counters(system):
    client, runtime, gemini, owner = system
    before = time.time()
    runtime.worker.maintenance()
    assert sample("mediaflow_worker_heartbeat_timestamp_seconds") >= int(before)

    from app.api.reconcile import Reconciler

    sessions = client.app.state.session_factory
    job_id = upload(client, owner, png()).json()["job_id"]
    runtime.r.lrem(runtime.queue.ready, 0, job_id)  # lose the queue entry
    requeued = sample("mediaflow_reconciler_actions_total", action="requeued")
    Reconciler(sessions, runtime.store, runtime.queue, client.app.state.storage,
               grace_s=0).run_once()
    assert sample("mediaflow_reconciler_actions_total", action="requeued") == requeued + 1


def test_metrics_are_served_on_their_own_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    metrics._server_port = None
    metrics.serve(port)
    body = urllib.request.urlopen(f"http://127.0.0.1:{port}/metrics", timeout=5).read().decode()
    assert "mediaflow_job_outcomes_total" in body
    metrics.serve(port + 1)  # once per process: a second call is a no-op
    assert metrics._server_port == port


def test_sql_statements_are_traced_without_their_parameters(spans):
    from sqlalchemy import create_engine, text

    from app.obs.tracing import instrument_engine, span

    engine = create_engine("sqlite://")
    instrument_engine(engine)
    instrument_engine(engine)  # idempotent: no duplicate spans
    with span("request") as parent, engine.connect() as conn:
        conn.execute(text("SELECT :secret AS x"), {"secret": "hunter2@example.com"})
    sql = [s for s in spans.get_finished_spans() if s.name == "SELECT sqlite"]
    assert len(sql) == 1 and sql[0].parent.span_id == parent.get_span_context().span_id
    assert sql[0].attributes["db.statement"] == "SELECT ? AS x"
    assert "hunter2" not in json.dumps(dict(sql[0].attributes))
    engine.dispose()


def test_orphan_infrastructure_calls_are_not_traced(spans):
    """Idle worker polls, metrics scrapes and healthchecks must not each become a trace."""
    from app.obs.tracing import tracer

    with tracer().start_as_current_span("orphan-redis-call", kind=trace.SpanKind.CLIENT) as s:
        assert not s.is_recording()
    with tracer().start_as_current_span("job.process") as root:
        with tracer().start_as_current_span("HGETALL", kind=trace.SpanKind.CLIENT) as child:
            assert root.is_recording() and child.is_recording()
    names = {s.name for s in spans.get_finished_spans()}
    assert "orphan-redis-call" not in names and {"job.process", "HGETALL"} <= names


def test_log_formats_and_idempotent_setup(capsys):
    record = logging.makeLogRecord({
        "name": "worker", "levelname": "ERROR", "levelno": 40, "msg": "boom %s",
        "args": ("x",), "job_id": "j1",
    })
    try:
        raise ValueError("bad")
    except ValueError:
        import sys

        record.exc_info = sys.exc_info()
    line = json.loads(JsonFormatter().format(record))
    assert line["msg"] == "boom x" and line["job_id"] == "j1" and "ValueError" in line["exc"]
    assert "[job_id=j1]" in TextFormatter().format(record)

    configure_logging("svc-a", fmt="json")
    configure_logging("svc-b", fmt="json")
    ours = [h for h in logging.getLogger().handlers if getattr(h, "_mediaflow_handler", False)]
    assert len(ours) == 1
    with context.bind(request_id="r-1", workspace_id=None):
        logging.getLogger("t").warning("hello", extra={"n": 3})
    out = json_lines(capsys.readouterr().err)[-1]
    assert out == {**out, "service": "svc-b", "request_id": "r-1", "msg": "hello", "n": 3}
    assert "workspace_id" not in out
