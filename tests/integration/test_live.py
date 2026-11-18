"""Live updates: committed changes announce their topics; the stream relays them."""

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from app.api import live
from app.api.database import Workspace
from tests.api_harness import add_member, build_app, login, png, send_for_review, upload
from tests.helpers import ScriptedClient

pytestmark = pytest.mark.integration


class FakeRedis:
    def __init__(self):
        self.published: list[tuple[str, dict]] = []

    def publish(self, channel, message):
        self.published.append((channel, json.loads(message)))

    def topics(self, channel):
        return set().union(*(m["topics"] for c, m in self.published if c == channel) or [set()])


@pytest.fixture
def team(make_rt, tmp_path, monkeypatch):
    monkeypatch.setattr("app.api.routes.JOB_RESULT_WAIT_S", 0)
    runtime = make_rt(client=ScriptedClient())
    application, engine, sessions = build_app(runtime, tmp_path / "media")
    ids = {n: add_member(sessions, f"{n}@a.example.com", role, "ws-a")
           for n, role in (("alice", "user"), ("max", "manager"))}
    with TestClient(application) as client:
        fake = FakeRedis()
        bus = live.configure(fake, "t")  # after startup, which configured the real Redis
        h = {n: login(client, f"{n}@a.example.com") for n in ids}
        yield client, sessions, runtime, h, ids, fake, bus
    engine.dispose()


def test_saved_changes_announce_their_topics_to_the_workspace(team):
    client, sessions, runtime, h, ids, fake, bus = team
    ws = bus.workspace_channel("ws-a")
    job_id = upload(client, h["alice"], png()).json()["job_id"]
    assert "jobs" in fake.topics(ws)

    fake.published.clear()
    runtime.worker.drain()  # the worker's direct SQL updates are announced too
    assert fake.topics(ws) == {"jobs"}

    fake.published.clear()
    send_for_review(client, h["alice"], job_id)
    assert {"jobs", "audit"} <= fake.topics(ws)
    # The reviewer's own channel hears about their new notification.
    assert fake.topics(bus.user_channel(ids["max"])) == {"notifications"}
    assert fake.topics(bus.user_channel(ids["alice"])) == set()
    # Only names of areas travel, never content or ids.
    assert all(set(m) == {"topics"} for _, m in fake.published)


def test_a_rolled_back_change_says_nothing(team):
    client, sessions, runtime, h, ids, fake, bus = team
    fake.published.clear()
    with sessions() as db:
        db.get(Workspace, "ws-a").name = "Renamed"
        db.flush()
        db.rollback()
    assert fake.published == []
    with sessions.begin() as db:
        db.get(Workspace, "ws-a").name = "Renamed"
    assert fake.topics(bus.workspace_channel("ws-a")) == {"workspace"}


def test_a_broken_redis_never_breaks_a_save(team):
    client, sessions, runtime, h, ids, fake, bus = team

    def boom(*_):
        raise ConnectionError("redis down")

    fake.publish = boom
    assert upload(client, h["alice"], png((5, 5, 5))).status_code == 202


def test_the_stream_needs_a_valid_session(team):
    client, sessions, runtime, h, ids, fake, bus = team
    assert client.get("/api/v1/events").status_code == 401
    assert client.get("/api/v1/events", headers={"Authorization": "Bearer nope"}).status_code == 401
    other = {**h["alice"], "X-Workspace-Id": "ws-b"}
    assert client.get("/api/v1/events", headers=other).status_code == 403


# ── The stream itself ────────────────────────────────────────────────────────────────────

class FakePubSub:
    def __init__(self, messages):
        self.messages, self.closed = list(messages), False

    async def get_message(self, ignore_subscribe_messages, timeout):
        return self.messages.pop(0) if self.messages else None

    async def aclose(self):
        self.closed = True


def collect(pubsub, *, steps, disconnect_after=None, max_s=600.0):
    clock = iter(range(10_000))
    calls = {"n": 0}

    async def disconnected():
        calls["n"] += 1
        return disconnect_after is not None and calls["n"] > disconnect_after

    async def run():
        out = []
        async for chunk in live.stream(pubsub, disconnected, heartbeat_s=0, max_s=max_s,
                                       clock=lambda: next(clock)):
            out.append(chunk)
            if len(out) >= steps:
                break
        return out

    return asyncio.run(run())


def test_the_stream_relays_changes_and_sends_heartbeats():
    message = {"type": "message", "data": b'{"topics": ["jobs"]}'}
    out = collect(FakePubSub([message]), steps=3)
    assert out == ["retry: 3000\n\n", 'event: change\ndata: {"topics": ["jobs"]}\n\n', ": ping\n\n"]


def test_the_stream_ends_when_the_browser_leaves_or_time_is_up():
    pubsub = FakePubSub([])
    assert collect(pubsub, steps=99, disconnect_after=2) == ["retry: 3000\n\n", ": ping\n\n",
                                                            ": ping\n\n"]
    assert pubsub.closed
    assert len(collect(FakePubSub([]), steps=99, max_s=3)) == 3  # retry + 2 pings, then reconnect
