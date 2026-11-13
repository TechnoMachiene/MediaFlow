"""Structured logging: one JSON object per line, with correlation and trace ids on every line.

    LOG_FORMAT=json (default in containers) | text (default for a terminal)
    LOG_LEVEL=INFO

Fields: ts, level, logger, msg, service, request_id/job_id/workspace_id/user_id (when set),
trace_id/span_id (when a span is active), any `extra={...}` passed to the log call, and `exc`
with the traceback for errors. Uvicorn's own access log is replaced by the API's access log
(see app/obs/http.py), which knows the route template, user and workspace.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone

from app.obs import context

# Attributes every LogRecord has; anything else on a record came from `extra=`.
_STANDARD = set(vars(logging.makeLogRecord({}))) | {"message", "asctime", "taskName"}
_MARK = "_mediaflow_handler"


def _trace_ids() -> dict[str, str]:
    try:
        from opentelemetry import trace
    except ImportError:  # pragma: no cover - dependency is in requirements.txt
        return {}
    ctx = trace.get_current_span().get_span_context()
    if not ctx.is_valid:
        return {}
    return {"trace_id": format(ctx.trace_id, "032x"), "span_id": format(ctx.span_id, "016x")}


class _Enrich(logging.Filter):
    def __init__(self, service: str):
        super().__init__()
        self.service = service

    def filter(self, record: logging.LogRecord) -> bool:
        record.service = self.service
        for key, value in {**context.current(), **_trace_ids()}.items():
            if not hasattr(record, key):
                setattr(record, key, value)
        return True


class _StderrHandler(logging.StreamHandler):
    """Writes to whatever sys.stderr is *now* (survives stream swaps by test runners, etc.)."""

    @property
    def stream(self):
        return sys.stderr

    @stream.setter
    def stream(self, _value):
        pass


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(
                timespec="milliseconds"
            ),
            "level": record.levelname.lower(),
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _STANDARD and not key.startswith("_") and key not in out:
                out[key] = value
        if record.exc_info:
            out["exc"] = self.formatException(record.exc_info)
        return json.dumps(out, default=str, ensure_ascii=False)


class TextFormatter(logging.Formatter):
    """Human-readable, but still shows the ids you need to correlate."""

    def format(self, record: logging.LogRecord) -> str:
        ids = " ".join(
            f"{k}={getattr(record, k)}"
            for k in ("request_id", "job_id", "trace_id")
            if getattr(record, k, None)
        )
        base = f"{self.formatTime(record, '%H:%M:%S')} {record.levelname:<7} {record.name}: "
        line = base + record.getMessage() + (f"  [{ids}]" if ids else "")
        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)
        return line


def configure_logging(service: str, *, fmt: str | None = None, level: str | None = None) -> None:
    """Install one structured handler on the root logger. Safe to call more than once, and it
    leaves other handlers (such as pytest's) alone."""
    fmt = (fmt or os.environ.get("LOG_FORMAT") or ("text" if sys.stderr.isatty() else "json"))
    level = (level or os.environ.get("LOG_LEVEL") or "INFO").upper()
    root = logging.getLogger()
    for handler in [h for h in root.handlers if getattr(h, _MARK, False)]:
        root.removeHandler(handler)
    handler = _StderrHandler()
    setattr(handler, _MARK, True)
    handler.addFilter(_Enrich(service))
    handler.setFormatter(JsonFormatter() if fmt.lower() == "json" else TextFormatter())
    root.addHandler(handler)
    root.setLevel(level)
    # Route uvicorn through the same handler; its access log is superseded by ours.
    for name in ("uvicorn", "uvicorn.error"):
        logger = logging.getLogger(name)
        logger.handlers, logger.propagate = [], True
    access = logging.getLogger("uvicorn.access")
    access.handlers, access.propagate, access.disabled = [], False, True
    for noisy in ("httpx", "httpcore", "botocore", "urllib3", "google_genai"):
        logging.getLogger(noisy).setLevel(max(logging.WARNING, root.level))
