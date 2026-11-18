"""The photo or video a provider posts, resolved by the delivery loop for one publication.

Facebook takes the bytes (`open`); Instagram fetches a URL itself (`public_url`), so the
worker signs a short-lived link to `/api/v1/publish-media/<token>` under PUBLIC_BASE_URL.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.api.database import ApiJob
from app.api.media_links import LinkClaim, MediaLinkSigner
from app.api.media_storage import locate_job_media
from app.publishing.base import ContentRejected
from app.storage import MediaStorage

# Long enough for Instagram to fetch a large Reel after accepting the container.
PUBLISH_LINK_TTL_S = 2 * 3600


def public_base_url() -> str:
    """The address the internet reaches this app on (no trailing slash), or ""."""
    return os.environ.get("PUBLIC_BASE_URL", "").strip().rstrip("/")


@dataclass
class JobMedia:
    storage: MediaStorage
    signer: MediaLinkSigner | None
    base_url: str
    job_id: str
    workspace_id: str
    key: str
    ref: str

    def open(self):
        """A context manager yielding a local path (downloaded first for S3)."""
        return self.storage.materialize(self.ref)

    def public_url(self) -> str:
        if not self.base_url.startswith("https://") or self.signer is None:
            raise ContentRejected(
                "Instagram fetches the media itself: set PUBLIC_BASE_URL to this app's public "
                "https:// address and restart.")
        token = self.signer.sign(LinkClaim(self.job_id, self.workspace_id, self.key,
                                           int(time.time()) + PUBLISH_LINK_TTL_S))
        return f"{self.base_url}/api/v1/publish-media/{token}"


class MediaResolver:
    def __init__(self, storage: MediaStorage, signer: MediaLinkSigner | None,
                 base_url: str | None = None):
        self.storage, self.signer = storage, signer
        self.base_url = public_base_url() if base_url is None else base_url

    def for_job(self, db: Session, job: ApiJob) -> JobMedia | None:
        where = locate_job_media(db, job, self.storage)
        if where.purged or where.key is None or where.storage_ref is None:
            return None
        return JobMedia(self.storage, self.signer, self.base_url, job.id, job.workspace_id,
                        where.key, where.storage_ref)
