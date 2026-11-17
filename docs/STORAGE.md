# Media storage, access links and deletion

Uploaded photos and videos (the *originals*) are the most sensitive data MediaFlow holds. This
document covers where they live, how browsers reach them, and when they are deleted.
Generated drafts, approvals, calendar entries and the audit trail are separate and are never
deleted by these rules (except rejected drafts, if a workspace asks; see below).

## Where originals live

| `MEDIA_STORAGE` | Where | Notes |
|---|---|---|
| `local` (default) | `MEDIA_STORAGE_DIR` | API and workers must share the directory. |
| `s3` | `S3_BUCKET` (+ `S3_PREFIX`) | Any S3-compatible store: AWS S3, Cloudflare R2, MinIO (`S3_ENDPOINT_URL`). No shared disk; workers download a temporary copy per job. |

Objects are **content-addressed** (`<sha256>.<ext>`): identical bytes are stored once, even
across workspaces, and a retried upload is a harmless overwrite. S3 uploads set
`Content-Type`, and `S3_SSE` (`AES256` or `aws:kms`, with `S3_SSE_KMS_KEY_ID`) asks for
server-side encryption on top of any bucket default.

## Expiring access links

Browsers never get a permanent media URL. `POST /api/v1/jobs/{id}/media-link` (the uploader
or a reviewer, the same rule as viewing the media) returns `{url, expires_at, kind}`, valid for
`MEDIA_LINK_TTL_S` seconds (default 300, range 30–3600):

| Backend | Link | Revocation |
|---|---|---|
| S3 | A **presigned GET** (SigV4). Bytes go straight from the bucket with Range support; the bucket stays private. `S3_PUBLIC_ENDPOINT_URL` sets the host browsers use when it differs from the one the servers use (e.g. MinIO inside Compose). | Can't be revoked early, so the lifetime is short. Deleting the object ends it at once. |
| Local | `/api/v1/media/<token>`: HMAC-SHA256 over job, workspace, object and expiry, with a key derived from `JWT_SECRET` (HKDF, its own label). Served with Range support. | **Immediate**: every request re-checks the database, so a deleted original stops working even through links already handed out. |

Responses are `inline`, `Cache-Control: private` (until expiry), `Referrer-Policy: no-referrer`
and `nosniff`. The token travels in the URL path, so it is kept out of telemetry: the access log
records only the route template, the uvicorn access log is off, and `/api/v1/media/` is excluded
from tracing.

The web app loads `<img>` and `<video>` straight from these links: video streams and seeks
instead of downloading whole. It reuses a link until 30 seconds before expiry, and a video
whose link expires mid-play fetches a new one and resumes where it was.
`GET /api/v1/jobs/{id}/media` still serves bytes through the API for scripts using the bearer
token.

## Deleting originals

### Two levels, because bytes are shared

Two workspaces that upload the same file share one stored object, so one tenant's deletion
must not reach the other's copy:

1. **Per job** (`api_jobs.media_deleted_at`): a policy or a person deletes *this job's*
   original. From that moment its media and all its links answer `410 Gone` with the reason,
   even if another job, perhaps in another tenant, still has the same bytes.
2. **Per object** (`media_assets.purged_at`): the stored bytes are deleted once **no** job
   anywhere still has them. Uploading the same file again stores it again, for the new job only.
   Old jobs keep their deletion.

### Each workspace's policy

Admins set it under **Settings**, or `PUT /api/v1/workspaces/{id}/retention`:

| Setting | Meaning | Default |
|---|---|---|
| `originals_days` | Delete a job's original this many days after its upload, or after its calendar date if that is later. `0` keeps originals forever; `null` follows the installation default. | `MEDIA_RETENTION_DAYS` (30) |
| `delete_after_publish` | Delete the original as soon as every publication of its post succeeded (the platforms have their copy). | off |
| `rejected_days` | Delete rejected jobs **entirely** (draft, versions, comments) this many days after the decision. `null` keeps them. | keep |

Whatever the policy, an original is never deleted while its job is still being analyzed, its
calendar entry is still ahead, or a publication of it is still on its way.

**Operator ceiling:** `MEDIA_RETENTION_MAX_DAYS` (default `0`, no ceiling) caps what a
workspace may choose. With a ceiling, "forever" and longer periods are refused, and a value
stored before the ceiling existed is capped.

**Preview before saving:** `GET .../retention` includes what the next run would delete under
the saved policy, and `POST .../retention/preview` with a proposed policy answers what it
*would* delete, without saving or deleting anything. The Settings page shows this as you edit.

**Deleting by hand:** `DELETE /api/v1/jobs/{id}/media` (the uploader or an admin) deletes one
job's original now. The stored bytes go immediately unless they were uploaded in the last
minute, or another job still has them.

### When it runs

Every worker applies all policies hourly (a Redis lock isn't needed: row locks with
`SKIP LOCKED` keep concurrent workers apart). Run it once with `python -m app.cli prune-media`,
or count without deleting with `--dry-run`. Abandoned partial uploads more than a day old are
removed too.

### Accountability

Every deletion is in the audit trail: `media.deleted` (reason `policy`, `published` or
`manual`; the actor is the person, or `system:retention`), `job.purged` for rejected drafts,
and `workspace.retention_changed` for policy changes, with before and after values.
`mediaflow_media_deletions_total{reason}` counts job-level deletions, and
`mediaflow_retention_purged_total` counts stored objects removed.

### Race safety

An upload locks the object's row and bumps `last_used_at` before its job row exists. Objects
are never collected within an hour of their last upload (one minute right after a manual
deletion), which covers that gap, so an upload is never paired with bytes being deleted.

## Is the bucket ready? `storage-check`

A deletion policy is only as good as the storage under it.
`python -m app.cli storage-check` (no Gemini or Redis settings needed) prints findings and
exits 1 on errors:

| Check | Why it matters |
|---|---|
| `bucket` | Reachable with these credentials. |
| `versioning` | With versioning on, deleted objects stay recoverable as old versions. The check passes only if a lifecycle rule expires noncurrent versions. |
| `public_access` | Media must be reachable only through signed links: all four public-access blocks should be on. |
| `encryption` | New uploads are encrypted at rest (`S3_SSE` or a bucket default). |

A minimal AWS setup that passes: a private bucket with Block Public Access on, default
encryption (SSE-S3 or SSE-KMS), versioning off (or a lifecycle rule with
`NoncurrentVersionExpiration`), and an IAM policy limited to `s3:GetObject`, `s3:PutObject`,
`s3:DeleteObject` and `s3:ListBucket` on that bucket and prefix. Browsers fetching presigned
links for `<img>`/`<video>` need no CORS configuration.

## Tests

`tests/integration/test_retention_and_storage.py` covers each rule above: per-workspace
policies, bytes shared across tenants, after-publish and rejected-draft deletion, what is kept
and why, the ceiling, previews, manual deletion, signed links (Range, expiry, tampering,
forgery, immediate revocation), presigned links, encryption and the readiness checks.
`tests/integration/test_tenant_isolation.py` proves the new routes can't reach another
workspace.
