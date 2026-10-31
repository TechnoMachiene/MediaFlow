# Media queue: Redis -> Gemini

Users submit images/videos through a CLI or FastAPI. Jobs wait in Redis. Workers send them to Gemini at a safe pace.
Identical uploads (same bytes + fixed system prompt + model) cost ONE Gemini call.
Real Gemini only. The app has no demo or stand-in mode.

```
submit -> validate -> sha256 -> dedup? --yes--> reuse result / wait for in-flight job
                                  | no
                                  v
                          Redis ready list -> worker -> limiter (RPM + daily cap + cooldown)
                                                   -> Gemini -> result -> all waiting users
```

## Setup (Ubuntu 26.04 on WSL, Python 3.14, Redis 8)
```bash
redis-cli ping                                   # PONG (Redis service already runs)
cd ~/code && unzip /mnt/c/Users/hp/Downloads/media_queue.zip && cd media_queue
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt              # uv alternative: uv venv && uv pip install -r requirements-dev.txt
export REDIS_URL=redis://localhost:6379/0
make done                                        # lint + unit + integration + coverage
```
`make done` never calls Gemini and needs no API key. Tests replace Gemini with a scripted
stand-in from `tests/helpers.py`. Tests use their own key prefix. Never FLUSHDB.
Tested on: Python 3.14.4 + Redis 8.0.5, and Python 3.12 + Redis 7.0.15.

## Configure real Gemini
```bash
cp -n .env.example .env          # -n: never overwrites your existing .env
nano .env                        # fill GEMINI_API_KEY, GEMINI_MODEL, GEMINI_RPM, GEMINI_RPD
grep -cE '^GEMINI_API_KEY=[^ ]' .env     # must print 1 (no space after =)
make live                        # one tiny real call; expect: 1 passed + printed text
```
`.env` rules: no spaces around `=`, no quotes, comments on their own lines.
Get model id and limits from your AI Studio page. Unset limits default to 5/min and 100/day.
Free tier: throwaway media only. Real user or company data needs paid billing.

## Run by hand (real calls)
```bash
python -m app.cli submit uploads/cat.png --user 1
python -m app.cli submit uploads/cat.png --user 2   # waiting (deduped)
python -m app.cli stats
python -m app.cli worker --drain                 # one real Gemini call serves both users
python -m app.cli status <job_id>
```
Long-running worker: `python -m app.cli worker` (Ctrl+C stops). Several workers share one limiter.
Worker logs show Gemini HTTP lines. Normal.

## FastAPI backend

The API is a separate HTTP interface to the same Redis queue. PostgreSQL stores user accounts,
the searchable job index, and a durable copy of completed Gemini output; Redis remains the live
queue/result store. The worker writes each completed result to the corresponding PostgreSQL job
row (including matching deduplicated uploads). Uploaded originals are written to
`MEDIA_STORAGE_DIR`, which must be a persistent directory shared by the API and worker processes.
New media files use their sanitized original filename; byte-identical uploads reuse the existing
stored file. Different content with a colliding filename gets a short SHA-256 suffix, while each
job retains its own `original_filename` in API responses and listings.
Apply `alembic upgrade head` before deploying this storage change; the migration adds a
content-addressed media-asset registry. Previously stored job-ID-named files are left in place for
existing job references and are not automatically deleted.
Apply the database migration before starting the API:

```bash
# Ubuntu: install/start PostgreSQL if quickstart.sh has not already done so.
sudo apt-get install -y postgresql
sudo service postgresql start

# Create a local development database/user (choose your own password).
sudo -u postgres psql
```

In the `psql` prompt:
```sql
CREATE USER media_queue WITH PASSWORD 'replace-with-a-local-password';
CREATE DATABASE media_queue OWNER media_queue;
\q
```

Configure `.env` (never commit it). Set `DATABASE_URL` to use the password you chose and generate
a JWT secret:
```bash
cp -n .env.example .env
openssl rand -hex 32
nano .env
```

The API also needs the existing `GEMINI_API_KEY` and `GEMINI_MODEL` settings. Run migrations, then
start the API and worker in separate terminals:
```bash
source .venv/bin/activate
alembic upgrade head
uvicorn app.api.main:app --reload

# Separate terminal, same project directory and .env:
source .venv/bin/activate
python -m app.cli worker
```
Install ffmpeg on the worker machine for uploads larger than `MAX_VIDEO_MB`:
`sudo apt-get update && sudo apt-get install -y ffmpeg`. The worker logs a warning at startup
when ffmpeg is unavailable. Successful `POST /jobs` responses include the generated prompt output
at `result.text`; failed jobs include an `error` and return HTTP 502.
Apply schema upgrades, including the manager approval-status column, with `alembic upgrade head`
before restarting the API.

Interactive API documentation: `http://127.0.0.1:8000/docs`

Create the first administrator after applying migrations. The password is prompted securely, not
passed in the shell command:
```bash
python -m app.api.admin abdullahtauseef988@gmail.com
```
Choose a new strong password; do not reuse a password pasted into chat. This command creates the
admin or promotes/resets the account if that email already exists.

Endpoints under `/api/v1`:
- `POST /auth/login` — JWT access token
- `POST /users` — managers can create regular users; admins can create any role
- `GET /users`, `PATCH /users/{user_id}/role` — admin only
- `POST /jobs` — authenticated multipart upload (`file`); users, managers, and admins. Waits up to
  five minutes for Gemini and includes `result` when complete; returns `202` with a null result if
  still queued/processing when the wait expires. Keep the worker running.
- `GET /jobs/{job_id}`, `DELETE /jobs/{job_id}` — users/managers access only their own jobs;
  admins can access any job
- `GET /jobs/review`, `GET /jobs/approved`, `GET /jobs/rejected` — managers/admins can list
  completed jobs by approval status; pending review items include Gemini output and a protected
  `media_url` and the submitter's `submitter_email`
- `PATCH /jobs/{job_id}/approval` — managers/admins set a completed job to `approved` or `rejected`
- `GET /jobs/{job_id}/media` — protected original upload; owners, managers, and admins can fetch
  it (manager/admin review listings include the URL)
- `GET /jobs`, `GET /stats` — admin only
- `GET /health/live`, `GET /health/ready` — process liveness and PostgreSQL/Redis readiness

Example request flow:
```bash
TOKEN=$(curl -fsS http://127.0.0.1:8000/api/v1/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"email":"you@example.com","password":"your-existing-password"}' | python3 -c \
  'import json,sys; print(json.load(sys.stdin)["access_token"])')

curl -fsS http://127.0.0.1:8000/api/v1/jobs \
  -H "Authorization: Bearer $TOKEN" \
  -F 'file=@uploads/cat.png'
```
The POST response includes the generated `result` when the worker finishes within five minutes.
If it remains queued after five minutes, the response has a null `result`; fetch
`GET /api/v1/jobs/{job_id}` to check later. Send JWTs with the `Authorization: Bearer <token>`
header. These are real Gemini jobs, so the worker requires valid credentials and may incur API
usage.

This is a self-hosted starter backend, not a fully hardened public deployment: registration is
restricted to managers/admins, access tokens are not revocable before expiry, and upload files
are retained in the storage directory. Put it behind HTTPS and a reverse proxy with request-size
and auth rate limits before public exposure; define and implement a media-retention policy for
your deployment.

## Files
| File | Job |
|---|---|
| `app/config.py` | env settings, validated; key + model required |
| `app/media.py` | validate, hash original bytes, shrink images |
| `app/redis_queue.py` | reliable queue: lease, reap, defer, dead-letter |
| `app/delayqueue.py` | atomic ZSET->LIST promotion |
| `app/limiter.py` | RPM bucket + daily cap + fleet cooldown + backoff |
| `app/jobs.py` | job records, submit, dedup |
| `app/gemini_client.py` | GeminiClient + error mapping |
| `app/worker.py` | claim -> limit -> call -> store -> ack |
| `app/cli.py` | submit, worker, status, stats |
| `app/api/` | FastAPI routes, JWT auth, PostgreSQL user/job index |
| `migrations/` | Alembic schema migrations |
| `tests/helpers.py` | scripted Gemini stand-in, TESTS ONLY |
| `tests/` | unit, integration, live |

## Job states
`queued` -> `processing` -> `done`
`waiting` (same upload already running) -> `done`
`retrying` (429/5xx, back in line after backoff) | `failed` (bad input) | `dead` (retries used up)

## Settings (env)
See `.env.example`. Key ones: `REDIS_URL`, `GEMINI_API_KEY`, `GEMINI_MODEL`, `GEMINI_RPM`, `GEMINI_RPD`, `QUEUE_LEASE_S` (must exceed slowest video), `QUEUE_MAX_ATTEMPTS`, `MAX_IMAGE_MB`, `MAX_VIDEO_MB`, `MAX_VIDEO_UPLOAD_MB`, `IMAGE_MAX_SIDE`, `VIDEO_FPS`. Large videos are accepted up to `MAX_VIDEO_UPLOAD_MB` (default 1024 MB) and compressed on the worker to fit `MAX_VIDEO_MB` (default 100 MB), using ffmpeg. Install it on every worker with `sudo apt install ffmpeg`.

## Next component (after you tick docs/DEFINITION_OF_DONE.md)
Postgres schema + migrations, then FastAPI upload endpoint. See `docs/COMPONENT_CARD_media_queue.md`.
