"""Workspace analytics: volume, approval, stage timings, channels, and what to act on.

Everything is computed from durable data, counted at the time each event happened:

| Number | Source | Counted at |
|---|---|---|
| uploads | api_jobs | created_at |
| drafted / failed | api_jobs | completed_at |
| approved / rejected, first-pass | audit trail (job.approved / job.rejected) | the decision |
| edits | audit trail (draft.edited) | the save |
| comments | draft_comments | created_at |
| scheduled | audit trail (post.scheduled) | the scheduling |
| published / publish failures | publications | published_at / updated_at |

Using the audit trail for decisions counts every decision (a draft rejected, revised and then
approved counts once each way) and survives rejected drafts being deleted by retention.

Stage timings (p50, p90, max, in seconds) follow one draft through the pipeline:

    queue wait   upload -> a worker starts it              (started_at - created_at)
    analysis     worker starts -> draft ready              (completed_at - started_at)
    review wait  sent for review -> the review decision    (decision - the latest
                                                            draft.submitted before it;
                                                            completed_at for older drafts)
    publish lag  calendar time -> live on the platform     (published_at - scheduled_at)

Percentiles are computed in Python over the window's rows (nearest rank). The window is at
most 366 days, which keeps that bounded for this product's volumes; a much larger deployment
would move them into SQL (`percentile_cont`) or a rollup table.

Insights are plain rules with thresholds, each needing enough data to mean something
(see INSIGHT_RULES in docs/ANALYTICS.md). They compare with the previous period of the same
length.
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from datetime import time as dtime
from typing import Annotated
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from app.api.database import (
    ApiJob,
    AuditEvent,
    DraftComment,
    Publication,
    PublishingConnection,
    ScheduledPost,
    Workspace,
)
from app.api.dependencies import CurrentMember, DbSession
from app.api.roles import is_platform_admin, is_reviewer
from app.api.schemas import (
    AnalyticsChannel,
    AnalyticsDay,
    AnalyticsKind,
    AnalyticsRates,
    AnalyticsReport,
    AnalyticsTimings,
    AnalyticsTotals,
    Insight,
    Timing,
    WorkspaceAnalytics,
)

router = APIRouter(prefix="/api/v1")

MAX_DAYS = 366
MIN_SAMPLE = 5  # an insight needs at least this many events behind it


# ── Small helpers ─────────────────────────────────────────────────────────────────────────


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _window(start: date, end: date, tz: str) -> tuple[datetime, datetime, ZoneInfo]:
    if end < start:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "end must be on or after start")
    if (end - start).days >= MAX_DAYS:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT,
                            f"A report covers at most {MAX_DAYS} days")
    try:
        zone = ZoneInfo(tz)
    except ZoneInfoNotFoundError:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT,
                            "Invalid IANA timezone") from None
    lo = datetime.combine(start, dtime.min, tzinfo=zone).astimezone(timezone.utc)
    hi = datetime.combine(end + timedelta(days=1), dtime.min, tzinfo=zone).astimezone(timezone.utc)
    return lo, hi, zone


def timing(seconds: list[float]) -> Timing:
    """Nearest-rank percentiles; negative spans (clock skew) count as zero."""
    values = sorted(max(0.0, s) for s in seconds)
    if not values:
        return Timing(count=0)

    def rank(p: float) -> float:
        return round(values[max(0, math.ceil(p * len(values)) - 1)], 1)

    return Timing(count=len(values), p50=rank(0.5), p90=rank(0.9), max=round(values[-1], 1))


def _rate(part: int, whole: int) -> float | None:
    return round(part / whole, 4) if whole else None


def human(seconds: float | None) -> str:
    if seconds is None:
        return "n/a"
    if seconds < 90:
        return f"{seconds:.0f} s"
    if seconds < 90 * 60:
        return f"{seconds / 60:.0f} min"
    if seconds < 36 * 3600:
        return f"{seconds / 3600:.1f} h"
    return f"{seconds / 86400:.1f} days"


# ── The report ────────────────────────────────────────────────────────────────────────────


class _Period:
    """All numbers for one workspace and window."""

    def __init__(self, db: Session, workspace_id: str, lo: datetime, hi: datetime):
        self.db, self.ws, self.lo, self.hi = db, workspace_id, lo, hi
        in_window = lambda col: and_(col >= lo, col < hi)  # noqa: E731
        jobs = ApiJob.workspace_id == workspace_id

        self.uploads = db.execute(select(ApiJob.id, ApiJob.media_kind, ApiJob.created_at)
                                  .where(jobs, in_window(ApiJob.created_at))).all()
        self.finished = db.execute(
            select(ApiJob.id, ApiJob.media_kind, ApiJob.status, ApiJob.created_at,
                   ApiJob.started_at, ApiJob.completed_at)
            .where(jobs, in_window(ApiJob.completed_at))).all()
        self.started = db.execute(select(ApiJob.created_at, ApiJob.started_at)
                                  .where(jobs, in_window(ApiJob.started_at))).all()

        audit = AuditEvent.workspace_id == workspace_id
        self.decisions = db.execute(
            select(AuditEvent.target_id, AuditEvent.action, AuditEvent.created_at)
            .where(audit, AuditEvent.action.in_(("job.approved", "job.rejected")),
                   in_window(AuditEvent.created_at))).all()
        targets = {d.target_id for d in self.decisions}
        # Earlier history of the decided drafts, for first-pass approval and review waits.
        self.history = db.execute(
            select(AuditEvent.target_id, AuditEvent.action, AuditEvent.created_at)
            .where(audit, AuditEvent.target_id.in_(targets),
                   AuditEvent.action.in_(("job.rejected", "draft.edited", "draft.submitted")))
        ).all() if targets else []
        self.sent: defaultdict[str, list[datetime]] = defaultdict(list)
        for h in self.history:
            if h.action == "draft.submitted":
                self.sent[h.target_id].append(_utc(h.created_at))
        self.decided_jobs = {
            row.id: row for row in db.execute(
                select(ApiJob.id, ApiJob.media_kind, ApiJob.completed_at)
                .where(ApiJob.id.in_(targets))).all()
        } if targets else {}
        self.edits = db.scalar(select(func.count()).where(
            audit, AuditEvent.action == "draft.edited", in_window(AuditEvent.created_at)))
        self.scheduled = db.scalar(select(func.count()).where(
            audit, AuditEvent.action == "post.scheduled", in_window(AuditEvent.created_at)))
        self.comments = db.scalar(select(func.count()).select_from(DraftComment).where(
            DraftComment.workspace_id == workspace_id, in_window(DraftComment.created_at)))

        pubs = select(Publication, PublishingConnection.channel, PublishingConnection.provider,
                      ScheduledPost.scheduled_at) \
            .join(PublishingConnection, PublishingConnection.id == Publication.connection_id) \
            .join(ScheduledPost, ScheduledPost.id == Publication.scheduled_post_id) \
            .where(Publication.workspace_id == workspace_id)
        self.published = db.execute(pubs.where(Publication.status == "published",
                                               in_window(Publication.published_at))).all()
        self.publish_failed = db.execute(pubs.where(
            Publication.status.in_(("failed", "unknown")),
            in_window(Publication.updated_at))).all()
        self.requested = db.execute(pubs.where(in_window(Publication.created_at))).all()

    # Totals and rates ------------------------------------------------------------------------

    def totals(self) -> AnalyticsTotals:
        actions = Counter(d.action for d in self.decisions)
        return AnalyticsTotals(
            uploads=len(self.uploads),
            drafted=sum(1 for f in self.finished if f.status == "done"),
            failed=sum(1 for f in self.finished if f.status != "done"),
            approved=actions["job.approved"], rejected=actions["job.rejected"],
            edits=self.edits or 0, comments=self.comments or 0,
            scheduled=self.scheduled or 0, published=len(self.published),
            publish_failed=len(self.publish_failed),
        )

    def first_pass(self) -> int:
        # Approved as Gemini wrote it: no earlier rejection or edit (the uploader's own rewrite
        # before sending counts too; the metric measures the draft, not the reviewer).
        earlier = defaultdict(list)
        for h in self.history:
            if h.action != "draft.submitted":
                earlier[h.target_id].append(_utc(h.created_at))
        return sum(
            1 for d in self.decisions if d.action == "job.approved"
            and not any(t < _utc(d.created_at) for t in earlier[d.target_id])
        )

    def rates(self, t: AnalyticsTotals) -> AnalyticsRates:
        return AnalyticsRates(
            approval=_rate(t.approved, t.approved + t.rejected),
            first_pass_approval=_rate(self.first_pass(), t.approved),
            failure=_rate(t.failed, t.drafted + t.failed),
            publish_success=_rate(t.published, t.published + t.publish_failed),
        )

    # Timings ---------------------------------------------------------------------------------

    def _review_waits(self, kind: str | None = None) -> list[float]:
        waits = []
        for d in self.decisions:
            job = self.decided_jobs.get(d.target_id)
            if job and job.completed_at and (kind is None or job.media_kind == kind):
                decided = _utc(d.created_at)
                since = max((t for t in self.sent[d.target_id] if t <= decided),
                            default=_utc(job.completed_at))
                waits.append((decided - since).total_seconds())
        return waits

    def timings(self) -> AnalyticsTimings:
        return AnalyticsTimings(
            queue_wait=timing([(_utc(s.started_at) - _utc(s.created_at)).total_seconds()
                               for s in self.started]),
            analysis=timing(self._analysis()),
            review_wait=timing(self._review_waits()),
            publish_lag=timing([(_utc(p.Publication.published_at) - _utc(p.scheduled_at))
                                .total_seconds() for p in self.published]),
        )

    def _analysis(self, kind: str | None = None) -> list[float]:
        return [(_utc(f.completed_at) - _utc(f.started_at)).total_seconds()
                for f in self.finished
                if f.status == "done" and f.started_at and (kind is None or f.media_kind == kind)]

    # Breakdowns ------------------------------------------------------------------------------

    def by_kind(self) -> list[AnalyticsKind]:
        out = []
        for kind in ("image", "video"):
            decided = [d for d in self.decisions
                       if (j := self.decided_jobs.get(d.target_id)) and j.media_kind == kind]
            out.append(AnalyticsKind(
                kind=kind,
                uploads=sum(1 for u in self.uploads if u.media_kind == kind),
                drafted=sum(1 for f in self.finished
                            if f.status == "done" and f.media_kind == kind),
                approved=sum(1 for d in decided if d.action == "job.approved"),
                rejected=sum(1 for d in decided if d.action == "job.rejected"),
                analysis=timing(self._analysis(kind)),
                review_wait=timing(self._review_waits(kind)),
            ))
        return out

    def by_channel(self) -> list[AnalyticsChannel]:
        keys = {(r.channel, r.provider) for r in (*self.requested, *self.published,
                                                  *self.publish_failed)}
        out = []
        for key in sorted(keys):
            def mine(rows, key=key):
                return [r for r in rows if (r.channel, r.provider) == key]

            channel, provider = key
            published, failed = mine(self.published), mine(self.publish_failed)
            codes = Counter(r.Publication.error_code or "error" for r in failed)
            out.append(AnalyticsChannel(
                channel=channel, provider=provider, requested=len(mine(self.requested)),
                published=len(published), failed=len(failed),
                success_rate=_rate(len(published), len(published) + len(failed)),
                top_error=codes.most_common(1)[0][0] if codes else None,
                publish_lag=timing([(_utc(p.Publication.published_at) - _utc(p.scheduled_at))
                                    .total_seconds() for p in published]),
            ))
        return out

    def series(self, start: date, end: date, zone: ZoneInfo) -> list[AnalyticsDay]:
        days = {start + timedelta(days=i): Counter() for i in range((end - start).days + 1)}

        def bump(when: datetime | None, key: str):
            if when is not None:
                day = _utc(when).astimezone(zone).date()
                if day in days:
                    days[day][key] += 1

        for u in self.uploads:
            bump(u.created_at, "uploads")
        for f in self.finished:
            if f.status == "done":
                bump(f.completed_at, "drafted")
        for d in self.decisions:
            bump(d.created_at, "approved" if d.action == "job.approved" else "rejected")
        for p in self.published:
            bump(p.Publication.published_at, "published")
        return [AnalyticsDay(date=day, **{k: c[k] for k in (
            "uploads", "drafted", "approved", "rejected", "published")}) for day, c in days.items()]


# ── What to act on ────────────────────────────────────────────────────────────────────────


def insights(now: _Period, before: _Period, t: AnalyticsTotals, r: AnalyticsRates,
             tm: AnalyticsTimings, channels: list[AnalyticsChannel], backlog: tuple[int, float]
             ) -> list[Insight]:
    out: list[Insight] = []
    stages = {"waiting for a worker": tm.queue_wait, "being analyzed": tm.analysis,
              "waiting for review": tm.review_wait, "waiting to go live": tm.publish_lag}
    measured = {name: s for name, s in stages.items() if s.count >= MIN_SAMPLE and s.p50}
    if len(measured) >= 2:
        slowest = max(measured, key=lambda n: measured[n].p50)
        rest = sorted((s.p50, n) for n, s in measured.items() if n != slowest)[-1]
        if measured[slowest].p50 >= 3 * rest[0]:
            out.append(Insight(
                kind="bottleneck", severity="warn" if slowest == "waiting for review" else "info",
                text=f"Most of a draft's time is spent {slowest} (median "
                     f"{human(measured[slowest].p50)}), against {human(rest[0])} {rest[1]}.",
            ))

    bt = before.totals()
    br = before.rates(bt)
    if (r.approval is not None and br.approval is not None
            and t.approved + t.rejected >= MIN_SAMPLE and bt.approved + bt.rejected >= MIN_SAMPLE
            and abs(r.approval - br.approval) >= 0.10):
        direction = "rose" if r.approval > br.approval else "fell"
        out.append(Insight(
            kind="approval_trend", severity="info" if direction == "rose" else "warn",
            text=f"Approval rate {direction} to {r.approval:.0%} from {br.approval:.0%} in the "
                 "previous period.",
        ))
    if r.first_pass_approval is not None and t.approved >= MIN_SAMPLE \
            and r.first_pass_approval < 0.5:
        out.append(Insight(
            kind="first_pass", severity="warn",
            text=f"Only {r.first_pass_approval:.0%} of approved drafts were approved as Gemini "
                 "wrote them; the rest needed a rejection or edits first. Rejection comments "
                 "show what to change in the prompt or the brief.",
        ))
    if r.failure is not None and t.drafted + t.failed >= 2 * MIN_SAMPLE and r.failure >= 0.05:
        out.append(Insight(
            kind="failures", severity="warn",
            text=f"{r.failure:.0%} of analyses failed ({t.failed} of {t.drafted + t.failed}); "
                 "see Activity for the errors.",
        ))
    for c in channels:
        if c.success_rate is not None and c.published + c.failed >= 3 and c.success_rate < 0.9:
            out.append(Insight(
                kind="channel", severity="warn",
                text=f"Publishing to {c.channel} ({c.provider}) succeeded {c.success_rate:.0%} of "
                     f"the time; the most common error was {c.top_error}.",
            ))
    waiting, oldest = backlog
    if waiting and oldest >= 24 * 3600:
        out.append(Insight(
            kind="backlog", severity="warn",
            text=f"{waiting} draft(s) are waiting for review; the oldest for {human(oldest)}.",
        ))
    if bt.uploads >= 2 * MIN_SAMPLE and t.uploads:
        change = (t.uploads - bt.uploads) / bt.uploads
        if abs(change) >= 0.3:
            out.append(Insight(
                kind="volume", severity="info",
                text=f"Uploads {'up' if change > 0 else 'down'} {abs(change):.0%} on the previous "
                     f"period ({t.uploads} vs {bt.uploads}).",
            ))
    return out


def _backlog(db: Session, workspace_id: str, now: datetime) -> tuple[int, float]:
    """Drafts in the review queue, and how long the oldest has been there (since it was last
    sent for review; since it was drafted, for drafts older than that step)."""
    waiting = db.execute(
        select(ApiJob.completed_at, func.max(AuditEvent.created_at))
        .outerjoin(AuditEvent, (AuditEvent.target_id == ApiJob.id)
                   & (AuditEvent.action == "draft.submitted"))
        .where(ApiJob.workspace_id == workspace_id, ApiJob.status == "done",
               ApiJob.approval_status == "pending")
        .group_by(ApiJob.id, ApiJob.completed_at)).all()
    stamped = [_utc(sent or done) for done, sent in waiting if sent or done]
    oldest = max(((now - w).total_seconds() for w in stamped), default=0.0)
    return len(waiting), oldest


def build_report(db: Session, workspace_id: str, start: date, end: date, tz: str,
                 now: datetime | None = None) -> AnalyticsReport:
    lo, hi, zone = _window(start, end, tz)
    span = hi - lo
    current, previous = _Period(db, workspace_id, lo, hi), _Period(db, workspace_id, lo - span, lo)
    totals = current.totals()
    rates = current.rates(totals)
    timings = current.timings()
    channels = current.by_channel()
    prev_totals = previous.totals()
    return AnalyticsReport(
        start=start, end=end, timezone=tz, totals=totals, rates=rates, timings=timings,
        previous_totals=prev_totals, previous_rates=previous.rates(prev_totals),
        by_kind=current.by_kind(), by_channel=channels, series=current.series(start, end, zone),
        insights=insights(current, previous, totals, rates, timings, channels,
                          _backlog(db, workspace_id, now or datetime.now(timezone.utc))),
    )


def _default_range(start: date | None, end: date | None, tz: str) -> tuple[date, date]:
    try:
        today = datetime.now(ZoneInfo(tz)).date()
    except ZoneInfoNotFoundError:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT,
                            "Invalid IANA timezone") from None
    end = end or today
    return start or end - timedelta(days=29), end


@router.get("/analytics", response_model=AnalyticsReport)
def workspace_analytics(
    actor: CurrentMember,
    db: DbSession,
    start: date | None = None,
    end: date | None = None,
    timezone_name: Annotated[str, Query(alias="timezone", max_length=64)] = "UTC",
):
    """This workspace's report (managers and admins). Default: the last 30 days."""
    if not is_reviewer(actor):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Manager or admin role required")
    start, end = _default_range(start, end, timezone_name)
    return build_report(db, actor.workspace_id, start, end, timezone_name)


@router.get("/analytics/workspaces", response_model=list[WorkspaceAnalytics])
def installation_analytics(
    actor: CurrentMember,
    db: DbSession,
    start: date | None = None,
    end: date | None = None,
    timezone_name: Annotated[str, Query(alias="timezone", max_length=64)] = "UTC",
):
    """Every workspace side by side, for platform admins (the installation's operators).
    Aggregates only: no drafts, files, people or ids of anything inside a workspace."""
    if not is_platform_admin(actor.user):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Platform admin role required")
    start, end = _default_range(start, end, timezone_name)
    lo, hi, _ = _window(start, end, timezone_name)
    rows = []
    for ws in db.scalars(select(Workspace).order_by(Workspace.name)).all():
        period = _Period(db, ws.id, lo, hi)
        totals, timings = period.totals(), period.timings()
        rows.append(WorkspaceAnalytics(
            workspace_id=ws.id, name=ws.name, totals=totals, rates=period.rates(totals),
            analysis_p50=timings.analysis.p50, review_wait_p50=timings.review_wait.p50,
        ))
    return rows
