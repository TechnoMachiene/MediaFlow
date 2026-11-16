"""Content-addressed media registry on top of a MediaStorage backend.

Identical bytes are stored once (keyed by SHA-256), whoever uploads them. Each upload bumps
`last_used_at` under a row lock; retention (app/api/retention.py) takes the same lock before it
deletes anything, so an upload can never be paired with an original that is being purged.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.database import ApiJob, MediaAsset
from app.storage import LocalStorage, MediaStorage


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def store_media_asset(
    db: Session,
    staged_path: Path,
    original_filename: str,
    storage: MediaStorage,
    *,
    sha256: str | None = None,
) -> MediaAsset:
    """Register `staged_path` and return its asset row. The staged file may be consumed.
    Pass `sha256` when the caller already hashed the bytes (e.g. while streaming them)."""
    sha256 = sha256 or _sha256(staged_path)
    now = datetime.now(timezone.utc)

    asset = db.scalar(select(MediaAsset).where(MediaAsset.sha256 == sha256).with_for_update())
    if asset is not None:
        if asset.purged_at is not None or not storage.exists(asset.stored_filename):
            # Retention deleted the original earlier (or the file went missing behind our
            # back); these bytes are here again, so store them again.
            asset.stored_filename = storage.save(staged_path, original_filename, sha256)
            asset.purged_at = None
        asset.last_used_at = now
        db.commit()
        return asset

    key = storage.save(staged_path, original_filename, sha256)
    asset = MediaAsset(sha256=sha256, stored_filename=key, last_used_at=now)
    db.add(asset)
    try:
        db.commit()
    except IntegrityError:
        # A concurrent upload of the same bytes registered first. Keep theirs.
        db.rollback()
        existing = db.scalar(
            select(MediaAsset).where(MediaAsset.sha256 == sha256).with_for_update()
        )
        if existing is None:
            storage.delete(key)
            raise
        if existing.stored_filename != key:
            storage.delete(key)
        existing.last_used_at = now
        db.commit()
        return existing
    except Exception:
        db.rollback()
        storage.delete(key)
        raise
    return asset


@dataclass(frozen=True)
class MediaLocation:
    key: str | None
    purged: bool
    storage_ref: str | None  # what a worker needs (path or s3:// ref)


def locate_job_media(db: Session, job: ApiJob, storage: MediaStorage) -> MediaLocation:
    if job.media_sha256 is not None:
        asset = db.get(MediaAsset, job.media_sha256)
        if asset is not None:
            if asset.purged_at is not None:
                return MediaLocation(asset.stored_filename, True, None)
            return MediaLocation(asset.stored_filename, False, storage.ref(asset.stored_filename))
    # Uploads from before content-addressed storage were saved under the job id.
    if isinstance(storage, LocalStorage):
        key = f"{job.id}{Path(job.filename).suffix.lower()}"
        return MediaLocation(key, False, storage.ref(key))
    return MediaLocation(None, False, None)
