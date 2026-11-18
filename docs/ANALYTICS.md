# Analytics

**Insights** (managers and admins) answers four product questions per workspace: how much
content is moving, how much of it gets approved, where the time goes between upload and
going live, and which channels deliver. It ends each report with what to act on.

`GET /api/v1/analytics?start=&end=&timezone=` returns the report (default: the last 30 days in
UTC, at most 366 days). Platform admins also get `GET /api/v1/analytics/workspaces`, every
workspace side by side, as aggregates only.

## How this differs from the operations metrics

| | Prometheus metrics ([OPERATIONS.md](OPERATIONS.md)) | Analytics (this page) |
|---|---|---|
| For | Operators keeping the service healthy | Teams improving their content workflow |
| Scope | Whole installation; no tenant labels by design | One workspace (or per-workspace aggregates for platform admins) |
| Source | Counters and histograms in process memory | Durable rows: jobs, audit trail, publications |
| Horizon | Live, with retention set by Prometheus | Any range up to a year, any timezone |

## What is counted, and when

Every number is counted at the moment its event happened, in the viewer's timezone:

| Number | Source | Counted at |
|---|---|---|
| Uploads | `api_jobs` | `created_at` |
| Drafted, failed | `api_jobs` | `completed_at` |
| Approved, rejected | audit trail: `job.approved`, `job.rejected` | the decision |
| Approved as written (first-pass) | an approval with no earlier `job.rejected` or `draft.edited` for that job | the decision |
| Edits | audit trail: `draft.edited` | the save |
| Comments | `draft_comments` | `created_at` |
| Scheduled | audit trail: `post.scheduled` | the scheduling |
| Published, publish failures | `publications` | `published_at`; `updated_at` for failures |

Decisions come from the audit trail rather than the job's current state. A draft rejected,
revised and then approved counts as one rejection and one approval, and the counts survive
rejected drafts being deleted by retention ([STORAGE.md](STORAGE.md)). Upload counts do not
include jobs that retention later deleted entirely.

## Rates

| Rate | Definition |
|---|---|
| Approval | approved / (approved + rejected) decisions in the period |
| Approved as written | first-pass approvals / approvals: how often Gemini's draft is good enough unchanged |
| Failure | failed / (drafted + failed) analyses |
| Publish success | published / (published + failed) deliveries |

A rate is `null` when there is nothing to divide by, never a misleading 0%.

## Where the time goes

Each draft's journey is split into stages, each reported as p50, p90 and max (nearest rank,
in seconds):

| Stage | Measures | From |
|---|---|---|
| Waiting for a worker | queue delay | `started_at - created_at` |
| Analysis | Gemini processing | `completed_at - started_at` |
| Waiting for review | review turnaround | decision - the latest `draft.submitted` before it (`completed_at` for drafts from before the send-for-review step) |
| Going live | publishing lag | `published_at - scheduled_at` |

`started_at` and `completed_at` are durable stage timestamps (migration 0018). They are stamped
once: by an ORM listener on every status change (API, reconciler, dispatch), and with
`COALESCE` in the worker hooks' SQL updates, so a replayed result never moves them. Jobs that
finished before the migration have `completed_at` backfilled from their last update and no
`started_at`, so queue-wait and analysis times start with new jobs. Cache hits are done without
ever starting, so they count as drafted but not in analysis time.

## Breakdowns

- **By media:** uploads, approval rate, and analysis and review times for images and videos.
- **By channel:** per publishing channel and provider, deliveries requested, published and
  failed, success rate, the most common error, and the publishing lag.
- **Per day:** uploads, drafted, approved, rejected and published.
- **Previous period:** the same totals and rates for the equal-length period just before, for
  deltas.

## What to act on

Insights are plain rules, each requiring enough events (at least 5, more where noted) to mean
something:

| Insight | Fires when | Suggests |
|---|---|---|
| Bottleneck | One stage's median is at least 3× the next slowest (two or more stages measured) | Where to add capacity; review delays are flagged as warnings |
| Approval trend | Approval rate moved 10+ points against the previous period | Whether draft quality or review standards changed |
| Approved as written | Under 50% of approvals were first-pass | Improve the prompt or brief; rejection comments say what |
| Failures | 5%+ of at least 10 analyses failed | Check Activity for the errors |
| Channel | A channel's success rate is under 90% over 3+ deliveries | Names the most common error (e.g. rate limiting, reconnect) |
| Review backlog | Drafts are waiting for review, the oldest sent over a day ago (drafts the uploader hasn't sent yet are not counted) | Clear the queue |
| Volume | Uploads changed 30%+ on a previous period of at least 10 | Capacity and quota planning |

## Access

Managers and admins see their own workspace's report; members don't. Platform admins'
installation view lists each workspace's totals, rates and median times: no drafts, files,
people or ids of anything inside a workspace. Both are in the cross-tenant sweep
(`tests/integration/test_tenant_isolation.py`).

## Scale

Percentiles are computed in Python over the period's rows, which is ample for this product's
volumes over a year. A much larger deployment would move them into SQL (`percentile_cont` on
PostgreSQL) or a daily rollup table; the response shape would not change.

## Tests

`tests/integration/test_analytics.py` covers stage stamping (success, failure, cache hits,
replays), counts and rates including first-pass approval, exact stage timings, local-day
bucketing across timezones, previous-period comparison, channel and media breakdowns, each
insight rule and its data threshold, access, and range validation.
`frontend/src/views/Insights.test.tsx` covers the page.
