# Operating MediaFlow

How to see what MediaFlow is doing, what "healthy" means, and what to do when an alert fires.
For how jobs move between PostgreSQL and Redis, and why they cannot be lost, see
[JOB_LIFECYCLE.md](JOB_LIFECYCLE.md).

## Start the observability stack

```bash
# demo stack (no Gemini key) or the real one - same overlay either way
docker compose -f compose.demo.yaml -f compose.observability.yaml up -d --build --wait
docker compose -f compose.yaml      -f compose.observability.yaml up -d --build --wait
```

| Tool | URL (localhost only) | Use it for |
|---|---|---|
| Grafana | <http://localhost:3000> | **MediaFlow – Operations** dashboard (opens by default; anonymous viewers are read-only). Admin login: user `admin`, password from `GRAFANA_ADMIN_PASSWORD`, or if unset a random one shown by `docker compose logs grafana \| grep password` (new on every start). Five wrong attempts lock the login for 5 minutes. |
| Prometheus | <http://localhost:9090/alerts> | Every alert rule and whether it is firing |
| Jaeger | <http://localhost:16686> | Traces: search service `mediaflow-api` or `mediaflow-worker`, or paste a `trace_id` |

Without the overlay, everything still produces logs, `trace_id`s and metrics; there is just
nothing scraping or storing them. Point `OTEL_EXPORTER_OTLP_ENDPOINT` at your own collector
(Honeycomb, Grafana Cloud, Datadog, ...) and scrape port `9100` with your own Prometheus.

## Signals

### Service level

| SLI | Target (SLO) | Where |
|---|---|---|
| Upload → draft stored, p95 | **≤ 2 minutes** over 30 minutes | `mediaflow_job_end_to_end_seconds`, dashboard "p95 upload → draft", alert `MediaFlowSlowJobs` |
| Jobs that finish without failing | **≥ 90%** over 15 minutes | `mediaflow_job_outcomes_total`, alert `MediaFlowJobFailureRate` |
| API requests without a 5xx | **≥ 95%** over 10 minutes | `mediaflow_http_requests_total`, alert `MediaFlowAPIErrors` |

Starting points: tune them to your traffic and Gemini quota. `POST /api/v1/jobs` waits up to
5 minutes for a result by design, so it is excluded from the latency alert.

### Logs

One JSON object per line on stderr (`LOG_FORMAT=json`, the default in containers; `text` for a
terminal). Every line has `ts`, `level`, `logger`, `msg` and `service`, plus whichever of these
apply:

| Field | Set by | Meaning |
|---|---|---|
| `request_id` | API edge | From the caller's `X-Request-ID` if it is safe, otherwise generated, and echoed in the response. **Stored with the job**, so worker lines for that job carry it too. |
| `job_id` | API (upload), worker, reconciler | The job being handled. |
| `trace_id`, `span_id` | Any line inside a span | Paste `trace_id` into Jaeger. |
| `user_id`, `workspace_id` | Access log | Ids only, never emails or request bodies. |
| `route`, `status`, `duration_ms` | Access log (`logger: http.access`) | `route` is the template, e.g. `/api/v1/jobs/{job_id}`. |
| `outcome` | Worker (`job done`, `job failed`, `job retry`, ...) | Same values as the `outcome` metric label. |

Health probes are counted in metrics but not logged.

### Traces

The worker continues the upload request's trace. The W3C `traceparent` is stored in the Redis
job hash at submit time, so one trace shows:

```
POST /api/v1/jobs                    mediaflow-api      (INSERT/SELECT postgresql, Redis EVALSHA)
└─ job.process                       mediaflow-worker   time waiting on the rate limiter shows here
   ├─ gemini.analyse                 the provider call; errors recorded with their exception
   └─ UPDATE postgresql, SET DEL HSET   result stored in both stores
reconcile.sweep                      mediaflow-worker   one trace per repair sweep
```

Redis and SQL calls that have no parent span, such as idle worker polls, metric scrapes and
healthchecks, are deliberately not traced, so traces stay readable. SQL spans carry the
statement text, never the bound parameters. Sample with `OTEL_TRACES_SAMPLER_ARG` (0.0–1.0;
the default 1.0 keeps everything).

### Metrics

Served on the private port `METRICS_PORT` (9100) of every API and worker container, never on
the public API port. Labels are bounded: route templates, outcomes and error kinds. User,
workspace and job ids are never labels.

| Metric | Type | What it tells you |
|---|---|---|
| `mediaflow_queue_depth{state}` | gauge | ready / processing / deferred / dead, read live from Redis |
| `mediaflow_queue_oldest_ready_age_seconds` | gauge | How long the next job in line has waited since upload: **the backlog signal** |
| `mediaflow_job_outcomes_total{outcome}` | counter | done, cached, retry, deferred (rate-limited), failed, dead, persist_retry, missing |
| `mediaflow_job_queue_wait_seconds` | histogram | Upload accepted → first attempt started |
| `mediaflow_job_end_to_end_seconds` | histogram | Upload accepted → result stored (the SLI) |
| `mediaflow_gemini_request_seconds{kind,result}` | histogram | Provider latency; `result`: ok, rate_limited, transient, permanent, error |
| `mediaflow_gemini_calls_today`, `mediaflow_gemini_daily_limit` | gauge | Daily quota use |
| `mediaflow_gemini_cooldown_seconds` | gauge | Fleet-wide pause after a 429 |
| `mediaflow_worker_heartbeat_timestamp_seconds` | gauge | Last worker loop iteration; stale means stuck |
| `mediaflow_reconciler_actions_total{action}` | counter | dispatched, synced, requeued, recovered, abandoned, failed |
| `mediaflow_http_requests_total{method,route,status}`, `mediaflow_http_request_duration_seconds` | counter, histogram | API traffic and latency |
| `mediaflow_publish_outcomes_total{provider,outcome}` | counter | Publishing delivery outcomes (published, processing, retry, rejected, authorization, unknown_outcome, ...); see [PUBLISHING.md](PUBLISHING.md) |
| `mediaflow_jobs_accepted_total{status}` | counter | Uploads by dispatch result (queued, waiting, done = cache hit, dispatch_deferred = row saved but Redis unreachable, so the reconciler dispatches it; replayed = retry answered via `Idempotency-Key`) |
| `mediaflow_retention_purged_total` | counter | Stored objects deleted because no job still uses them |
| `mediaflow_media_deletions_total{reason}` | counter | Job originals deleted: `policy`, `published`, `manual`, `rejected_job` (see [STORAGE.md](STORAGE.md)) |
| `mediaflow_redis_up` | gauge | Redis answered the last scrape |
| `mediaflow_workers_alive` | gauge | Workers with a current heartbeat in Redis, including embedded workers that Prometheus can't discover |

Per-workspace product numbers (approval rates, review turnaround, volume by channel) are
deliberately not metrics: they come from durable rows, per tenant. See
[ANALYTICS.md](ANALYTICS.md).

## Investigating one job

A user reports "my upload never finished" and gives you the `X-Request-ID` from the response
(or you have the job id from the UI):

```bash
# every log line for it, from the API and the worker
docker compose logs --no-log-prefix api worker | grep '<request_id or job_id>'
# its state in both stores
docker compose exec worker python -m app.cli status <job_id>
```

Take the `trace_id` from any of those lines and open it in Jaeger to see where the time went:
the queue, the limiter, Gemini or the database.

## Alerts

Each heading matches the alert name; the `runbook` annotation on every alert links here.

### MediaFlowNoWorkers

**Meaning:** no worker is being scraped. Uploads are accepted and queue up, but nothing reaches
Gemini. The API still answers. Because no worker heartbeat is in Redis
(`mediaflow_workers_alive` = 0), uploads return `202` after about a second instead of holding
the request open, the API logs `no live worker; not waiting for the result`, and users see
jobs stuck in "Queued".
**Check:** `docker compose ps worker` and `docker compose logs --tail=50 worker`. A worker that
exits at once usually has a config error, which the logs name, e.g. a missing
`GEMINI_API_KEY`.
**Fix:** `docker compose up -d worker`. Nothing is lost: queued jobs wait in Redis and are
processed when a worker returns.

### MediaFlowWorkerStalled

**Meaning:** workers are up, but their loop has not run for over 2 minutes. A worker is blocked
inside one job (a hung provider call, or a very slow video compression) or deadlocked.
**Check:** the dashboard's "Analysing" stat, and in Jaeger an unfinished `job.process` that
never ends. `docker compose logs worker` shows the last job it started.
**Fix:** `docker compose restart worker`. Its lease expires (`QUEUE_LEASE_S`), the reaper puts
the job back on the queue, and another attempt runs. If one video keeps doing this, it will go
to dead letters after `QUEUE_MAX_ATTEMPTS`.

### MediaFlowAPIDown

**Meaning:** Prometheus cannot scrape any API container.
**Check:** `docker compose ps api`; `curl localhost:8080/health/ready` returns `503` naming the
database or Redis if a dependency is the cause.
**Fix:** restart the API, or the dependency it names.

### MediaFlowRedisDown

**Meaning:** Redis is unreachable. Uploads fail with an error, and workers cannot claim jobs.
**Check:** `docker compose ps redis` and `docker compose logs redis`. Disk full is the usual
cause with append-only persistence.
**Fix:** restore Redis. Then watch `MediaFlowStoresDiverged`: if Redis lost data, the
reconciler rebuilds the missing jobs from PostgreSQL within about a minute, with no manual step.

### MediaFlowQueueBacklog

**Meaning:** the next job in line has waited over 5 minutes since upload (the critical variant
fires at 30). Work is arriving faster than it is finished.
**Check, in this order:**
1. "Rate-limit pause" or "Gemini quota used" on the dashboard. If either is high, the provider
   is the bottleneck, not your workers (see the Gemini alerts below).
2. "Workers up", "Analysing" and job outcomes. Too few workers for the load?
3. p95 "Analysis call latency". Have Gemini calls got slow?

**Fix:** add workers (`docker compose up -d --scale worker=3`; they share the rate limiter, so
this only helps if the limiter is not the cap), or raise `GEMINI_RPM`/`GEMINI_RPD` if your quota
allows it.

### MediaFlowJobFailureRate

**Meaning:** more than 10% of finished jobs fail.
**Check:** the "Analysis calls by result" panel. `permanent` means bad media, blocked content
or an auth problem; `transient`/`error` means the provider or network. Then
`docker compose logs worker | grep '"job failed"'` for the `error` text of recent failures.
**Fix:** permanent auth errors mean a bad or expired `GEMINI_API_KEY`, or a wrong
`GEMINI_MODEL`. Content-specific failures are expected and need no action.

### MediaFlowDeadLetters

**Meaning:** at least one job used up all its retries (`QUEUE_MAX_ATTEMPTS`) on retryable
errors.
**Check:** `docker compose exec redis redis-cli LRANGE ma:dead 0 -1` lists them; `app.cli status
<id>` shows the last error. The job is `dead` in PostgreSQL too, and the user sees "Failed".
**Fix:** fix the cause first, then have the user upload again. A dead job is never retried
automatically.

### MediaFlowSlowJobs

**Meaning:** p95 upload-to-draft time is above the 2-minute SLO.
**Check:** split the time. If "Time in queue" is high, it's a backlog (see above). If queue
time is low but end-to-end is high, Gemini is slow ("Analysis call latency") or videos are
large and spend time in compression. In Jaeger, the slowest `job.process` traces show which.

### MediaFlowGeminiRateLimited

**Meaning:** Gemini keeps answering 429, and the whole fleet has been paused for 10+ minutes.
Jobs wait, but are not lost.
**Fix:** lower `GEMINI_RPM` to match the limit shown in AI Studio, or raise your quota. More
workers make this worse, not better.

### MediaFlowGeminiQuotaNearlyUsed

**Meaning:** over 90% of today's `GEMINI_RPD` is used. When it runs out, jobs wait until the
daily reset at midnight US Pacific time.
**Fix:** expected on a busy day. If it happens every day, raise the quota, or check
`mediaflow_jobs_accepted_total{status="done"}` (cache hits) to confirm dedup is working.

### MediaFlowStoresDiverged

**Meaning:** the reconciler had to rebuild or requeue jobs that Redis lost. The jobs are fine,
because they were recovered automatically, but **Redis lost data**, which it should not do.
**Check:** `docker compose logs worker | grep reconciler` lists the job ids it repaired. Look
for a Redis restart without its volume, a `FLUSHALL`, or memory eviction (`maxmemory-policy`
must be `noeviction` for a queue).
**Fix:** make Redis durable (append-only file on a persistent volume, `noeviction`). The
lifecycle doc explains each repair: [JOB_LIFECYCLE.md](JOB_LIFECYCLE.md).

### MediaFlowAPIErrors

**Meaning:** more than 5% of API requests return 5xx.
**Check:** `docker compose logs api | grep '"level": "error"'`; every 5xx access-log line has
the `route` and `trace_id`, and the matching trace shows the exception.
**Fix:** depends on the route; a spike on every route at once usually means PostgreSQL or
Redis (`/health/ready` says which).

### MediaFlowAPISlow

**Meaning:** p95 latency, excluding uploads, is above 1 second.
**Check:** "p95 latency by route" shows which route; its traces in Jaeger show whether the time
goes on SQL (look for many `SELECT postgresql` spans, an N+1) or Redis.

## Routine operations

| Task | Command |
|---|---|
| Queue and quota at a glance | `docker compose exec worker python -m app.cli stats` |
| One repair sweep now | `docker compose exec worker python -m app.cli reconcile` |
| Apply retention now | `docker compose exec worker python -m app.cli prune-media` (add `--dry-run` to only count) |
| Check the media store | `docker compose exec worker python -m app.cli storage-check` |
| Back up / restore | `scripts/backup.sh`, `scripts/restore.sh <dir> --yes` (see [DEPLOYMENT.md](DEPLOYMENT.md)) |
| More workers | `docker compose up -d --scale worker=3` (Prometheus finds them automatically) |
| Reload alert rules after editing | `curl -X POST localhost:9090/-/reload` |
| Test alert rules | `docker run --rm --entrypoint promtool -v "$PWD/ops/prometheus:/p" -w /p prom/prometheus:v3.7.0 test rules alerts_test.yml` |
| Change the dashboard | edit `ops/grafana/generate_dashboard.py`, run it with `ops/grafana/dashboards/mediaflow.json` as the argument, restart Grafana |

## Live updates

Browsers keep one `GET /api/v1/events` stream open per tab (Server-Sent Events,
`app/api/live.py`). Committed changes publish topic names (never content) on Redis pub/sub
channels `<QUEUE_PREFIX>:live:ws:<workspace>` and `<QUEUE_PREFIX>:live:user:<user>`; the API and
the worker both publish.

- **Screens stop updating by themselves:** the stream is being buffered or cut by something in
  front of the API. The nginx overlay turns buffering off for `/api/v1/events`; another proxy or
  load balancer needs the same, and an idle timeout above 20 seconds (the heartbeat). Browsers
  still refresh when a tab regains focus, and the bell still checks every 30 seconds.
- **Redis down:** saves still succeed (publishing is best effort); live updates resume when it
  is back, and each browser refreshes everything when it reconnects.
- Each stream ends after 10 minutes and the browser reconnects, re-checking the session, so a
  removed member stops hearing within that time. A restart cuts streams after 5 seconds
  (`--timeout-graceful-shutdown`); browsers reconnect on their own.
- Watch: `redis-cli PUBSUB NUMSUB <prefix>:live:ws:<workspace>` shows how many tabs listen.

## Capacity

Measured on a 4-CPU machine with the demo stack (`python scripts/load_test.py`, 100 requests
at once per scenario, client on the same machine):

| 100 at once | Before (Nov 2026 review) | 1 API replica | 3 API replicas |
|---|---|---|---|
| `GET /health/live` | 100/100 | 100/100 | 100/100, avg 0.1 s |
| `GET /auth/me` | 100/100, avg 0.65-1.2 s | 100/100, avg 0.76 s | 100/100, avg 0.77 s |
| `POST /auth/login` | 70-75/100, the rest **500**, up to 67 s | **100/100**, max 9 s | 100/100, max 11 s |
| `POST /jobs` (upload) | **0/100** (500s and 2-minute timeouts) | **100/100**, avg 2.8 s | **100/100**, avg 1.5 s |

What changed, and why:

- **Sign-in releases its database connection before checking the password.** Argon2 is slow
  on purpose (about 110 ms of all four CPUs per check at the recommended settings); holding a
  connection through it is what exhausted the pool.
- **At most `PASSWORD_HASH_CONCURRENCY` password checks run at once** (default: the CPUs the
  container may use, read from its cgroup limit: `os.cpu_count()` reports the host's, which
  over-committed a CPU-limited container), each using 64 MiB. A sign-in waits its turn in an
  async queue that holds no request thread, up to `PASSWORD_HASH_WAIT_S` (30 s), then gets
  **503** with `Retry-After`; the web app retries once. An unknown email is checked against a
  stand-in hash, so its response time doesn't reveal which addresses have accounts.
- **Sign-in throughput is set by CPU, on purpose:** one check takes about 110 ms of 4 CPUs, or
  about 0.5 s on 1 CPU. 100 people signing in at the same moment on a 1-CPU API need about
  50 s: measured with the API limited to 1 CPU, 59 of 100 got in within the 30 s wait, and all
  100 with the web app's retry (the last after 61 s). More CPUs per API replica is what makes
  this faster; lowering Argon2's cost would too, at the price of weaker password storage.
- **Every request releases its connection after the sign-in check**, before it may queue for a
  thread, so a burst can't hold more connections than there are threads to use them.
- **Uploads answer at once (202)**. Waiting for the draft held a thread per upload for up to
  five minutes; a client that wants it sends `Prefer: wait=N` (at most 300). The web app never
  needed it: it hears about the draft through live updates.
- **A full pool answers 503 with `Retry-After` within `DB_POOL_TIMEOUT`**, never a 500 after
  30 s. The web app retries reads and sign-in once, after the wait the server asks for.
- `mediaflow_overload_total{resource}` counts those 503s (`password_hashing`,
  `database_pool`). If it climbs, add replicas or raise the pool.

### Settings (per API process)

| Setting | Default | What it is |
|---|---|---|
| `API_THREADS` | 40 | Requests that run at once |
| `DB_POOL_SIZE` + `DB_MAX_OVERFLOW` | 20 + 20 | Connections; keep the sum at least `API_THREADS` |
| `DB_POOL_TIMEOUT` | 10 s | How long a request waits for a connection before 503 |
| `PASSWORD_HASH_CONCURRENCY` | CPUs allowed | Password checks at once (64 MiB each) |
| `PASSWORD_HASH_WAIT_S` | 30 s | How long a sign-in waits its turn before 503 |
| `API_REPLICAS` | 2 | API containers behind the HTTPS proxy (`compose.proxy.yaml`) |
| `POSTGRES_MAX_CONNECTIONS` | 200 | Keep above replicas x (pool + overflow) + the worker's |

### Scaling out

Behind the proxy (`compose.proxy.yaml`), the API runs as `API_REPLICAS` containers. nginx
re-reads Docker's DNS every 10 seconds, spreads requests across replicas, and skips one that is
restarting within 3 seconds; it never re-sends an upload that reached a replica. Everything
replicas share lives in Redis or PostgreSQL (sign-in throttling, live updates, the job queue),
so any number can run. On one machine, replicas share its CPUs: they help uploads and
database-bound requests most, and a server with more cores gains more. Sign-ins are bound by
CPU however many replicas run.

Re-run the benchmark after changing any of this, against a demo stack only (it signs in with
the demo accounts and creates jobs): `python scripts/load_test.py http://localhost:8081`.
