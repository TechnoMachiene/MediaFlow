# Component card: Media analysis queue (Redis -> Gemini)

**Goal:** User images/videos go through a Redis queue, so Gemini sees fewer, rate-limited, deduplicated calls.
**Depends on:** Redis 6.2+ only. No API, no Postgres, no auth yet.

## Done when
1. Upload validated (type, size, magic bytes) before queueing.
2. Same bytes + prompt + model = ONE Gemini call, many users served.
3. Calls never exceed configured RPM / daily cap, across any number of workers.
4. Worker crash loses no job (lease + reaper).
5. 429 pauses the whole fleet; retries back off; poison jobs dead-letter.
6. Result readable by job id for every submitter.

## Test plan (all automated, `make done`)
| Layer | Cases |
|---|---|
| Unit | media validation, hashing, image resize/EXIF/alpha, config parsing, backoff bounds, Gemini client with stub SDK (error mapping, video upload cleanup) |
| Integration (real Redis) | FIFO, lease/reap/extend/defer/dead-letter, limiter burst + daily cap + cooldown, dedup, worker outcomes, CLI |
| Concurrency | 8 claimers/300 jobs no dup; 20 submitters same file = 1 queued; 20 callers burst 5 = 5 allowed; 30 callers cap 10 = 10 |
| Failure | crash after claim, crash after result saved, 429, 5xx x N, permanent error, unexpected exception |
| E2E | 3 workers keep >= 0.14s gap at 300 RPM; 10 users same image = 1 call; 4 uploads = 2 calls |
| Live (opt-in) | `make live` one tiny image against real Gemini |

## Out of scope (next components)
FastAPI upload endpoint, Postgres durability, auth/roles, per-user quotas, video duration cap, TPM tracking, result storage beyond Redis TTL.

## Known limits
- No lease heartbeat. Job running longer than QUEUE_LEASE_S gets re-run (extra Gemini call, same result).
- Redis default persistence may lose last seconds of jobs on crash. Postgres source of truth comes later.
- Gemini token-per-minute limits not tracked. Only requests per minute/day.
