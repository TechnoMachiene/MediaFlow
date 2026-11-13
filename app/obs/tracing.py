"""OpenTelemetry tracing, including across the Redis queue.

A tracer provider is always installed, so every log line inside a request or job carries a real
trace_id even with no collector. Spans are *exported* only when OTEL_EXPORTER_OTLP_ENDPOINT is
set (e.g. http://jaeger:4318); every standard OTEL_* variable (sampler, headers, resource
attributes) is honoured by the SDK.

Queue propagation: at submit time the current W3C trace context is written into the job hash
(`traceparent`). The worker starts its `job.process` span as a child of it, so one trace shows
the upload request, the time the job waited in the queue, the Gemini call and the DB write.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from contextlib import contextmanager

from opentelemetry import propagate, trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.trace.sampling import (
    Decision,
    ParentBased,
    Sampler,
    SamplingResult,
    TraceIdRatioBased,
)
from opentelemetry.trace import Span, Status, StatusCode

log = logging.getLogger("tracing")
_configured: str | None = None
_instrumented: set[str] = set()


class OrphanClientFilter(Sampler):
    """Drop CLIENT spans (Redis, SQL) that have no parent.

    Infrastructure calls only matter inside a request or a job. Without this, every idle worker
    poll (one per second per worker), every Prometheus scrape and every healthcheck would each
    become a one-span trace and bury the traces worth reading."""

    def __init__(self, inner: Sampler):
        self.inner = inner

    def should_sample(self, parent_context, trace_id, name, kind=None, attributes=None,
                      links=None, trace_state=None) -> SamplingResult:
        parent = trace.get_current_span(parent_context).get_span_context()
        if kind == trace.SpanKind.CLIENT and not parent.is_valid:
            return SamplingResult(Decision.DROP)
        return self.inner.should_sample(
            parent_context, trace_id, name, kind, attributes, links, trace_state
        )

    def get_description(self) -> str:
        return f"OrphanClientFilter{{{self.inner.get_description()}}}"


def _ratio_sampler() -> Sampler:
    """Parent-based ratio sampling; OTEL_TRACES_SAMPLER_ARG sets the ratio (default 1.0)."""
    try:
        ratio = float(os.environ.get("OTEL_TRACES_SAMPLER_ARG") or 1.0)
    except ValueError:
        ratio = 1.0
    return ParentBased(TraceIdRatioBased(min(1.0, max(0.0, ratio))))


def configure_tracing(service: str) -> None:
    """Install the global tracer provider once per process."""
    global _configured
    if _configured is not None:
        return
    resource = Resource.create({
        "service.name": os.environ.get("OTEL_SERVICE_NAME") or service,
        "service.namespace": "mediaflow",
    })
    provider = TracerProvider(resource=resource, sampler=OrphanClientFilter(_ratio_sampler()))
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()
    if endpoint:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
        log.info("exporting traces", extra={"otlp_endpoint": endpoint})
    trace.set_tracer_provider(provider)
    _configured = service
    _instrument_redis()


def shutdown_tracing() -> None:
    """Flush buffered spans (call on process exit)."""
    provider = trace.get_tracer_provider()
    if hasattr(provider, "force_flush"):
        provider.force_flush(timeout_millis=5000)


def _instrument_redis() -> None:
    if "redis" in _instrumented:
        return
    from opentelemetry.instrumentation.redis import RedisInstrumentor

    RedisInstrumentor().instrument()
    _instrumented.add("redis")


def instrument_app(app) -> None:
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

    # Health probes and scrapes would drown real traffic in the trace store.
    FastAPIInstrumentor.instrument_app(app, excluded_urls="health/live,health/ready")


def instrument_engine(engine) -> None:
    """One CLIENT span per SQL statement, via SQLAlchemy's public cursor events.

    Hand-rolled because opentelemetry-instrumentation-sqlalchemy refuses SQLAlchemy 2.1 and
    silently instruments nothing. Spans carry the SQL text but never bound parameters, so no
    user data reaches the trace store."""
    from sqlalchemy import event

    if getattr(engine, "_mediaflow_traced", False):
        return
    engine._mediaflow_traced = True
    system = engine.dialect.name

    @event.listens_for(engine, "before_cursor_execute")
    def _start(conn, cursor, statement, parameters, context, executemany):
        operation = statement.lstrip().split(None, 1)[0].upper() if statement.strip() else "SQL"
        current = tracer().start_span(
            f"{operation} {system}", kind=trace.SpanKind.CLIENT,
            attributes={"db.system": system, "db.statement": statement[:1000],
                        "db.operation": operation},
        )
        conn.info.setdefault("_mediaflow_spans", []).append(current)

    @event.listens_for(engine, "after_cursor_execute")
    def _end(conn, cursor, statement, parameters, context, executemany):
        spans = conn.info.get("_mediaflow_spans")
        if spans:
            current = spans.pop()
            if cursor.rowcount is not None and cursor.rowcount >= 0:
                current.set_attribute("db.rows_affected", cursor.rowcount)
            current.end()

    @event.listens_for(engine, "handle_error")
    def _error(exception_context):
        conn = exception_context.connection
        spans = conn.info.get("_mediaflow_spans") if conn is not None else None
        if spans:
            current = spans.pop()
            current.record_exception(exception_context.original_exception)
            mark_error(current, type(exception_context.original_exception).__name__)
            current.end()


def tracer():
    return trace.get_tracer("mediaflow")


def inject() -> dict[str, str]:
    """The current trace context as a dict to store alongside a job."""
    carrier: dict[str, str] = {}
    propagate.inject(carrier)
    return carrier


@contextmanager
def span(name: str, *, parent: dict[str, str] | None = None, **attributes) -> Iterator[Span]:
    """A span, optionally continuing a trace stored with a job. Exceptions mark it as an error."""
    ctx = propagate.extract(parent) if parent else None
    with tracer().start_as_current_span(
        name, context=ctx, attributes={k: v for k, v in attributes.items() if v is not None},
        record_exception=True, set_status_on_exception=True,
    ) as current:
        yield current


def mark_error(current: Span, message: str) -> None:
    current.set_status(Status(StatusCode.ERROR, message[:200]))
