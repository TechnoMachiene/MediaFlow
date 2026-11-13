# MediaFlow — AI Media Analysis & Content Calendar

MediaFlow is a multi-tenant app (FastAPI backend plus a React web app) for uploading images and
videos, generating social-media content drafts with Google Gemini, reviewing jobs with
role-based workspaces, and planning approved posts on a timezone-aware calendar.

## Try the demo: no Gemini key, nothing to install

[![Open in GitHub Codespaces](https://github.com/codespaces/badge.svg)](https://codespaces.new/TechnoMachiene/mgc_viral_machine?quickstart=1)

**In your browser:** click the button above (you need a free GitHub account). GitHub builds a
private copy, starts the demo, and opens it in a new tab, which takes a few minutes the first
time. Then sign in with one of the accounts below. It runs on your GitHub Codespaces quota
(personal accounts get free hours each month), and only you can open your demo's URL.

**On your machine,** with only Docker installed (no `.env`, no API key):

```bash
git clone https://github.com/TechnoMachiene/mgc_viral_machine.git && cd mgc_viral_machine
docker compose -f compose.demo.yaml up --build
```

Open **<http://localhost:8081>** when the log prints the `MediaFlow DEMO MODE` banner. The
first build takes a few minutes; after that, startup takes about half a minute.

| Sign in as | Email | Password | Can |
|---|---|---|---|
| Admin | `demo-admin@example.com` | `demo-password-123` | Everything, including Team and Activity |
| Manager | `demo-manager@example.com` | `demo-password-123` | Upload, review (approve/reject), add people |
| Member | `demo-member@example.com` | `demo-password-123` | Upload and schedule their own approved drafts |

The sign-in page also has one-click **Continue as …** buttons for each of these.

**Walk through the whole workflow (about 2 minutes):**

1. **Continue as Member** and upload any photo in **Library**. Within a few seconds it shows
   **In review**, with Instagram, TikTok and Facebook drafts written from the photo's real
   colours and shape.
2. Sign out, **Continue as Manager**, open **Review**, and approve the draft.
3. Sign out, **Continue as Member**, open the draft, and schedule it for a future date.
4. Open **Calendar** to see it planned.

Shortcut: the Admin can do all four steps in one account. To check the same workflow from a
terminal, run `python3 scripts/demo_check.py` (it needs only Python).

Stop with `Ctrl+C`. Run it in the background with `docker compose -f compose.demo.yaml up -d
--build --wait` (or `make docker-demo`). Delete it and all its data with
`docker compose -f compose.demo.yaml down --volumes` (`make docker-demo-down`). To use another
port, set `MEDIAFLOW_DEMO_PORT=9000`.

Without Docker, with Redis and Node.js installed, run `make demo` instead. It serves on
<http://localhost:8082> and uses SQLite and a worker inside the API process.

### What the demo is, and isn't

Drafts come from a scripted stand-in (`app/demo.py`), not Gemini. They are deterministic
templates filled with facts read from the image, and a banner on every page says so.
Everything else is the real system: PostgreSQL, the Redis queue, a separate worker process,
workspaces and roles, review and calendar.

The scripted provider is confined to local and demo use, and the code enforces that:

- It is built only when `DEMO_MODE=1` (`build_client` in `app/gemini_client.py`).
- `DEMO_MODE=1` **refuses to start if `GEMINI_API_KEY` is set**, so a demo can't run inside a
  deployment that has real credentials.
- `DEMO_MODE=1` **requires a Redis prefix starting with `demo`**, so a demo worker can never
  take jobs from, or write simulated drafts into, the real queue (`ma`).
- Demo accounts, with their published password, are **only seeded into an empty database**
  (or one the demo created), never into one that holds real users.
- `compose.demo.yaml` is self-contained and reads nothing from `.env`, so real settings can't
  leak in. It runs as its own Compose project with its own volumes.

Tests for each guard are in `tests/integration/test_demo_mode.py`. Never set `DEMO_MODE=1` on
a real deployment.

## Run with Docker

`compose.yaml` runs the whole system: PostgreSQL 17, Redis 8 (append-only, so the queue
survives restarts), a one-shot `migrate` step, the API (which also serves the web app) and a
separate queue worker. API and worker are the same image; the worker also runs the DB↔Redis
reconciler and media retention. Only the API is published, on **port 8080**; PostgreSQL and
Redis stay on the private Compose network, so they never clash with local installs.

```bash
cp -n .env.example .env              # set GEMINI_API_KEY, GEMINI_MODEL, JWT_SECRET (openssl rand -hex 32)
make docker-up                       # build, migrate, start, wait until healthy
make docker-admin EMAIL=you@example.com   # create the first admin (prompts for a password)
open http://localhost:8080
```

| Task | Command |
|---|---|
| Logs | `make docker-logs` |
| More workers | `docker compose up -d --scale worker=3` (they share the rate limiter) |
| Stop (keeps data) | `make docker-down` |
| One repair sweep | `docker compose exec worker python -m app.cli reconcile` |
| Change the port | `MEDIAFLOW_PORT=9000` in `.env` |

Compose reads `.env` only to fill in variables. `DATABASE_URL` and `REDIS_URL` are set by
Compose to point at its own containers, and `.env` is never copied into the image. Set
`POSTGRES_PASSWORD` in `.env` before the first start; it defaults to `mediaflow`, which is
acceptable only because PostgreSQL is not published. Originals live in the `uploads` volume;
for several hosts, use `MEDIA_STORAGE=s3` instead.

## Observability

Add one overlay to either stack to get metrics, alerts, a dashboard and distributed traces:

```bash
docker compose -f compose.demo.yaml -f compose.observability.yaml up -d --build --wait  # or: make obs-demo
```

| | |
|---|---|
| **Dashboard** | Grafana at <http://localhost:3000>: "MediaFlow – Operations" shows backlog and oldest waiting job, the upload→draft SLI, job outcomes and failure ratio, Gemini latency, quota and rate-limit pauses, API rate/latency/errors, and reconciler repairs. Viewing needs no login. There is no default admin password: set `GRAFANA_ADMIN_PASSWORD`, or read the random one from `docker compose logs grafana`. |
| **Alerts** | 14 Prometheus rules (<http://localhost:9090/alerts>), each linked to a runbook entry in [docs/OPERATIONS.md](docs/OPERATIONS.md) and covered by `promtool` unit tests in CI. |
| **Traces** | Jaeger at <http://localhost:16686>. A worker's `job.process` continues the trace of the upload request that created it, because the trace context travels in the Redis job hash. One trace shows the request, the queue and rate-limit wait, the Gemini call, and every SQL statement. |
| **Logs** | JSON lines with `request_id`, `job_id` and `trace_id` on every line. The `X-Request-ID` returned by the API also appears on the worker's lines for that job. |
| **Metrics** | Prometheus format on a private port (`METRICS_PORT`, never the public one), with bounded labels: no user, workspace or job ids. |

Without the overlay, logs, trace ids and metrics are still produced. Point
`OTEL_EXPORTER_OTLP_ENDPOINT` and your own Prometheus at any backend. The runbook lists every
signal, the SLOs, and how to trace a single user's job.

> **Current scope:** This project analyzes media and creates content suggestions. It does not
> connect to Instagram, Facebook, or TikTok, and it does not automatically publish posts. The
> calendar is for planning and tracking only.

## What it does

- Accepts images and videos through a command-line interface or an authenticated FastAPI endpoint.
- Validates media type, file contents, and configured size limits.
- Uses Gemini to analyze the media and draft platform-tailored captions and content suggestions.
- Processes work asynchronously through a Redis queue, with status checks and retries.
- Deduplicates identical media/model/prompt jobs to avoid repeated Gemini calls where possible.
- Stores user/job records and durable completed results in PostgreSQL, and keeps PostgreSQL and
  Redis consistent through every crash point
  (see [docs/JOB_LIFECYCLE.md](docs/JOB_LIFECYCLE.md)).
- Isolates tenants in **workspaces**. Every job, post and permission belongs to one workspace,
  people can belong to several with a different role in each, and dedup never crosses tenants.
- Supports members, managers and admins per workspace, platform admins, and a protected
  super-admin account.
- Stores originals on local disk or any S3-compatible bucket, and applies a media retention
  policy (originals deleted after 30 idle days by default; drafts kept).
- Lets managers/admins review completed work and approve or reject it.
- Lets owners schedule completed, approved jobs and view, reschedule, or cancel them in a calendar.
- Stores schedule instants in UTC and displays them in the requested IANA timezone.
- Ships a responsive web app (`frontend/`) covering upload, review, calendar, team, and queue
  activity.
- Includes automated unit and integration tests that use synthetic media and a scripted Gemini
  client, plus a Vitest + Testing Library suite for the web app; live Gemini tests are separate.
- Runs in a zero-setup demo mode with an offline draft writer.

## What it does not do

- It does not connect social accounts, send posts to platform APIs, or publish automatically when
  a calendar date arrives.
- It does not provide in-app notifications for review results or scheduled dates.
- It does not offer invitations, billing or per-workspace quotas yet. A workspace admin creates
  accounts directly, and the Gemini rate limit is shared by the whole installation.
- Demo drafts are deterministic templates, not real media understanding.

## Workflow

```text
upload -> validate -> store original -> job row (PostgreSQL outbox) -> Redis queue -> worker
       -> Gemini -> result in Redis + PostgreSQL          (reconciler repairs any gap)
completed job -> manager review -> approved -> owner schedules -> calendar
```

The Gemini system prompt is designed to inspect the supplied media and generate platform-specific
content drafts, including captions and related suggestions. Drafts are returned as analysis
results; they are not sent to social platforms.

## Technology

- Python 3.11 or newer
- FastAPI and Uvicorn for the HTTP API
- Redis for job queues, job status, caching, and shared rate limiting
- PostgreSQL for accounts, job metadata, approval state, schedules, and durable results
- SQLAlchemy and Alembic for database access and migrations
- Google Gemini for media analysis and draft generation
- Pillow for image inspection and resizing
- FFmpeg for compressing larger videos
- Local disk or S3-compatible object storage (boto3) for originals
- Pytest, Ruff, Vitest and Testing Library for testing and linting

## Quick setup on Ubuntu or WSL

The repository includes a setup script that installs the system packages, creates a virtual
environment, and runs the automated test gate:

```bash
bash quickstart.sh
```

Or install the dependencies and run the test suite manually:

```bash
sudo apt-get update
sudo apt-get install -y redis-server postgresql python3-venv python3-pip make ffmpeg
sudo service redis-server start
sudo service postgresql start

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
export REDIS_URL=redis://localhost:6379/0
make done
```

`make done` runs linting, unit tests, integration tests, and the coverage check. It does not call
Gemini. Integration tests need a reachable Redis service. Never run `FLUSHDB` against a database
that contains data you need.

## Configure Gemini

Copy the example configuration without replacing an existing `.env`, then set your Gemini API key
and model:

```bash
cp -n .env.example .env
nano .env
```

Set at least:

```dotenv
GEMINI_API_KEY=your-key
GEMINI_MODEL=your-model-id
```

Set `GEMINI_RPM` and `GEMINI_RPD` to the limits shown in your AI Studio project. If omitted, the
application uses conservative defaults of 5 requests per minute and 100 requests per day. Keep
`.env` private and never commit or share it.

To make a small real Gemini request:

```bash
make live
```

This calls Gemini and may incur usage or charges. Use non-sensitive test media unless you have
confirmed the privacy and billing terms for your account.

## CLI usage

The CLI can submit jobs, inspect status and queue statistics, and run a worker. These commands use
real Gemini when processing a job:

```bash
# Submit an image to the queue
python -m app.cli submit uploads/cat.png --user 1

# Inspect queue and rate-limit statistics
python -m app.cli stats

# Process the current queue and exit when idle
python -m app.cli worker --drain

# Check a job
python -m app.cli status JOB_ID

# Run a persistent worker; Ctrl+C stops it
python -m app.cli worker
```

Identical uploads may be reported as waiting for an existing job or may reuse its completed
result. Multiple workers share the Redis-backed rate limiter.

## FastAPI setup

### 1. Configure PostgreSQL

Start PostgreSQL and create a local development database and user. Choose your own password:

```bash
sudo service postgresql start
sudo -u postgres psql
```

In the `psql` prompt:

```sql
CREATE USER media_queue WITH PASSWORD 'choose-a-local-password';
CREATE DATABASE media_queue OWNER media_queue;
\q
```

Set `DATABASE_URL` in `.env`, URL-encoding special characters in the password if needed:

```dotenv
DATABASE_URL=postgresql+psycopg://media_queue:choose-a-local-password@localhost:5432/media_queue
JWT_SECRET=replace-with-at-least-32-random-bytes
MEDIA_STORAGE_DIR=uploads
```

Generate a JWT secret, for example:

```bash
openssl rand -hex 32
```

### 2. Migrate and create the administrator

```bash
source .venv/bin/activate
alembic upgrade head
python -m app.api.admin abdullahtauseef988@gmail.com
```

The admin command prompts for a password securely. It makes the account a platform admin and an
admin of the oldest workspace; on a fresh install it creates a "Default workspace". Pass
`--workspace NAME` to choose or create another. The designated email is protected as the
super-admin account. Running the command again for an existing account resets its password and
revokes its existing tokens.

Upgrading from a release without workspaces: `alembic upgrade head` moves all existing users,
jobs and posts into "Default workspace", and each user's old global role becomes their role
there.

### 3. Start the API and worker

Run the API and Gemini worker in separate terminals from the project directory:

```bash
# Terminal 1
source .venv/bin/activate
uvicorn app.api.main:app --reload
```

```bash
# Terminal 2
source .venv/bin/activate
python -m app.cli worker
```

The API and worker must use the same Redis queue, PostgreSQL database and media storage: either
a shared `MEDIA_STORAGE_DIR`, or the same S3 bucket with `MEDIA_STORAGE=s3`, which needs no
shared disk. FFmpeg is needed on worker machines to compress videos that exceed the Gemini
processing size limit.

Nothing reaches Gemini until a worker runs, so the upload stays `queued`. For local
development you can skip Terminal 2 with `EMBEDDED_WORKER=1`, which runs a worker inside the API
process. Each worker also runs the DB↔Redis reconciler every 30 seconds and the media retention
pruner every hour.

Interactive API documentation: <http://127.0.0.1:8000/docs>

### 4. Web app

The web app lives in `frontend/` (React, TypeScript, Vite; needs Node.js 20.19 or newer).

```bash
make web        # install dependencies and build frontend/dist
```

When `frontend/dist` exists, the API serves it at <http://127.0.0.1:8000/>, so one origin hosts
both the app and the API. Restart `uvicorn` after the first build so it picks the folder up.

For frontend development with hot reload, keep the API running and start Vite in a third
terminal. It serves <http://127.0.0.1:5173> and proxies `/api` and `/health` to the API
(override the target with `MEDIAFLOW_API=http://host:port`):

```bash
make web-dev
```

Sign in with an account created by the admin command above. Roles are per workspace; if you
belong to several workspaces, a switcher appears in the sidebar (or the account sheet on phones).
What each role sees:

| Screen | Who | What it does |
|---|---|---|
| Library | Everyone | Upload (button or drag and drop), follow analysis, read and copy drafts, cancel queued jobs, schedule approved drafts. |
| Review | Manager/admin | Approve or reject finished drafts; browse approved and rejected ones. |
| Calendar | Everyone | Month view of planned posts in any timezone; reschedule or cancel. |
| Team | Manager/admin | Add people; admins can also edit roles, reset passwords, and remove accounts. |
| Activity | Admin | This workspace's job counts, service health, and every job. Platform admins also see Gemini calls today. |

The access token is kept in the browser's session storage and expires after
`ACCESS_TOKEN_TTL_MINUTES`; the app then asks you to sign in again.

## API reference

All application endpoints below use the `/api/v1` prefix unless noted otherwise. Authenticated
endpoints expect `Authorization: Bearer <access_token>`. Workspace-scoped endpoints act in the
workspace named by the optional `X-Workspace-Id` header, or the caller's oldest workspace when it
is absent. A caller who is not a member gets `403`. "Manager" and "admin" below mean the
caller's role in that workspace, and records from other workspaces always answer `404`.

### Authentication and account management

| Method and path | Access | Purpose |
|---|---|---|
| `GET /meta` | Public | Whether demo mode and sign-up are on (and demo accounts in demo mode). |
| `POST /auth/login` | Public | Log in and receive a bearer token. |
| `POST /auth/signup` | Public, if `ALLOW_SIGNUP=1` | Create an account plus a new workspace it administers. |
| `GET /auth/me` | Authenticated | The caller, their role in the current workspace, and all their workspaces. |
| `GET /workspaces` | Authenticated | The caller's workspaces and role in each. |
| `POST /workspaces` | Platform admin, or anyone if sign-up is open | Create a workspace; the creator becomes its admin. |
| `POST /users` | Manager/admin | Create an account in this workspace; admins can also create admins. |
| `GET /users`, `GET /users/{user_id}` | Admin | List or get members of this workspace. |
| `PATCH /users/{user_id}` | Admin | Update role, email or password. |
| `PATCH /users/{user_id}/role` | Admin | Change a member's role in this workspace. |
| `DELETE /users/{user_id}` | Admin | Remove a member and their jobs here; the account is deleted if it is in no other workspace. |

Managers can create users and managers, but cannot create admins. A workspace always keeps at
least one admin. An admin can change a member's email or password only if that account belongs
to no other workspace, so one tenant can never take over a person shared with another. The
protected super-admin account cannot be modified or deleted through the API. Without
`ALLOW_SIGNUP=1`, only managers and admins can create accounts.

### Media jobs and review

| Method and path | Access | Purpose |
|---|---|---|
| `POST /jobs` | Authenticated | Upload an image/video as multipart field `file` and enqueue analysis. |
| `GET /jobs/mine` | Authenticated | List the caller's jobs with processing and approval statuses; supports `limit` and `offset`. |
| `GET /jobs/{job_id}` | Owner/manager/admin | Get a job's current status and result; non-admins can access their own jobs. |
| `DELETE /jobs/{job_id}` | Owner/manager/admin | Cancel an owned job when it has not started processing. |
| `GET /jobs/{job_id}/media` | Owner/manager/admin | Download the protected original upload; `410` once retention deleted it. |
| `GET /jobs/review` | Manager/admin | List completed jobs awaiting review. |
| `GET /jobs/approved` | Manager/admin | List completed, approved jobs. |
| `GET /jobs/rejected` | Manager/admin | List completed, rejected jobs. |
| `PATCH /jobs/{job_id}/approval` | Manager/admin | Approve or reject a completed job. |
| `GET /jobs` | Admin | Search/list jobs, with pagination and optional media-kind filter. |
| `GET /stats` | Admin | This workspace's job counts; platform admins also get installation-wide queue depths and Gemini calls today. |

Review items include the submitter's email and protected media URL. Once a job is approved or
rejected, it no longer appears in the pending review list. Upload responses include the result
when processing finishes during the response wait (up to 5 minutes); otherwise the API returns
`202 Accepted` and the job can be checked later. The API only waits while a worker is alive:
workers heartbeat into Redis, and with none alive, or if the last one stops mid-wait, the
upload returns `202` within about a second instead of holding the request open.

Supported uploads are JPG/JPEG, PNG, WebP, MP4, MOV, and WebM. Defaults are 10 MB per image,
100 MB per processed video, and 1 GB per uploaded video before compression; configure these in
`.env`. The same bytes uploaded more than once are stored once. A filename collision with
different content is handled using a short content-hash suffix, while each job retains its
original filename.

### Calendar-only scheduling

| Method and path | Access | Purpose |
|---|---|---|
| `POST /jobs/{job_id}/schedule` | Job owner | Schedule a future time for a completed, approved job. |
| `GET /scheduled-posts` | Authenticated | List posts for a date range. |
| `GET /calendar` | Authenticated | Get date-grouped posts for a date range. |
| `GET /scheduled-posts/{post_id}` | Owner/admin | Inspect a scheduled post. |
| `PATCH /scheduled-posts/{post_id}` | Owner/admin | Change its scheduled time or timezone. |
| `DELETE /scheduled-posts/{post_id}` | Owner/admin | Mark it cancelled. |

Provide `start`, `end`, and an IANA `timezone` (for example `Asia/Karachi`) to list or query the
calendar. Ranges may not exceed 93 days; calendar responses are capped at 1,000 posts. Schedule
timestamps must include a UTC offset and must be in the future. The API stores timestamps in UTC
and converts them for calendar display. A job may have only one scheduled entry. Cancelled entries
remain stored and visible with their cancelled status.

Scheduling is only a planning record. A scheduled time does not trigger a social post or an
automatic task.

### Health checks

| Method and path | Purpose |
|---|---|
| `GET /health/live` | Check that the API process is alive. |
| `GET /health/ready` | Check PostgreSQL, required tables, and Redis availability. |

## Job lifecycle

Queue/analysis statuses commonly include:

- `queued`: waiting for a worker.
- `processing`: a worker is analyzing the media.
- `waiting`: the same media is already being processed by another job.
- `retrying`: a retryable failure is waiting for backoff.
- `done`: Gemini returned a result.
- `failed`: a non-retryable error occurred.
- `dead`: the job exhausted its retry limit.
- `cancelled`: a queued job or calendar entry was cancelled.

Approval is tracked separately: `pending`, `approved`, or `rejected`. Only jobs with analysis status
`done` and approval status `approved` can be scheduled.

PostgreSQL and Redis are kept in agreement without a shared transaction. The job row is an
outbox committed before Redis hears of the job, the worker writes status and results back, and
a reconciler repairs every divergence: an API crash before dispatch, a DB outage mid-result, a
Redis flush or lost queue entry, or a crashed worker. [docs/JOB_LIFECYCLE.md](docs/JOB_LIFECYCLE.md)
has the sequence diagram, the full failure matrix and the test that covers each row. Run a
repair sweep by hand with `make reconcile`.

## Media storage and retention

| Setting | Effect |
|---|---|
| `MEDIA_STORAGE=local` (default) | Originals in `MEDIA_STORAGE_DIR`; API and workers must share it. |
| `MEDIA_STORAGE=s3` + `S3_BUCKET` | Originals in any S3-compatible bucket (AWS, R2, MinIO via `S3_ENDPOINT_URL`). Workers download a temporary copy per job. |
| `MEDIA_RETENTION_DAYS=30` (default) | Delete an original once nobody has uploaded those bytes for 30 days, no job using it is active, and no upcoming scheduled post uses it. `0` keeps originals forever. |

Retention deletes originals only. Drafts, approvals and calendar entries are kept, and the
media URL answers `410 Gone`. Uploading the same file again restores it. Workers apply the
policy hourly; run it once with `make prune-media`. Abandoned partial uploads more than a day
old are removed too.

## Configuration

See [.env.example](./.env.example). Common settings:

| Variable | Purpose |
|---|---|
| `REDIS_URL` | Redis connection used by the queue and limiter. |
| `QUEUE_PREFIX` | Namespace for this application's Redis keys. |
| `DATABASE_URL` | PostgreSQL SQLAlchemy connection string (SQLite only in demo mode). |
| `JWT_SECRET` | Secret used to sign access tokens; at least 32 bytes. |
| `ACCESS_TOKEN_TTL_MINUTES` | JWT lifetime; defaults to 30 minutes. |
| `MEDIA_STORAGE_DIR` | Persistent upload storage directory; defaults to `uploads`. Also the staging area for S3 uploads. |
| `MEDIA_STORAGE`, `S3_BUCKET`, `S3_PREFIX`, `S3_ENDPOINT_URL`, `S3_REGION` | Where originals live: `local` or `s3`. |
| `MEDIA_RETENTION_DAYS` | Days an unused original is kept; default 30, `0` keeps forever. |
| `ALLOW_SIGNUP` | Self-serve sign-up that creates a workspace; default off. |
| `EMBEDDED_WORKER` | Run a queue worker inside the API process; default off. |
| `DEMO_MODE` | Offline drafts, SQLite, embedded worker and demo accounts; never in production. |
| `GEMINI_API_KEY`, `GEMINI_MODEL` | Gemini credentials and model identifier (not needed in demo mode). |
| `GEMINI_RPM`, `GEMINI_RPD`, `GEMINI_BURST` | Shared per-minute, daily, and burst call limits. |
| `QUEUE_LEASE_S`, `QUEUE_MAX_ATTEMPTS` | Worker lease duration and retry limit. |
| `MAX_IMAGE_MB`, `MAX_VIDEO_MB`, `MAX_VIDEO_UPLOAD_MB` | Media upload/processing limits. |
| `IMAGE_MAX_SIDE`, `VIDEO_FPS` | Image resize and video sampling settings. |
| `RESULT_TTL_S`, `JOB_TTL_S`, `INFLIGHT_TTL_S` | Redis result, job, and in-flight record lifetimes. |

## Development and tests

```bash
source .venv/bin/activate
export REDIS_URL=redis://localhost:6379/0

make lint          # Ruff
make unit          # unit tests
make integration   # Redis-backed integration tests
make cov           # all non-live tests plus coverage (85% minimum)
make done          # lint, unit, integration, and coverage
make web-test      # web app: Vitest + Testing Library (also run in CI)
```

The web app tests (`frontend/src/**/*.test.ts(x)`) cover job-state and calendar logic, the API
client (auth and workspace headers, 401 handling, error messages, pagination), sign-in, demo
sign-in and sign-up, and switching workspaces. Run them in watch mode with
`cd frontend && npm run test:watch`.

Live tests are separate and may call Gemini:

```bash
make live
```

The test suite's Gemini stand-in is under `tests/` and is not used by the application at runtime.
Integration tests use a unique Redis key prefix per test; do not clear a shared Redis database to
clean up tests.

## Project layout

| Path | Responsibility |
|---|---|
| `frontend/` | React web app; `npm run build` writes `frontend/dist`, which the API serves. |
| `app/api/` | FastAPI routes, authentication, workspaces, schemas, database models, and the media registry. |
| `app/api/reconcile.py` | DB↔Redis reconciler and the shared job-dispatch step. |
| `app/api/retention.py` | Media retention policy. |
| `app/storage.py` | Local and S3-compatible media storage backends. |
| `app/obs/` | Structured logging, correlation ids, tracing (incl. across the queue), metrics. |
| `ops/` | Prometheus scrape config, alert rules and their tests, Grafana provisioning and dashboard. |
| `app/demo.py` | Demo-mode draft writer and demo account seeding. |
| `app/cli.py` | Command-line job submission, status/stats, and worker entry point. |
| `app/jobs.py` | Job records, submission, and deduplication. |
| `app/redis_queue.py`, `app/delayqueue.py` | Reliable Redis queues, delayed retries, and recovery. |
| `app/worker.py` | Queue worker and Gemini job processing. |
| `app/gemini_client.py` | Gemini client and provider error handling. |
| `app/limiter.py` | Redis-backed shared rate limits, daily quota, cooldown, and backoff. |
| `app/media.py` | Media validation, hashing, image preparation, and video compression. |
| `app/system_prompt.py` | Instructions for media analysis and content-draft generation. |
| `migrations/` | Alembic schema history. |
| `docs/JOB_LIFECYCLE.md` | Cross-store job lifecycle, failure matrix, guarantees. |
| `docs/OPERATIONS.md` | Runbook: signals, SLOs, investigating a job, what to do for each alert. |
| `tests/` | Unit, integration, and optional live tests. |

## Deployment notes

Not yet a fully hardened public deployment. Put it behind HTTPS and a reverse proxy with
request-size and authentication rate limits before exposing it publicly, especially with
`ALLOW_SIGNUP=1`, since every new workspace can spend your Gemini quota. Use strong secrets,
secure PostgreSQL/Redis, run at least one `make worker` with `DATABASE_URL` set (it runs the
reconciler and retention), persist the media directory or use S3, back up the database, and
review `MEDIA_RETENTION_DAYS`. Protect `.env` and any uploaded media as sensitive data.
