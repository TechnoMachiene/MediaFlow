"""The API edge: request ids, access logs and HTTP metrics in one pure-ASGI middleware.

- Takes X-Request-ID from the caller if it is safe, otherwise makes one, and returns it on
  every response so a user's bug report can be matched to logs and traces.
- One access-log line per request: method, route template, status, duration, user and
  workspace (ids only - never emails or bodies).
- Prometheus labels use the route *template* (/api/v1/jobs/{job_id}), so cardinality stays
  bounded no matter how many jobs exist.
"""

from __future__ import annotations

import logging
import time

from opentelemetry import trace

from app.obs import context
from app.obs.metrics import HTTP_DURATION, HTTP_REQUESTS

log = logging.getLogger("http.access")
QUIET_PREFIXES = ("/health/",)  # counted in metrics, not logged


def _route(scope) -> str:
    route = scope.get("route")
    if route is not None and getattr(route, "path", None) is not None:
        return route.path
    path = scope.get("path", "")
    return "unmatched" if path.startswith("/api/") else "static"


class ObservabilityMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        raw = dict(scope.get("headers") or ()).get(b"x-request-id", b"")
        request_id = context.clean_request_id(raw.decode("latin-1"))
        status = 500
        started = time.perf_counter()

        async def send_with_id(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                headers = list(message.get("headers") or ())
                headers.append((b"x-request-id", request_id.encode()))
                message = {**message, "headers": headers}
            await send(message)

        with context.bind(request_id=request_id):
            trace.get_current_span().set_attribute("mediaflow.request_id", request_id)
            try:
                await self.app(scope, receive, send_with_id)
            finally:
                elapsed = time.perf_counter() - started
                route, method = _route(scope), scope.get("method", "")
                HTTP_REQUESTS.labels(method, route, str(status)).inc()
                HTTP_DURATION.labels(method, route).observe(elapsed)
                path = scope.get("path", "")
                if not path.startswith(QUIET_PREFIXES):
                    state = scope.get("state") or {}
                    log.log(
                        logging.ERROR if status >= 500 else logging.INFO,
                        "%s %s %s", method, route, status,
                        extra={
                            "http_method": method,
                            "route": route,
                            "status": status,
                            "duration_ms": round(elapsed * 1000, 1),
                            "user_id": state.get("user_id"),
                            "workspace_id": state.get("workspace_id"),
                        },
                    )
