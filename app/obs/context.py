"""Correlation context carried through logs, the queue and traces.

`request_id` is born at the API edge (or taken from the caller's X-Request-ID), stored in the
Redis job hash at submit time, and restored by the worker, so one id ties together the upload
request, every log line the worker writes for that job, and the reconciler's repairs.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

_FIELDS = ("request_id", "job_id", "workspace_id", "user_id")
_vars: dict[str, ContextVar[str | None]] = {
    name: ContextVar(name, default=None) for name in _FIELDS
}
# Accept the caller's id only if it is short and boring, so it is safe to log and echo.
_SAFE_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


def new_request_id() -> str:
    return uuid.uuid4().hex


def clean_request_id(value: str | None) -> str:
    return value if value and _SAFE_ID.match(value) else new_request_id()


def get(name: str) -> str | None:
    return _vars[name].get()


def current() -> dict[str, str]:
    """All correlation fields that are set right now."""
    return {name: value for name, var in _vars.items() if (value := var.get())}


@contextmanager
def bind(**fields: str | None) -> Iterator[None]:
    """Set correlation fields for the duration of a block (None leaves a field unchanged)."""
    tokens = [(_vars[k], _vars[k].set(v)) for k, v in fields.items() if v is not None]
    try:
        yield
    finally:
        for var, token in reversed(tokens):
            var.reset(token)
