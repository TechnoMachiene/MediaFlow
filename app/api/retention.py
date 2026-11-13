"""Media retention: delete original uploads that nobody needs any more.

Policy (MEDIA_RETENTION_DAYS, default 30; 0 disables):
- An original is purged once these exact bytes have not been uploaded by anyone for
  MEDIA_RETENTION_DAYS, AND no job using them is still queued or processing, AND no scheduled
  (not cancelled) post using them is still in the future.
- Generated drafts, approvals and calendar entries are kept. Only the original file goes; its
  media URL then answers 410 Gone. Uploading the same bytes again restores it.
- Abandoned partial uploads (`.upload-*` files left by a crash mid-request) older than a day
  are removed from local storage.

Race safety: the asset row is locked while it is checked and deleted, and every upload locks
the same row to bump `last_used_at`, so a new upload either refreshes the asset before the
check (and it is skipped) or sees `purged_at` afterwards (and stores the bytes again).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from sqlalchemy import exists, select
from sqlalchemy.orm import Session

from app.api.database import ACTIVE_STATUSES, ApiJob, MediaAsset, ScheduledPost
from app.storage import LocalStorage, MediaStorage

log = logging.getLogger("retention")
STAGED_MAX_AGE_S = 24 * 3600


def prune_media(
    session_factory: Callable[[], Session],
    storage: MediaStorage,
    *,
    retention_days: int,
    batch: int = 200,
    now: datetime | None = None,
) -> dict[str, int]:
    counts = {"purged": 0, "kept": 0, "staged_removed": 0}
    if isinstance(storage, LocalStorage) and storage.directory.is_dir():
        oldest = time.time() - STAGED_MAX_AGE_S
        for leftover in storage.directory.glob(".upload-*"):
            try:
                if leftover.stat().st_mtime < oldest:
                    leftover.unlink()
                    counts["staged_removed"] += 1
            except FileNotFoundError:
                pass
    if retention_days <= 0:
        return counts

    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=retention_days)
    idle = (MediaAsset.purged_at.is_(None), MediaAsset.last_used_at < cutoff)

    def still_needed(sha256: str):
        in_use = exists().where(
            ApiJob.media_sha256 == sha256, ApiJob.status.in_(ACTIVE_STATUSES)
        )
        upcoming = exists().where(
            ScheduledPost.job_id == ApiJob.id,
            ApiJob.media_sha256 == sha256,
            ScheduledPost.status == "scheduled",
            ScheduledPost.scheduled_at >= now,
        )
        return select(in_use | upcoming)

    with session_factory() as db:
        candidates = db.scalars(
            select(MediaAsset.sha256).where(*idle).order_by(MediaAsset.last_used_at).limit(batch)
        ).all()
    for sha256 in candidates:
        with session_factory.begin() as db:
            asset = db.scalar(
                select(MediaAsset)
                .where(MediaAsset.sha256 == sha256, *idle)
                .with_for_update(skip_locked=True)
            )
            if asset is None:
                continue
            if db.scalar(still_needed(sha256)):
                counts["kept"] += 1
                continue
            storage.delete(asset.stored_filename)
            asset.purged_at = now
            counts["purged"] += 1
    if counts["purged"]:
        log.info("retention purged %d original(s) idle since %s", counts["purged"], cutoff)
    return counts
