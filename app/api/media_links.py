"""Expiring media links: how a browser fetches an original without the bearer token.

`POST /jobs/{id}/media-link` (same access rule as the media itself) returns a URL that works
for MEDIA_LINK_TTL_S seconds (default 5 minutes):

* **S3:** a presigned GET. Bytes go straight from the bucket, with Range support, and the
  bucket can stay private. A presigned URL can't be revoked before it expires, which is why
  the lifetime is short; deleting the object ends it at once.
* **Local storage:** `/api/v1/media/<token>`, an HMAC-SHA256 signed token naming one job, one
  object and an expiry. Each request re-checks the database, so deleting an original (by
  policy or by hand) ends every outstanding link immediately, not at expiry.

The token travels in the path. The app's access log records only the route template, the
uvicorn access log is off, and the route is excluded from tracing, so tokens are not
written anywhere; responses also send `Referrer-Policy: no-referrer`.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.api.database import ApiJob, MediaAsset
from app.api.dependencies import DbSession
from app.media import ALLOWED

router = APIRouter(prefix="/api/v1")

EXPIRED = "This media link expired. Open the job again to get a new one."
DELETED = {
    "policy": "The original was deleted by this workspace's retention policy; the drafts are kept",
    "published": "The original was deleted after publishing, as this workspace's policy asks",
    "manual": "The original was deleted on request; the drafts are kept",
}
GONE = "The original was deleted; the drafts are kept"


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


@dataclass(frozen=True)
class LinkClaim:
    job_id: str
    workspace_id: str
    key: str
    expires: int


class MediaLinkSigner:
    def __init__(self, jwt_secret: str):
        self._key = HKDF(algorithm=hashes.SHA256(), length=32, salt=None,
                         info=b"mediaflow media links v1").derive(jwt_secret.encode())

    def _sig(self, body: str) -> str:
        return _b64(hmac.new(self._key, body.encode(), hashlib.sha256).digest())

    def sign(self, claim: LinkClaim) -> str:
        body = _b64(json.dumps({"j": claim.job_id, "w": claim.workspace_id, "k": claim.key,
                                "e": claim.expires}, separators=(",", ":")).encode())
        return f"{body}.{self._sig(body)}"

    def verify(self, token: str) -> LinkClaim:
        body, _, sig = token.partition(".")
        if not body or not hmac.compare_digest(sig.encode(), self._sig(body).encode()):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Invalid media link")
        data = json.loads(_unb64(body))
        claim = LinkClaim(data["j"], data["w"], data["k"], int(data["e"]))
        if claim.expires <= time.time():
            raise HTTPException(status.HTTP_410_GONE, EXPIRED)
        return claim


def deleted_detail(job: ApiJob) -> str:
    return DELETED.get(job.media_deleted_reason or "", GONE)


def issue(request: Request, job: ApiJob, key: str) -> tuple[str, datetime, str]:
    """(url, expires_at, kind) for one job's original."""
    cfg = request.app.state.api_settings
    ttl = cfg.media_link_ttl_s
    expires = int(time.time()) + ttl
    content_type = ALLOWED[Path(job.filename).suffix.lower()][1]
    url = request.app.state.storage.link(key, filename=job.filename, content_type=content_type,
                                         ttl_s=ttl)
    kind = "presigned"
    if url is None:
        token = request.app.state.media_signer.sign(
            LinkClaim(job.id, job.workspace_id, key, expires))
        url, kind = f"/api/v1/media/{token}", "signed"
    return url, datetime.fromtimestamp(expires, timezone.utc), kind


def _current_key(db: Session, job: ApiJob) -> str | None:
    if job.media_sha256 is None:  # legacy uploads stored under the job id
        return f"{job.id}{Path(job.filename).suffix.lower()}"
    asset = db.get(MediaAsset, job.media_sha256)
    if asset is None or asset.purged_at is not None:
        return None
    return asset.stored_filename


@router.get("/media/{token}", include_in_schema=False)
def serve_signed_media(token: str, request: Request, db: DbSession):
    claim = request.app.state.media_signer.verify(token)
    job = db.get(ApiJob, claim.job_id)
    if job is None or job.workspace_id != claim.workspace_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Media not found")
    if job.media_deleted_at is not None:
        raise HTTPException(status.HTTP_410_GONE, deleted_detail(job))
    if _current_key(db, job) != claim.key:  # purged or replaced since the link was issued
        raise HTTPException(status.HTTP_410_GONE, GONE)
    storage = request.app.state.storage
    path = storage.local_path(claim.key)
    if path is None or not path.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Media not found")
    remaining = max(0, claim.expires - int(time.time()))
    return FileResponse(  # Starlette answers Range requests, so video can seek
        path,
        media_type=ALLOWED[Path(job.filename).suffix.lower()][1],
        content_disposition_type="inline",
        filename=job.filename,
        headers={
            "Cache-Control": f"private, max-age={remaining}",
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
        },
    )
