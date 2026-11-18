"""Live updates: open browsers hear that something in their workspace changed.

Every committed change to a tracked table publishes a small message on Redis pub/sub naming
the *topics* it touched (no content, no ids), on a channel per workspace, or per person for
notifications. `GET /api/v1/events` streams those messages to the browser as Server-Sent
Events, and each screen reloads what it shows when one of its topics arrives.

    jobs           uploads, analysis status, drafts, versions, comments, approvals
    calendar       calendar entries and their deliveries
    publishing     connected accounts and deliveries
    team           members and invitations
    audit          new audit events
    workspace      the workspace itself (name, retention)
    notifications  the person's own notifications (personal channel)

Messages are published only after the transaction commits (a rolled-back change says
nothing), and publishing is best effort: if Redis is briefly unreachable the change is still
saved, and the browser catches up when it reconnects or regains focus. The worker publishes
too (job status and results, deliveries, notifications), so the browser hears about Gemini
finishing without polling.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy import event, select
from sqlalchemy.orm import Session

from app.api.database import (
    ApiJob,
    AuditEvent,
    DraftComment,
    DraftVersion,
    Notification,
    Publication,
    PublishingConnection,
    ScheduledPost,
    Workspace,
    WorkspaceInvitation,
    WorkspaceMember,
)

log = logging.getLogger("live")

TOPICS: dict[type, tuple[str, ...]] = {
    ApiJob: ("jobs",),
    DraftVersion: ("jobs",),
    DraftComment: ("jobs",),
    ScheduledPost: ("calendar", "jobs"),
    Publication: ("calendar", "publishing"),
    PublishingConnection: ("publishing",),
    WorkspaceMember: ("team",),
    WorkspaceInvitation: ("team",),
    AuditEvent: ("audit",),
    Workspace: ("workspace",),
}
HEARTBEAT_S = 20.0
# Streams end after this long; the browser reconnects at once, re-authenticating, so a revoked
# session or a removed member stops hearing within this time.
MAX_STREAM_S = 600.0


class Bus:
    def __init__(self, redis_client, prefix: str):
        self.r, self.prefix = redis_client, prefix

    def workspace_channel(self, workspace_id: str) -> str:
        return f"{self.prefix}:live:ws:{workspace_id}"

    def user_channel(self, user_id: str) -> str:
        return f"{self.prefix}:live:user:{user_id}"

    def publish(self, channel: str, topics) -> None:
        try:
            self.r.publish(channel, json.dumps({"topics": sorted(set(topics))}))
        except Exception as err:  # never let a notice undo or fail a saved change
            log.debug("live publish failed: %s", err)


_bus: Bus | None = None


def configure(redis_client, prefix: str) -> Bus:
    """Set the process's bus (API and worker each call this once at startup)."""
    global _bus
    _bus = Bus(redis_client, prefix)
    _install()
    return _bus


def bus() -> Bus | None:
    return _bus


def announce(workspace_id: str | None, *topics: str) -> None:
    if _bus is not None and workspace_id:
        _bus.publish(_bus.workspace_channel(workspace_id), topics)


def announce_job(session_factory, job_id: str) -> None:
    """For the worker's direct SQL updates, which the ORM hooks below don't see."""
    if _bus is None:
        return
    try:
        with session_factory() as db:
            workspace_id = db.scalar(select(ApiJob.workspace_id).where(ApiJob.id == job_id))
    except Exception:
        return
    announce(workspace_id, "jobs")


# ── ORM hooks: collect what a transaction touched, publish once it commits ─────────────────

_installed = False


def _collect(session: Session, _flush_context) -> None:
    pending = session.info.setdefault("live", {})
    for obj in (*session.new, *session.dirty, *session.deleted):
        if isinstance(obj, Notification):
            if obj.user_id:
                pending.setdefault(("user", obj.user_id), set()).add("notifications")
            continue
        topics = TOPICS.get(type(obj))
        if not topics:
            continue
        workspace_id = obj.id if isinstance(obj, Workspace) else getattr(obj, "workspace_id", None)
        if workspace_id:
            pending.setdefault(("ws", workspace_id), set()).update(topics)


def _publish(session: Session) -> None:
    pending = session.info.pop("live", None)
    if not pending or _bus is None:
        return
    for (kind, key), topics in pending.items():
        channel = _bus.workspace_channel(key) if kind == "ws" else _bus.user_channel(key)
        _bus.publish(channel, topics)


def _forget(session: Session, *_args) -> None:
    session.info.pop("live", None)


def _install() -> None:
    global _installed
    if _installed:
        return
    event.listen(Session, "after_flush", _collect)
    event.listen(Session, "after_commit", _publish)
    event.listen(Session, "after_soft_rollback", _forget)
    _installed = True


# ── The stream ─────────────────────────────────────────────────────────────────────────────


async def stream(pubsub, is_disconnected: Callable[[], Awaitable[bool]], *,
                 heartbeat_s: float = HEARTBEAT_S, max_s: float = MAX_STREAM_S,
                 clock: Callable[[], float] = time.monotonic) -> AsyncIterator[str]:
    """Server-Sent Events from a subscribed pub/sub: one `data:` line per change, a comment
    line as heartbeat (keeps proxies from closing an idle connection)."""
    started = clock()
    yield "retry: 3000\n\n"  # how soon the browser should reconnect after a drop
    try:
        while clock() - started < max_s:
            if await is_disconnected():
                return
            message = await pubsub.get_message(ignore_subscribe_messages=True,
                                               timeout=heartbeat_s)
            if message is None:
                yield ": ping\n\n"
                continue
            data = message.get("data")
            if isinstance(data, bytes):
                data = data.decode()
            yield f"event: change\ndata: {data}\n\n"
    finally:
        try:
            await pubsub.aclose()
        except Exception:
            pass


router = APIRouter(prefix="/api/v1")


def _authenticate(request: Request):
    """The same checks as every other route, on a short-lived session (a stream can stay open
    for minutes, and must not hold a database connection all that time)."""
    from app.api.dependencies import current_member, current_user

    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not authenticated",
                            headers={"WWW-Authenticate": "Bearer"})
    with request.app.state.session_factory() as db:
        user = current_user(request, HTTPAuthorizationCredentials(scheme="Bearer",
                                                                  credentials=token), db)
        member = current_member(request, user, db,
                                workspace_id=request.headers.get("x-workspace-id") or None)
        return member.workspace_id, member.id


@router.get("/events", include_in_schema=False)
async def events(request: Request):
    """Server-Sent Events: `change` messages naming the topics that changed."""
    workspace_id, user_id = await run_in_threadpool(_authenticate, request)
    the_bus = bus()
    client = getattr(request.app.state, "live_redis", None)
    if the_bus is None or client is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Live updates are off")
    pubsub = client.pubsub()
    await pubsub.subscribe(the_bus.workspace_channel(workspace_id),
                           the_bus.user_channel(user_id))
    return StreamingResponse(
        stream(pubsub, request.is_disconnected),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
    )


def async_client(redis_url: str):
    import redis.asyncio as aioredis

    return aioredis.Redis.from_url(redis_url)

