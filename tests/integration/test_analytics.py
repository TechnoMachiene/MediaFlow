"""Workspace analytics. See docs/ANALYTICS.md."""

from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from app.api.analytics import human, timing
from app.api.database import (
    ApiJob,
    AuditEvent,
    Publication,
    PublishingConnection,
    ScheduledPost,
)
from app.api.persistence import persist_api_result
from app.errors import Permanent
from tests.api_harness import add_member, build_app, login, png, upload
from tests.helpers import ScriptedClient

pytestmark = pytest.mark.integration

T0 = datetime(2026, 3, 10, 9, 0, tzinfo=timezone.utc)  # a Tuesday, well in the past


@pytest.fixture
def team(make_rt, tmp_path, monkeypatch):
    monkeypatch.setattr("app.api.routes.JOB_RESULT_WAIT_S", 0)
    gemini = ScriptedClient()
    runtime = make_rt(client=gemini)
    application, engine, sessions = build_app(runtime, tmp_path / "media")
    add_member(sessions, "anna@a.example.com", "admin", "ws-a")
    add_member(sessions, "max@a.example.com", "manager", "ws-a")
    add_member(sessions, "alice@a.example.com", "user", "ws-a")
    add_member(sessions, "bea@b.example.com", "admin", "ws-b")
    add_member(sessions, "pat@b.example.com", "user", "ws-b", platform_role="admin")
    with TestClient(application) as client:
        h = {n: login(client, f"{n}@{'b' if n in ('bea', 'pat') else 'a'}.example.com")
             for n in ("anna", "max", "alice", "bea", "pat")}
        yield client, runtime, sessions, h, gemini
    engine.dispose()


def drafted(client, runtime, headers, n) -> str:
    job_id = upload(client, headers, png((n, n, n))).json()["job_id"]
    runtime.worker.drain()
    return job_id


def at(sessions, job_id, *, created, started=None, completed=None):
    with sessions.begin() as db:
        db.execute(update(ApiJob).where(ApiJob.id == job_id).values(
            created_at=created, started_at=started, completed_at=completed))


def decide(client, h, job_id, status, when, sessions):
    client.patch(f"/api/v1/jobs/{job_id}/approval", headers=h["max"], json={"status": status})
    with sessions.begin() as db:
        event = db.scalars(select(AuditEvent).where(
            AuditEvent.target_id == job_id, AuditEvent.action == f"job.{status}")
            .order_by(AuditEvent.id.desc())).first()
        event.created_at = when


def reopen(sessions, job_id):
    with sessions.begin() as db:
        db.get(ApiJob, job_id).approval_status = "pending"


def report(client, headers, start=date(2026, 3, 1), end=date(2026, 3, 31), tz="UTC"):
    response = client.get(f"/api/v1/analytics?start={start}&end={end}&timezone={tz}",
                          headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


# ── Durable stage timestamps ──────────────────────────────────────────────────────────────

def test_jobs_record_when_they_started_and_finished(team):
    client, runtime, sessions, h, gemini = team
    ok = drafted(client, runtime, h["alice"], 1)
    gemini.plan = [Permanent("blocked content")]
    bad = drafted(client, runtime, h["alice"], 2)
    cached = upload(client, h["alice"], png((1, 1, 1))).json()["job_id"]  # same bytes: a cache hit
    with sessions() as db:
        good, failed, hit = (db.get(ApiJob, j) for j in (ok, bad, cached))
    assert good.created_at <= good.started_at <= good.completed_at
    assert failed.status == "failed" and failed.started_at and failed.completed_at
    assert hit.status == "done" and hit.completed_at and hit.started_at is None

    first = good.completed_at
    persist_api_result(sessions, ok, good.result)  # a replayed result
    with sessions() as db:
        assert db.get(ApiJob, ok).completed_at == first  # stamped once


# ── The report ────────────────────────────────────────────────────────────────────────────

def test_counts_rates_and_first_pass_approval(team):
    client, runtime, sessions, h, _ = team
    a, b, c, d = (drafted(client, runtime, h["alice"], n) for n in (1, 2, 3, 4))
    for job_id in (a, b, c, d):
        at(sessions, job_id, created=T0, started=T0, completed=T0 + timedelta(minutes=1))
    decide(client, h, a, "approved", T0 + timedelta(hours=1), sessions)
    decide(client, h, b, "rejected", T0 + timedelta(hours=1), sessions)
    reopen(sessions, b)  # revised and resubmitted...
    decide(client, h, b, "approved", T0 + timedelta(hours=2), sessions)  # ...then approved
    decide(client, h, c, "rejected", T0 + timedelta(hours=1), sessions)

    r = report(client, h["max"])
    t = r["totals"]
    assert (t["uploads"], t["drafted"], t["approved"], t["rejected"]) == (4, 4, 2, 2)
    assert r["rates"]["approval"] == 0.5
    assert r["rates"]["first_pass_approval"] == 0.5  # only `a` was approved as written
    assert r["rates"]["failure"] == 0.0 and r["rates"]["publish_success"] is None


def test_stage_timings_follow_each_draft(team):
    client, runtime, sessions, h, _ = team
    for n in range(1, 6):
        job_id = drafted(client, runtime, h["alice"], n)
        start = T0 + timedelta(days=n)
        at(sessions, job_id, created=start, started=start + timedelta(seconds=10 * n),
           completed=start + timedelta(seconds=10 * n + 60))
        decide(client, h, job_id, "approved",
               start + timedelta(seconds=10 * n + 60 + 3600 * n), sessions)

    timings = report(client, h["max"])["timings"]
    assert timings["queue_wait"] == {"count": 5, "p50": 30.0, "p90": 50.0, "max": 50.0}
    assert timings["analysis"] == {"count": 5, "p50": 60.0, "p90": 60.0, "max": 60.0}
    assert timings["review_wait"]["p50"] == 3 * 3600
    assert timings["publish_lag"] == {"count": 0, "p50": None, "p90": None, "max": None}


def test_days_are_the_viewers_local_days(team):
    client, runtime, sessions, h, _ = team
    job_id = drafted(client, runtime, h["alice"], 1)
    late = datetime(2026, 3, 14, 23, 30, tzinfo=timezone.utc)  # 00:30 on the 15th in Berlin
    at(sessions, job_id, created=late, started=late, completed=late)
    utc = {d["date"]: d["uploads"] for d in report(client, h["max"])["series"]}
    berlin = {d["date"]: d["uploads"]
              for d in report(client, h["max"], tz="Europe/Berlin")["series"]}
    assert utc["2026-03-14"] == 1 and berlin["2026-03-15"] == 1
    assert len(utc) == 31 and sum(utc.values()) == 1


def test_the_previous_period_is_reported_alongside(team):
    client, runtime, sessions, h, _ = team
    for n in range(1, 4):
        job_id = drafted(client, runtime, h["alice"], n)
        at(sessions, job_id, created=T0 - timedelta(days=31))  # February: the period before
    job_id = drafted(client, runtime, h["alice"], 9)
    at(sessions, job_id, created=T0)
    r = report(client, h["max"])
    assert (r["totals"]["uploads"], r["previous_totals"]["uploads"]) == (1, 3)


def test_publishing_is_broken_down_by_channel(team):
    client, runtime, sessions, h, _ = team
    jobs = [drafted(client, runtime, h["alice"], n) for n in range(1, 5)]
    with sessions.begin() as db:
        conns = {}
        for channel in ("instagram", "facebook"):
            conn = PublishingConnection(
                workspace_id="ws-a", provider="mock", channel=channel, account_id=f"sbx_{channel}",
                account_name=channel, status="active", options={},
                connected_by_email="anna@a.example.com")
            db.add(conn)
            db.flush()
            conns[channel] = conn.id
        outcomes = [("instagram", "published", 120), ("instagram", "published", 30),
                    ("instagram", "failed", None), ("facebook", "published", 5)]
        for job_id, (channel, state, lag) in zip(jobs, outcomes, strict=True):
            job = db.get(ApiJob, job_id)
            post = ScheduledPost(job_id=job_id, workspace_id="ws-a", owner_id=job.owner_id,
                                 scheduled_at=T0, timezone="UTC")
            db.add(post)
            db.flush()
            db.add(Publication(
                workspace_id="ws-a", scheduled_post_id=post.id, connection_id=conns[channel],
                status=state, requested_by_email="alice@a.example.com", created_at=T0,
                updated_at=T0, error_code="rate_limited" if state == "failed" else None,
                published_at=T0 + timedelta(seconds=lag) if lag is not None else None))

    channels = {c["channel"]: c for c in report(client, h["max"])["by_channel"]}
    ig = channels["instagram"]
    assert (ig["published"], ig["failed"], ig["top_error"]) == (2, 1, "rate_limited")
    assert ig["success_rate"] == pytest.approx(2 / 3, abs=1e-4)
    assert ig["publish_lag"]["p50"] == 30.0
    assert channels["facebook"]["success_rate"] == 1.0
    r = report(client, h["max"])
    assert r["rates"]["publish_success"] == 0.75
    assert any(i["kind"] == "channel" and "instagram" in i["text"] for i in r["insights"])


def test_media_kinds_are_compared(team):
    client, runtime, sessions, h, _ = team
    job_id = drafted(client, runtime, h["alice"], 1)
    at(sessions, job_id, created=T0, started=T0, completed=T0 + timedelta(seconds=40))
    kinds = {k["kind"]: k for k in report(client, h["max"])["by_kind"]}
    assert kinds["image"]["uploads"] == 1 and kinds["image"]["analysis"]["p50"] == 40.0
    assert kinds["video"]["uploads"] == 0 and kinds["video"]["analysis"]["count"] == 0


# ── Insights ──────────────────────────────────────────────────────────────────────────────

def kinds(r):
    return {i["kind"]: i for i in r["insights"]}


def test_review_as_the_bottleneck_and_a_stale_backlog_are_called_out(team):
    client, runtime, sessions, h, _ = team
    for n in range(1, 6):
        job_id = drafted(client, runtime, h["alice"], n)
        start = T0 + timedelta(days=n)
        at(sessions, job_id, created=start, started=start + timedelta(seconds=5),
           completed=start + timedelta(seconds=45))
        decide(client, h, job_id, "approved", start + timedelta(hours=20), sessions)
    waiting = drafted(client, runtime, h["alice"], 9)
    at(sessions, waiting, created=T0, completed=datetime.now(timezone.utc) - timedelta(days=2))

    found = kinds(report(client, h["max"]))
    assert found["bottleneck"]["severity"] == "warn"
    assert "waiting for review" in found["bottleneck"]["text"]
    assert "1 draft(s) are waiting for review" in found["backlog"]["text"]


def test_insights_need_enough_data(team):
    client, runtime, sessions, h, _ = team
    job_id = drafted(client, runtime, h["alice"], 1)
    at(sessions, job_id, created=T0, started=T0, completed=T0 + timedelta(seconds=30))
    decide(client, h, job_id, "rejected", T0 + timedelta(days=3), sessions)
    assert report(client, h["max"])["insights"] == []  # one rejection proves nothing


def test_low_first_pass_approval_and_falling_approval_are_flagged(team):
    client, runtime, sessions, h, _ = team
    for n in range(1, 7):  # February: all approved as written
        job_id = drafted(client, runtime, h["alice"], n)
        at(sessions, job_id, created=T0 - timedelta(days=30), completed=T0 - timedelta(days=30))
        decide(client, h, job_id, "approved", T0 - timedelta(days=29), sessions)
    for n in range(10, 16):  # March: each rejected once, then approved; plus two plain rejects
        job_id = drafted(client, runtime, h["alice"], n)
        at(sessions, job_id, created=T0, completed=T0)
        decide(client, h, job_id, "rejected", T0 + timedelta(hours=1), sessions)
        reopen(sessions, job_id)
        decide(client, h, job_id, "approved", T0 + timedelta(hours=2), sessions)
    found = kinds(report(client, h["max"]))
    assert "0% of approved drafts" in found["first_pass"]["text"]
    assert found["approval_trend"]["severity"] == "warn"
    assert "fell to 50% from 100%" in found["approval_trend"]["text"]


# ── Access and validation ─────────────────────────────────────────────────────────────────

def test_only_managers_and_admins_see_their_own_workspace(team):
    client, runtime, sessions, h, _ = team
    job_id = drafted(client, runtime, h["alice"], 1)
    at(sessions, job_id, created=T0)
    assert client.get("/api/v1/analytics", headers=h["alice"]).status_code == 403
    assert report(client, h["anna"])["totals"]["uploads"] == 1
    assert report(client, h["bea"])["totals"]["uploads"] == 0  # B sees only B
    spoofed = {**h["bea"], "X-Workspace-Id": "ws-a"}
    assert client.get("/api/v1/analytics", headers=spoofed).status_code == 403


def test_platform_admins_compare_workspaces_by_aggregates_only(team):
    client, runtime, sessions, h, _ = team
    job_id = drafted(client, runtime, h["alice"], 1)
    at(sessions, job_id, created=T0)
    assert client.get("/api/v1/analytics/workspaces", headers=h["anna"]).status_code == 403
    response = client.get("/api/v1/analytics/workspaces?start=2026-03-01&end=2026-03-31",
                          headers=h["pat"])
    rows = {r["workspace_id"]: r for r in response.json()}
    assert rows["ws-a"]["totals"]["uploads"] == 1 and rows["ws-b"]["totals"]["uploads"] == 0
    assert "alice@" not in response.text and job_id not in response.text


@pytest.mark.parametrize("query, reason", [
    ("start=2026-03-10&end=2026-03-01", "on or after"),
    ("start=2025-01-01&end=2026-03-01", "at most 366"),
    ("timezone=Mars/Olympus", "timezone"),
])
def test_bad_ranges_are_refused(team, query, reason):
    client, runtime, sessions, h, _ = team
    response = client.get(f"/api/v1/analytics?{query}", headers=h["max"])
    assert response.status_code == 422 and reason in response.json()["detail"]


def test_the_default_is_the_last_30_days(team):
    client, runtime, sessions, h, _ = team
    r = client.get("/api/v1/analytics", headers=h["max"]).json()
    assert (date.fromisoformat(r["end"]) - date.fromisoformat(r["start"])).days == 29
    assert len(r["series"]) == 30


# ── Helpers ───────────────────────────────────────────────────────────────────────────────

def test_percentiles_use_nearest_rank():
    assert timing([]).model_dump() == {"count": 0, "p50": None, "p90": None, "max": None}
    t = timing([5, 1, 4, 2, 3, 10, 9, 8, 7, 6])
    assert (t.p50, t.p90, t.max) == (5, 9, 10)
    assert timing([-3, 2]).p50 == 0  # clock skew never shows as negative time


def test_human_durations():
    assert [human(s) for s in (None, 42, 600, 7200, 3 * 86400)] == [
        "n/a", "42 s", "10 min", "2.0 h", "3.0 days"]
