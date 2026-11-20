# MediaFlow — AI Media Analysis & Content Calendar

MediaFlow is a multi-tenant app (FastAPI backend plus a React web app) for uploading images and
videos, generating social-media content drafts with Google Gemini, reviewing jobs with
role-based workspaces, and planning approved posts on a timezone-aware calendar.

## Try the demo: no Gemini key, nothing to install

[![Open in GitHub Codespaces](https://github.com/codespaces/badge.svg)](https://codespaces.new/TechnoMachiene/mgc_viral_machine?quickstart=1)

**In your browser:** click the button above (you need a free GitHub account). GitHub builds a
private copy, starts the demo, and opens it in a new tab, which takes a few minutes the first
time. Then sign in with one of the accounts below. It runs on your GitHub Codespaces quota
(personal accounts get free hours each month). The demo port is made public so it opens in any
browser without GitHub's port sign-in; the link is long and random, and each Codespace signs
logins with its own key.

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
   **Draft ready**, with Facebook and Instagram drafts written from the photo's real colours
   and shape. Open it, edit anything you like, and press **Send for review**.
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
cp -n .env.example .env              # set GEMINI_API_KEY, GEMINI_MODEL, JWT_SECRET (openssl rand -hex 32),
                                     # and POSTGRES_PASSWORD (openssl rand -hex 24): required
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
Compose to point at its own containers, and `.env` is never copied into the image.
`POSTGRES_PASSWORD` has no default: Compose refuses to start without it, and the app refuses a
weak one for any database that isn't on localhost. Originals live in the `uploads` volume;
for several hosts, use `MEDIA_STORAGE=s3` instead. For a public host, follow
[docs/DEPLOYMENT.md](docs/DEPLOYMENT.md): HTTPS and rate limits (`compose.proxy.yaml`), secrets,
and tested backup and restore scripts.

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

> **Current scope:** This project analyzes media and creates content suggestions. Publishing
> runs behind a provider interface, and the only provider is a **sandbox**: a pretend platform
> inside MediaFlow that exercises consent, tokens, delivery, retries and failures. It does not
> connect to Instagram, Facebook or TikTok yet; a real platform is added only after it passes
> the readiness checklist in [docs/PUBLISHING.md](docs/PUBLISHING.md).

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
  People join by **invitation**: a single-use, expiring link where newcomers choose their own
  password and existing accounts consent by signing in (see [docs/TENANCY.md](docs/TENANCY.md)).
- Keeps an append-only **audit trail** of role and membership changes, invitations, review
  decisions and calendar changes, written in the same transaction as each change, and a
  route-by-route test suite proving no one can reach across a workspace boundary (see
  [docs/AUDIT.md](docs/AUDIT.md)).
- Supports members, managers and admins per workspace, platform admins, and a protected
  super-admin account.
- Stores originals on local disk or any S3-compatible bucket (optionally encrypted), serves them
  through short-lived signed or presigned links, and applies per-workspace deletion policies
  (originals after 30 days by default, after publishing, rejected drafts; drafts kept).
- Lets the uploader check Gemini's draft first: they send it for review as written, or rewrite
  it by hand and then send it. Until then it stays with them, editable and out of the review
  queue.
- Lets managers/admins review drafts they were sent and approve or reject them, with a
  **draft editor**: the uploader and reviewers edit drafts field by field, every save is a
  version (with a field-level diff and one-click restore), concurrent saves can't overwrite
  each other, and review comments thread on a version and a field. A rejection's reason lands
  in the thread. When the uploader edits a decided draft it comes back to them to send again;
  when a reviewer edits it, it goes straight back to review.
- Lets owners schedule completed, approved jobs and view, reschedule, or cancel them in a calendar.
- Reports **insights** per workspace: content volume by day, media and channel, approval and
  first-pass approval rates, where the time goes (queue wait, analysis, review, publishing lag
  at p50/p90), comparison with the previous period, and rule-based findings on what to act on
  (see [docs/ANALYTICS.md](docs/ANALYTICS.md)).
- Publishes calendar entries at their time through a **provider interface** (encrypted
  credentials, retries with backoff, async processing, crash-safe delivery) to a real
  **Facebook Page and Instagram business account** (Meta Graph API, credentials in `.env`), or
  to the sandbox, whose accounts can simulate a failure mode on purpose
  (see [docs/PUBLISHING.md](docs/PUBLISHING.md)).
- **Notifies people** in the app (the bell) and by email when SMTP is set up: drafts to review,
  approvals and rejections with the reason, posts that went live or failed, and invitation
  links. Each person can turn email copies off. A notification, or its email, opens the draft
  it is about.
- **Updates live:** every open screen refreshes by itself when someone else uploads, sends,
  decides, edits, comments, schedules or publishes, or when Gemini finishes a draft. Saved
  changes publish topic names on Redis pub/sub, and browsers listen on one Server-Sent Events
  stream each (`app/api/live.py`). Behind the nginx overlay that stream is unbuffered.
- Treats Gemini's drafts as suggestions: before approving, the reviewer sees what to verify
  (unfilled `[NEEDS: …]` gaps, flagged phrases such as investment promises, Unicode bold text,
  extra hashtags or phone numbers), can't approve while gaps remain, and confirms they checked
  names, prices, dates and places, which the audit trail records.
- Stores schedule instants in UTC and displays them in the requested IANA timezone.
- Ships a responsive web app (`frontend/`) covering upload, review, calendar, team, and queue
  activity.
- Includes automated unit and integration tests that use synthetic media and a scripted Gemini
  client, plus a Vitest + Testing Library suite for the web app; live Gemini tests are separate.
- Runs in a zero-setup demo mode with an offline draft writer.

## What is verified, and how

Precisely what the automated checks exercise, and what they don't:

| Area | Verified by | Not verified |
|---|---|---|
| Queue, worker, retries, crash recovery | Integration tests on real Redis, one per crash point ([docs/JOB_LIFECYCLE.md](docs/JOB_LIFECYCLE.md)) | |
| Gemini | The client's request handling, error mapping and video upload flow, against a stubbed SDK; every pipeline test uses a scripted stand-in | **Real Gemini responses.** One live smoke test (`make live`, one small image) runs only with your key; it is not run in CI. Draft quality is not tested |
| Video | Validation, and the compression decision with ffmpeg stubbed | Real ffmpeg compression of real videos |
| Publishing | The sandbox provider end to end: OAuth with PKCE, delivery, every failure type, crash safety. Meta: contract tests against recorded Graph API replies (photos, videos, Reels, containers, every error class) and end to end through the delivery loop with Meta scripted ([docs/PUBLISHING.md](docs/PUBLISHING.md)) | **Meta's live API**: run one real post by hand before relying on it. TikTok, YouTube, LinkedIn and X are not implemented |
| Notifications | Who is told what, read state, email outbox retries and permanent failures, opt-out, invitation links erased after sending | A real SMTP server |
| Object storage | Local disk; S3 behaviour against `moto` (an emulator): presigned links, encryption, bucket checks | A real AWS, R2 or MinIO bucket |
| Database | SQLite for most tests; PostgreSQL 17 in CI for the full migration chain and the audit trail's trigger | |
| Tenancy and security | A route-by-route cross-tenant sweep, audit-trail and throttle tests | An external penetration test |
| Web app | Component tests (Vitest); **real-browser end-to-end tests** (Playwright, Chromium on desktop and phone) of the main journey and the revision loop, against the real app in demo mode | Firefox and Safari; the UI with real Gemini drafts |
| Deployment | In CI: the demo stack from a clean checkout and its HTTP workflow, a clean one-shot migration, the production stack refusing an empty database password. By hand: the nginx proxy (redirect, headers, rate limits, forwarded address) and a full backup and restore drill | Real certificates and DNS; the drills are not automated in CI |

## What it does not do

- It posts to one Facebook Page and its Instagram account per installation, not to TikTok,
  YouTube, LinkedIn or X, and not to Stories or carousels.
- Email needs an SMTP account you provide; without one, notifications stay in the app.
- It can't tell whether a fact in a draft is true. It points at what to check and makes the
  reviewer confirm; a person still has to read every draft.
- It does not offer billing or per-workspace quotas yet. The Gemini rate limit is shared by the
  whole installation.
- Demo drafts are deterministic templates, not real media understanding.

## Workflow

```text
upload -> validate -> store original -> job row (PostgreSQL outbox) -> Redis queue -> worker
       -> Gemini -> result in Redis + PostgreSQL          (reconciler repairs any gap)
completed job -> manager review -> approved -> owner schedules -> calendar
                      ^       |
                      |   rejected (reason in comments)
                      +-- edit = new version, back to review
```

### The system prompt

Gemini writes with the system prompt in **`prompts/system_prompt.md`**: MGC Developments'
copywriter for its Facebook Page and Instagram account, with brand rules, post types, a fact
sheet (part G) and an output format (part I). Edit that file to change the copy rules or to
update the fact sheet before a campaign, then restart the API and workers (with Compose,
`docker compose restart api worker`; the folder is mounted, so no rebuild). Set
`SYSTEM_PROMPT_FILE` to use a different file.

- Each job keeps the prompt it was queued with, and the prompt is part of the cache key, so an
  edited prompt never reuses drafts written under the old one.
- The prompt replies in labelled sections (`FACEBOOK CAPTION:`, `INSTAGRAM HASHTAGS:`, `NOTES:`
  ...). `app/copy_format.py` turns them into the per-platform draft the editor, review and
  publishing use (Facebook caption; Instagram caption, hashtags, Reel text, cover title, slide
  titles; post type, alt text, first comment and notes). The original reply is stored with it.
- The prompt expects a post brief (part H), but uploads don't carry one yet, so Gemini is told
  to work from the media and the fact sheet and to mark missing facts `[NEEDS: ...]` in the
  draft's notes.
- It writes for Facebook and Instagram only: drafts have no TikTok section.

Drafts are suggestions for people to review; nothing is posted to a real platform.

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
| `POST /invitations` | Manager/admin | Invite an email with a role; returns a single-use link (shown once). Inviting again replaces the old link. |
| `GET /invitations` | Manager/admin | Pending invitations of this workspace. |
| `DELETE /invitations/{id}` | Manager/admin | Withdraw a pending invitation (admin invitations: admins only). |
| `POST /invitations/lookup` | Anyone with the link | Workspace, email, role and expiry of an invitation, and whether the email already has an account. |
| `POST /invitations/accept` | The invitee | New email: send a `password` to create the account. Existing account: call it signed in as that email. |
| `POST /workspaces/{id}/leave` | Member of it | Leave a workspace (not your only one, not as its last admin). Your work stays with it. |
| `PATCH /workspaces/{id}` | Admin of it | Rename the workspace. |
| `GET /publishing/providers` | Authenticated | Enabled publishing providers (today: the sandbox). |
| `GET /publishing/connections` | Authenticated | This workspace's connected accounts (never credentials). |
| `POST /publishing/connections/authorize` | Admin | Start OAuth (code + PKCE); returns the consent URL. |
| `POST /publishing/oauth/callback` | The admin who started it | Finish OAuth with `state` and `code`; stores the account, credentials encrypted. |
| `DELETE /publishing/connections/{id}` | Admin | Disconnect: wipe credentials, cancel pending deliveries. |
| `GET`, `POST /scheduled-posts/{post_id}/publications` | Post owner/admin | List, or add an account to publish an approved calendar entry to at its time. |
| `POST /publications/{id}/retry`, `DELETE /publications/{id}` | Post owner/admin | Retry a failed or uncertain delivery; cancel a pending one. |
| `GET /publishing/sandbox` | Authenticated | The sandbox platform's posts and request log for this workspace. |
| `GET /analytics` | Manager/admin | This workspace's report for `start`..`end` in a `timezone` (default: last 30 days). |
| `GET /analytics/workspaces` | Platform admin | Every workspace's totals, rates and median times, side by side. |
| `GET /audit-events` | Admin | This workspace's audit trail, newest first; filter by `category`, `action`, `actor_id` or `target_id`, page with `before`. |

Managers can create and invite users and managers, but not admins. A workspace always keeps at
least one admin. An admin can change a member's email or password only if that account belongs
to no other workspace, so one tenant can never take over a person shared with another. An
account joins a second workspace only by accepting an invitation while signed in. The
protected super-admin account cannot be modified or deleted through the API. Without
`ALLOW_SIGNUP=1`, people join by invitation or through an account a manager or admin creates.
The full model, role matrix and invitation security properties are in
[docs/TENANCY.md](docs/TENANCY.md).

### Media jobs and review

| Method and path | Access | Purpose |
|---|---|---|
| `POST /jobs` | Authenticated | Upload an image/video as multipart field `file` and enqueue analysis; answers `202` at once. Send `Prefer: wait=N` to wait up to N seconds (at most 300) for the draft (`200` with it if ready). Send an `Idempotency-Key` header (e.g. a UUID) to make retries safe: the same key and file return the original job. |
| `GET /jobs/mine` | Authenticated | List the caller's jobs with processing and approval statuses; supports `limit` and `offset`. |
| `GET /jobs/{job_id}` | Owner/manager/admin | Get a job's current status and result; non-admins can access their own jobs. |
| `DELETE /jobs/{job_id}` | Owner/manager/admin | Cancel an owned job when it has not started processing. |
| `GET /jobs/{job_id}/media` | Owner/manager/admin | Download the protected original upload; `410` (with the reason) once it was deleted. |
| `POST /jobs/{job_id}/media-link` | Owner/manager/admin | An expiring URL for the original (presigned S3, or signed). |
| `DELETE /jobs/{job_id}/media` | Owner/admin | Delete this job's original now; drafts are kept. |
| `GET`, `PUT /workspaces/{id}/retention` | Admin of it | The workspace's deletion policy, with a preview of what the next run deletes. |
| `POST /workspaces/{id}/retention/preview` | Admin of it | What a proposed policy would delete, without saving it. |
| `GET /jobs/review` | Manager/admin | List completed jobs awaiting review. |
| `GET /jobs/approved` | Manager/admin | List completed, approved jobs. |
| `GET /jobs/rejected` | Manager/admin | List completed, rejected jobs. |
| `POST /jobs/{job_id}/submit` | Owner/admin | Send a finished draft (`with_uploader`) to the review queue, as written or after editing it. `409` if there is no draft yet or it was already sent. |
| `PATCH /jobs/{job_id}/approval` | Manager/admin | Approve or reject a draft that was sent for review (`409` while it is still `with_uploader`); an optional `comment` (the reason) is added to the draft's thread. Approving needs `"checked": true` (the reviewer verified the facts; else `422`) and no `[NEEDS: …]` gaps left (`409`). `GET /jobs/{id}` lists them in `checks`. |
| `GET /notifications` | Authenticated | The caller's notifications in this workspace, newest first, and the unread count. |
| `POST /notifications/read` | Authenticated | Mark `ids` (or `all`) read; only the caller's own. |
| `GET`/`PUT /notifications/preferences` | Authenticated | Email copies on or off (`email`), and whether this server sends email at all. |
| `GET /jobs/{job_id}/versions` | Owner/manager/admin | The draft's versions, newest first (1 is Gemini's). |
| `POST /jobs/{job_id}/versions` | Owner/manager/admin | Save `text` (or `restore` an older version) as a new version. `base_version` must be the current one, or `409`. An uploader's save keeps the draft (or takes it back) `with_uploader`; a reviewer's save sends a decided draft back to `pending`. Refused while it is on the calendar. |
| `GET /jobs/{job_id}/comments` | Owner/manager/admin | Review comments, oldest first; replies carry `parent_id`. |
| `POST /jobs/{job_id}/comments` | Owner/manager/admin | Comment on the current version, optionally on one `field` (e.g. `instagram.caption`), or reply with `parent_id`. |
| `PATCH /jobs/{job_id}/comments/{comment_id}` | Owner/manager/admin | Resolve or reopen a thread (`{"resolved": true}`). |
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

Approval is tracked separately: `with_uploader` (drafted; the uploader hasn't sent it for review
yet), `pending` (in the review queue), `approved`, or `rejected`. Only jobs with analysis status
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
| `MEDIA_RETENTION_DAYS=30` (default) | Default policy: a job's original is deleted 30 days after its upload or calendar date. Each workspace can choose its own, within `MEDIA_RETENTION_MAX_DAYS`. `0` keeps originals forever. |

Browsers reach originals only through **expiring links**: presigned S3 URLs, or links the API
signs for local storage (`MEDIA_LINK_TTL_S`, default 5 minutes). Workspace admins set a
**deletion policy** under Settings: how long originals are kept, whether they go once
published, and whether rejected drafts are deleted, with a preview of what a change would
delete. Drafts, approvals and calendar entries are kept, every deletion is audited, and
bytes shared by several workspaces are deleted only when the last one lets go. Check a bucket
is ready (versioning, public access, encryption) with `python -m app.cli storage-check`. Full
details: [docs/STORAGE.md](docs/STORAGE.md).

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
| `MEDIA_RETENTION_DAYS` | Default days a job's original is kept; default 30, `0` keeps forever. Workspaces may choose their own. |
| `MEDIA_RETENTION_MAX_DAYS` | Ceiling on what a workspace may choose; default `0` (no ceiling). |
| `MEDIA_LINK_TTL_S` | Lifetime of an expiring media link; default 300 (30–3600). |
| `S3_PUBLIC_ENDPOINT_URL` | Host for presigned links when browsers reach the store at a different address than the servers. |
| `S3_SSE`, `S3_SSE_KMS_KEY_ID` | Server-side encryption for new uploads: `AES256` or `aws:kms`. |
| `ALLOW_SIGNUP` | Self-serve sign-up that creates a workspace; default off. |
| `PUBLISHING_PROVIDERS` | Enabled publishing providers, comma-separated: `mock` (the sandbox, default) and/or `meta` (Facebook and Instagram); `none` disables. |
| `PUBLISHING_KEY` | Fernet key encrypting publishing credentials; derived from `JWT_SECRET` if unset (set it in production). |
| `PUBLIC_BASE_URL` | The app's public `https://` address: links in emails, and the media URLs Instagram fetches. |
| `META_PAGE_ID`, `META_PAGE_ACCESS_TOKEN` | The Facebook Page and its system-user token; required with `meta` ([docs/PUBLISHING.md](docs/PUBLISHING.md#meta)). |
| `META_INSTAGRAM_ACCOUNT_ID` | The Instagram business account linked to the Page (Instagram posting). |
| `META_APP_SECRET`, `META_GRAPH_VERSION` | Optional: adds `appsecret_proof` to every call; Graph API version (default `v24.0`). |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_SECURITY`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `SMTP_FROM` | Email copies of notifications, sent by the worker. Empty `SMTP_HOST`: in-app only. `SMTP_SECURITY` is `starttls` (587, default), `ssl` (465) or `none`. |
| `API_THREADS`, `DB_POOL_SIZE`, `DB_MAX_OVERFLOW`, `DB_POOL_TIMEOUT` | Capacity per API process (defaults 40, 20, 20, 10 s). See [docs/OPERATIONS.md](docs/OPERATIONS.md#capacity). |
| `PASSWORD_HASH_CONCURRENCY`, `PASSWORD_HASH_WAIT_S` | Password checks at once (default: the CPUs the container may use) and how long a sign-in waits its turn before a 503 "busy" (default 30 s). |
| `API_REPLICAS`, `POSTGRES_MAX_CONNECTIONS` | API containers behind the HTTPS proxy (default 2), and PostgreSQL's connection limit (default 200). |
| `FLAGGED_PHRASES_FILE` | Phrases a reviewer is warned about before approving (default `prompts/flagged_phrases.txt`). |
| `LOGIN_MAX_FAILURES`, `LOGIN_IP_MAX_FAILURES`, `LOGIN_WINDOW_S`, `SIGNUP_IP_MAX` | Sign-in and signup throttling: defaults 10 per account and 50 per address in 15 minutes; 10 signups an hour per address. |
| `FORWARDED_ALLOW_IPS` | Proxies whose `X-Forwarded-For` uvicorn trusts; `compose.proxy.yaml` sets it. |
| `INVITATION_TTL_HOURS` | How long an invitation link stays valid; default 168 (7 days), 1–720. |
| `EMBEDDED_WORKER` | Run a queue worker inside the API process; default off. |
| `DEMO_MODE` | Offline drafts, SQLite, embedded worker and demo accounts; never in production. |
| `GEMINI_API_KEY`, `GEMINI_MODEL` | Gemini credentials and model identifier (not needed in demo mode). |
| `SYSTEM_PROMPT_FILE` | The Gemini system prompt; default `prompts/system_prompt.md`. Read at startup. |
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
make unit          # unit tests (seconds)
make integration   # Redis-backed integration tests
make cov           # all non-live tests once, plus coverage (85% minimum; about 2 minutes)
make done          # lint, unit, and coverage
make web-test      # web app: Vitest + Testing Library (also run in CI)
make e2e           # real browser (Chromium, desktop and phone) against the real app; see below
```

`make e2e` runs Playwright (`frontend/e2e/`) against the actual app in demo mode
(`scripts/e2e_server.py`: real API, queue, embedded worker and database, offline drafts) with a
fresh database and Redis prefix per run, removed afterwards. It covers the main journey (upload,
draft, send for review, approval, schedule, calendar) and the revision loop (edit before sending,
reject with a reason, edit, send back to review). It needs Redis and, once, `cd frontend && npx playwright install chromium`.

Tests use a minimal-cost Argon2 hasher (production keeps the recommended parameters) and
return from `drain()` as soon as the queue is empty, which keeps the whole suite near two
minutes.

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
| `app/api/retention.py` | Per-workspace deletion policies and object collection (docs/STORAGE.md). |
| `app/api/media_links.py` | Expiring signed media links. |
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
| `prompts/system_prompt.md` | The Gemini system prompt: copy rules, fact sheet, output format. |
| `app/system_prompt.py` | Loads it (`SYSTEM_PROMPT_FILE` to override). |
| `app/copy_format.py` | Turns the prompt's labelled reply into the structured draft. |
| `prompts/flagged_phrases.txt`, `app/draft_checks.py` | What a reviewer must verify before approving. |
| `app/publishing/meta.py`, `app/publishing/media.py` | Facebook Page and Instagram publishing (Graph API), and the media they post. |
| `app/api/notifications.py` | In-app notifications, their email copies (outbox) and the API. |
| `scripts/load_test.py` | Burst benchmark (100 at once) against a demo stack. |
| `app/api/live.py`, `frontend/src/live.ts` | Live updates: changes announced on Redis, streamed to browsers. |
| `migrations/` | Alembic schema history. |
| `docs/JOB_LIFECYCLE.md` | Cross-store job lifecycle, failure matrix, guarantees. |
| `docs/OPERATIONS.md` | Runbook: signals, SLOs, investigating a job, what to do for each alert. |
| `tests/` | Unit, integration, and optional live tests. |

## Deployment notes

Follow [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) before exposing MediaFlow on the internet:

- **HTTPS and edge rate limits:** `compose.proxy.yaml` puts nginx in front (TLS, HSTS, per-address
  limits on sign-in and the API), and the API is no longer published. The API also throttles
  sign-in and signup itself.
- **Secrets:** a strong `POSTGRES_PASSWORD` is required; `JWT_SECRET` and `PUBLISHING_KEY` too.
- **Backups:** `scripts/backup.sh` and `scripts/restore.sh` (database and originals), with a
  restore drill that has been run end to end.
- **Operations:** run at least one worker with `DATABASE_URL` set (reconciler, retention,
  publishing), monitor it ([docs/OPERATIONS.md](docs/OPERATIONS.md)), and keep `ALLOW_SIGNUP=0`
  unless strangers may spend your Gemini quota.

Protect `.env`, backups and uploaded media as sensitive data.
