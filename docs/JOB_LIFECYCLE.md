# Job lifecycle across PostgreSQL and Redis

A job lives in two stores that cannot share a transaction:

| Store | Owns | Survives |
|---|---|---|
| **PostgreSQL** `api_jobs` | That the job exists, who owns it, which workspace it belongs to, its final status, error and result, approval and schedule. | Everything. It is the record of truth. |
| **Redis** `ma:*` | Where the job is *right now*: ready list, processing leases, retry schedule, dead letters, dedup and result cache, rate limits. | Usually. It can lose recent writes (failover, `appendfsync everysec`), keys expire by TTL, and someone can flush it. |

This document lists every step that writes to both stores, how each step can fail, and what
repairs it. Each row of the failure matrix names the test that injects that failure.

## The happy path

```mermaid
sequenceDiagram
    participant C as Client
    participant A as API
    participant P as PostgreSQL
    participant R as Redis
    participant W as Worker
    participant G as Gemini
    C->>A: POST /jobs (file, Idempotency-Key)
    A->>P: SELECT api_jobs by (workspace, owner, key): a retry? answer with that job
    A->>P: INSERT media_assets (lock row, bump last_used_at)
    A->>P: INSERT api_jobs (status=queued, enqueued_at=NULL, key)  [1 durable intent]
    A->>R: submit: job hash + marker + dedup + LPUSH, one Lua script  [2 dispatch]
    A->>P: UPDATE enqueued_at=now, cache_key
    W->>R: claim (RPOP ready + ZADD processing lease), atomic
    W-->>P: status=processing (best effort)
    W->>G: analyse
    W->>R: result cache + job status=done, atomic
    W->>P: result + status=done  [3 must succeed before ack]
    W->>R: ack (ZREM processing)
```

`api_jobs` is an **outbox**: the row is committed *before* Redis hears about the job, and
`enqueued_at` records that the hand-off happened. Anything committed but not handed off is
found and dispatched later. Nothing the API accepted can be silently lost.

The commit in step 1 is the moment the job is accepted. If step 2 fails (Redis down, its reply
lost, or the `enqueued_at` write fails), the API does **not** undo the row: Redis may in fact
have the job, and deleting the row would leave it running with no record. It answers
`202 queued` instead, and the job converges through the reconciler or the client's retry.

## Submitting a job: idempotency

`POST /jobs` takes an optional `Idempotency-Key` header (1–255 visible ASCII characters; the
web app sends a fresh UUID per file and reuses it on retry).

| Request | Answer |
|---|---|
| New key | A new job, as usual. |
| Same key, same file (bytes and filename) | The **same job**: its current status and result, with `Idempotent-Replayed: true`. HTTP status follows the job (`200` done, `202` active or cancelled, `502` failed/dead). |
| Same key, different file | `422`, nothing created. |
| No key | A new job every time (dedup still avoids a second Gemini call for identical bytes). |

How it holds under failure:

- **The key lives on the row it created** (`api_jobs.idempotency_key`), under a unique index on
  `(workspace_id, owner_id, idempotency_key)`. The key and the job are committed together, so
  there is no window where one exists without the other, and no separate key store to expire
  or lose. Keys are kept for the job's lifetime. They are scoped per user per workspace, so one
  user's key can never reveal or return another user's job.
- **Concurrent retries:** both requests may miss the lookup, but only one insert can pass the
  unique index. The loser catches the conflict and answers with the winner's job.
- **Retry of a job that never reached Redis** (`enqueued_at IS NULL`): the replay dispatches it
  at once rather than waiting for the reconciler's grace period. Dispatch is atomic and
  idempotent per job id, so racing the reconciler or the original request is harmless.
- **Dispatch is one Lua script** (`SUBMIT_LUA` in `app/jobs.py`): the job hash, the per-job
  `submitted` marker, dedup and the `LPUSH` either all happen or none do. A marker is only
  trusted when the job hash also exists, so partial Redis loss re-enqueues the job instead of
  reporting it as already submitted.

## States

| Status | Where it is set | Meaning |
|---|---|---|
| `queued` | API (row insert), Redis on submit | Waiting for a worker. |
| `waiting` | Redis on submit | Identical media in the same workspace is already in flight; this job reuses its result. |
| `processing` | worker | A worker holds the lease. |
| `retrying` | worker | Transient error or 429; scheduled in `deferred` with backoff. |
| `done` | worker / reconciler | Result stored in both stores. Final. |
| `failed` | worker / reconciler | Not retryable (bad media, blocked content, original gone, lost too often). |
| `dead` | worker | Exhausted `QUEUE_MAX_ATTEMPTS`. Id kept in `ma:dead` for inspection. |
| `cancelled` | API | Cancelled before a worker claimed it. Final. |

### Allowed transitions

These are the rules for `api_jobs.status`, enforced by `app/api/lifecycle.py`. Every write
goes through them: the worker hooks apply them inside the `UPDATE ... WHERE status IN (...)`,
so a late write loses the race safely, and the API and reconciler use `set_status`.
`tests/unit/test_job_transitions.py` checks that this table matches the code.

<!-- transitions:start -->
| From | May become |
|---|---|
| `queued` | `waiting`, `processing`, `retrying`, `done`, `failed`, `dead`, `cancelled` |
| `waiting` | `queued`, `processing`, `retrying`, `done`, `failed`, `dead`, `cancelled` |
| `processing` | `queued`, `waiting`, `retrying`, `done`, `failed`, `dead`, `cancelled` |
| `retrying` | `queued`, `waiting`, `processing`, `done`, `failed`, `dead`, `cancelled` |
| `failed` | `done` |
| `dead` | `done` |
| `done` | (final) |
| `cancelled` | (final) |
<!-- transitions:end -->

Why the active states are so permissive: the status hooks are best effort, so the row can skip
states (`queued` straight to `dead` if `processing` was never recorded), and the reaper or
reconciler legitimately moves a job back to `queued`. What matters is the right-hand side:

- **`done` and `cancelled` are final.** Nothing reopens them.
- **`failed` and `dead` accept only `done`.** Delivery is at-least-once, so a redelivered attempt
  can finish after the first one gave up. A real result is kept, but a stale `processing` or
  `retrying` from a worker whose lease expired can never bring a failed job back to life.
- **`unknown` is never written.** It means Redis forgot the job; the row keeps its last
  status, so it stays active and the reconciler recovers it.

The normal path through Redis:

```mermaid
stateDiagram-v2
    [*] --> queued: POST /jobs commits the row
    queued --> waiting: identical media in flight
    queued --> done: result cache hit
    queued --> processing: worker claims
    queued --> cancelled: DELETE /jobs/{id}
    waiting --> done: leader finished
    waiting --> failed: leader failed
    processing --> done
    processing --> retrying: transient error / 429
    processing --> failed: permanent error
    processing --> dead: attempts exhausted
    processing --> queued: lease expired (reaper)
    retrying --> processing
    retrying --> cancelled
    failed --> done: redelivered attempt finished
    dead --> done: redelivered attempt finished
    done --> [*]
    cancelled --> [*]
```

## Who keeps the stores in agreement

1. **The worker's hooks** (`app/api/persistence.py`, wired in `app/api/worker_setup.py`):
   - `on_result` writes the result. It **must** succeed before the job is acked. If PostgreSQL
     is down, the job is re-deferred for one second and the next claim replays the result from
     the Redis cache. Gemini is **not** called again.
   - `on_status` writes `processing` / `retrying` / `failed` / `dead` on a best-effort basis.
     A failure is logged and left to the reconciler.
2. **The reconciler** (`app/api/reconcile.py`) runs every 30 s inside every worker. A Redis lock
   makes sure only one sweep runs at a time, and row locks (`FOR UPDATE SKIP LOCKED`) guard
   each row. It does two sweeps:
   - **Dispatch:** rows with `enqueued_at IS NULL` older than a 60 s grace period are submitted.
   - **Sync:** active rows not updated for 60 s are compared with Redis and repaired, as
     described below.
3. **The reaper** (`ReliableQueue.reap`) puts expired processing leases back on `ready`, so a
   worker that crashed mid-job never strands it.

Run a sweep by hand with `python -m app.cli reconcile`.

## Failure matrix

| # | Failure | State it leaves | Repair | Test (`tests/integration/test_lifecycle.py`) |
|---|---|---|---|---|
| 1 | API dies after committing the row, before Redis | Row `queued`, `enqueued_at NULL`, nothing in Redis | Reconciler dispatch sweep | `test_row_committed_but_never_dispatched_is_dispatched_by_reconciler` |
| 2 | Redis unreachable during upload | Submit raises; row `enqueued_at NULL` | Row kept, client gets `202 queued`; reconciler (or the client's keyed retry) dispatches once Redis is back. Cancelling still works | `test_redis_down_during_upload_keeps_the_job_in_the_outbox`, `test_job_accepted_while_redis_was_down_can_be_cancelled` |
| 3 | Redis accepted the job, but the API never learned it (reply lost, or the `enqueued_at` commit failed) | Row `enqueued_at NULL`, job already queued | Re-dispatch is a no-op: the submit script sees the job's `submitted` marker and hash, so the job is never enqueued twice | `test_redis_reply_lost_after_it_accepted_the_job_never_double_enqueues`, `test_enqueued_at_commit_fails_after_redis_accepted`, `test_dispatch_is_idempotent_so_a_retried_dispatch_never_double_enqueues` |
| 4 | Worker crashes holding a lease | Id in `processing`, lease expires | Reaper puts it back on `ready` | `test_worker_crash_mid_job_is_reaped_and_finished` |
| 5 | PostgreSQL down when the result is written | Redis `done`, row not `done` | Job re-deferred; replayed from the result cache; acked only after the DB write | `test_result_write_failure_is_retried_without_a_second_gemini_call` |
| 6 | Status hook fails | Row status stale | Ignored by the worker; reconciler sync copies the Redis status | `test_status_hook_failure_never_blocks_processing`, `test_terminal_state_missed_by_the_hooks_is_copied_by_reconciler` |
| 7 | Result never reached the DB at all (hook absent or failed) | Row has no result | Reconciler sync copies the result from Redis | `test_result_missed_by_the_db_is_copied_by_reconciler` |
| 8 | Redis loses the job hash (flush, failover, TTL) | Row active, Redis `unknown` | Reconciler re-submits with `force=True` and increments `recoveries` | `test_job_forgotten_by_redis_is_resubmitted` |
| 9 | Redis loses only the queue entry | Hash says `queued`, id in no structure | Reconciler `requeue_if_lost`: one Lua script checks all four structures and pushes only if the id is in none | `test_queue_entry_lost_while_job_hash_survived_is_requeued` |
| 10 | Redis keeps losing the job | Repeated case 8 | After `max_recoveries` (3) the row becomes `failed` with a clear error, so it never loops forever | `test_recovery_is_bounded` |
| 11 | Original purged or missing at re-dispatch | No media to send | Row becomes `failed` ("upload it again") instead of retrying | `test_job_whose_original_is_gone_fails_cleanly_instead_of_looping` |
| 12 | Several workers reconcile at once | Duplicate sweeps | Redis lock elects one runner per sweep; per-row idempotency covers the rest | `test_only_one_reconciler_runs_a_sweep_at_a_time`, `test_concurrent_reconcilers_dispatch_each_job_once` |
| 13 | Redis kept the `submitted` marker but lost the job hash | Marker says "already submitted", nothing to run | The marker counts only together with the hash, so re-dispatch enqueues it. Before this, re-dispatch raised on every sweep, stranding the job and aborting the rest of the sweep | `test_submitted_marker_without_job_hash_is_dispatched_not_stranded` |
| 14 | Cancel reached Redis, but the API died before its commit | Redis `cancelled`, row active | Reconciler sync copies `cancelled` | `test_cancel_reached_redis_but_the_db_write_was_lost` |
| 15 | A worker whose lease expired reports late (`processing`, `retrying`, …) | Hook would overwrite a final status | Transition guard in the `UPDATE` ignores it; only a real result may replace `failed`/`dead` | `test_late_status_hooks_cannot_move_a_job_out_of_a_final_state` |
| 16 | Client never saw the response and retries | Possible duplicate job | Same `Idempotency-Key` → the same job, whatever state the first attempt reached: done, active, never dispatched, or Redis still down | `tests/integration/test_idempotency.py` |
| 17 | Two retries with one key race | Both miss the lookup | The unique index lets one insert win; the other answers with its job | `test_concurrent_requests_with_one_key_create_one_job` |

Failures that are not cross-store, such as Gemini errors, rate limits and dedup, are covered by
`tests/integration/test_worker.py`, `test_e2e.py` and `test_jobs.py`.

## Guarantees and their limits

- **No accepted job is lost.** Once the `api_jobs` row commits (so certainly once `POST /jobs`
  returns a `job_id`), the job reaches a final status in PostgreSQL, provided at least one
  worker with `DATABASE_URL` set is running. Redis being down delays a job; it doesn't refuse
  or lose it.
- **One job per idempotency key.** A retried request never creates a second job, whichever
  crash point the first attempt stopped at.
- **At-least-once processing, mostly-once Gemini calls.** Cases 8 and 9 can hand a job to a
  worker a second time. If the first attempt finished, the second is answered from the result
  cache. Gemini is called twice only if Redis lost the result cache *and* the first attempt
  had already run, which requires real Redis data loss.
- **Bounded staleness.** Without the hooks, PostgreSQL catches up within about one reconcile
  interval plus the grace period (≈90 s by default).
- **Dedup and caching are per workspace.** The cache key is prefixed with the workspace id, so
  tenants never share Gemini results, and a `"cached": true` answer cannot reveal that another
  tenant uploaded the same file.

## Media retention

Originals are deleted by `app/api/retention.py` when **all** of these hold:

- These bytes have not been uploaded by anyone for `MEDIA_RETENTION_DAYS` (default 30; `0`
  disables purging).
- No job using them is still active.
- No scheduled (not cancelled) post using them is still in the future.

Drafts, approvals and calendar entries are kept. After a purge, the media URL answers
**410 Gone**, and uploading the same bytes again restores the original. Upload and purge both
lock the asset row, so an upload can never be paired with an original that is being deleted.
The pruner runs hourly from every worker, or by hand with `python -m app.cli prune-media`.
It also removes `.upload-*` partial files that are more than a day old.

Tests: `tests/integration/test_retention_and_storage.py`.
