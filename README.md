# MediaFlow — AI Media Analysis & Content Calendar

MediaFlow is a self-hosted backend for uploading images and videos, generating social-media
content drafts with Google Gemini, reviewing jobs with role-based accounts, and planning approved
posts on a timezone-aware calendar.

> **Current scope:** This project analyzes media and creates content suggestions. It does not
> connect to Instagram, Facebook, or TikTok, and it does not automatically publish posts. The
> calendar is for planning and tracking only.

## What it does

- Accepts images and videos through a command-line interface or an authenticated FastAPI endpoint.
- Validates media type, file contents, and configured size limits.
- Uses Gemini to analyze the media and draft platform-tailored captions and content suggestions.
- Processes work asynchronously through a Redis queue, with status checks and retries.
- Deduplicates identical media/model/prompt jobs to avoid repeated Gemini calls where possible.
- Stores user/job records and durable completed results in PostgreSQL.
- Supports users, managers, admins, and a protected super-admin account.
- Lets managers/admins review completed work and approve or reject it.
- Lets owners schedule completed, approved jobs and view, reschedule, or cancel them in a calendar.
- Stores schedule instants in UTC and displays them in the requested IANA timezone.
- Includes automated unit and integration tests that use synthetic media and a scripted Gemini
  client; live Gemini tests are separate.

## What it does not do

- It has no web frontend; use the API and its interactive docs at `/docs`.
- It does not connect social accounts, send posts to platform APIs, or publish automatically when
  a calendar date arrives.
- It does not provide in-app notifications for review results or scheduled dates.
- It does not run in a demo mode: real job processing requires valid Gemini credentials. Automated
  tests use a test-only scripted client instead.

## Workflow

```text
upload -> validate -> hash and deduplicate -> Redis queue -> worker -> Gemini
                                                         -> persist result
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
- Pytest and Ruff for testing and linting

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

The admin command prompts for a password securely. The designated email is protected as the
super-admin account. Running the command again for an existing account resets its password and
revokes its existing tokens.

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

The API and worker must use the same Redis queue, PostgreSQL database, and persistent
`MEDIA_STORAGE_DIR`. FFmpeg is needed on worker machines to compress videos that exceed the
Gemini processing size limit. Uploaded originals are retained in media storage; set and enforce a
retention policy appropriate for your deployment.

Interactive API documentation: <http://127.0.0.1:8000/docs>

## API reference

All application endpoints below use the `/api/v1` prefix unless noted otherwise. Authenticated
endpoints expect `Authorization: Bearer <access_token>`.

### Authentication and account management

| Method and path | Access | Purpose |
|---|---|---|
| `POST /auth/login` | Public | Log in and receive a bearer token. |
| `POST /users` | Manager/admin | Create a user or manager; admins can also create admins. |
| `GET /users`, `GET /users/{user_id}` | Admin | List users or get one user. |
| `PATCH /users/{user_id}` | Admin | Update supported account fields. |
| `PATCH /users/{user_id}/role` | Admin | Change a user's role. |
| `DELETE /users/{user_id}` | Admin | Delete a user, subject to active-job safeguards. |

Managers can create users and managers, but cannot create admins. Admins can manage accounts;
the protected super-admin account cannot be modified or deleted through the API. Account creation
is restricted to authorized managers/admins—there is no public registration endpoint.

### Media jobs and review

| Method and path | Access | Purpose |
|---|---|---|
| `POST /jobs` | Authenticated | Upload an image/video as multipart field `file` and enqueue analysis. |
| `GET /jobs/mine` | Authenticated | List the caller's jobs with processing and approval statuses; supports `limit` and `offset`. |
| `GET /jobs/{job_id}` | Owner/manager/admin | Get a job's current status and result; non-admins can access their own jobs. |
| `DELETE /jobs/{job_id}` | Owner/manager/admin | Cancel an owned job when it has not started processing. |
| `GET /jobs/{job_id}/media` | Owner/manager/admin | Download the protected original upload. |
| `GET /jobs/review` | Manager/admin | List completed jobs awaiting review. |
| `GET /jobs/approved` | Manager/admin | List completed, approved jobs. |
| `GET /jobs/rejected` | Manager/admin | List completed, rejected jobs. |
| `PATCH /jobs/{job_id}/approval` | Manager/admin | Approve or reject a completed job. |
| `GET /jobs` | Admin | Search/list jobs, with pagination and optional media-kind filter. |
| `GET /stats` | Admin | Read queue depths and today's Gemini call count. |

Review items include the submitter's email and protected media URL. Once a job is approved or
rejected, it no longer appears in the pending review list. Upload responses include the result
when processing finishes during the response wait; otherwise the API returns `202 Accepted` and
the job can be checked later.

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

## Configuration

See [.env.example](./.env.example). Common settings:

| Variable | Purpose |
|---|---|
| `REDIS_URL` | Redis connection used by the queue and limiter. |
| `QUEUE_PREFIX` | Namespace for this application's Redis keys. |
| `DATABASE_URL` | PostgreSQL SQLAlchemy connection string. |
| `JWT_SECRET` | Secret used to sign access tokens; at least 32 bytes. |
| `ACCESS_TOKEN_TTL_MINUTES` | JWT lifetime; defaults to 30 minutes. |
| `MEDIA_STORAGE_DIR` | Persistent upload storage directory; defaults to `uploads`. |
| `GEMINI_API_KEY`, `GEMINI_MODEL` | Required Gemini credentials and model identifier. |
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
```

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
| `app/api/` | FastAPI routes, authentication, schemas, database models, and media storage. |
| `app/cli.py` | Command-line job submission, status/stats, and worker entry point. |
| `app/jobs.py` | Job records, submission, and deduplication. |
| `app/redis_queue.py`, `app/delayqueue.py` | Reliable Redis queues, delayed retries, and recovery. |
| `app/worker.py` | Queue worker and Gemini job processing. |
| `app/gemini_client.py` | Gemini client and provider error handling. |
| `app/limiter.py` | Redis-backed shared rate limits, daily quota, cooldown, and backoff. |
| `app/media.py` | Media validation, hashing, image preparation, and video compression. |
| `app/system_prompt.py` | Instructions for media analysis and content-draft generation. |
| `migrations/` | Alembic schema history. |
| `tests/` | Unit, integration, and optional live tests. |

## Deployment notes

This is a self-hosted starter backend, not a fully hardened public deployment. Put it behind
HTTPS and a reverse proxy with request-size and authentication rate limits before exposing it
publicly. Use strong secrets, secure PostgreSQL/Redis, persist the media directory, back up the
database, and define a media-retention policy. Protect `.env` and any uploaded media as sensitive
data.
