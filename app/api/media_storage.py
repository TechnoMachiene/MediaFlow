from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.database import ApiJob, MediaAsset


def _safe_filename(original_filename: str) -> str:
    name = Path(original_filename).name
    name = re.sub(r"[^A-Za-z0-9._ -]", "_", name).strip(" .")
    return name[:255] or "upload"


def _same_content(path: Path, sha256: str) -> bool:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest() == sha256


def _reserve_filename(directory: Path, original_filename: str, sha256: str) -> tuple[Path, bool]:
    original = _safe_filename(original_filename)
    stem, suffix = os.path.splitext(original)
    max_stem = 255 - len(suffix) - 65
    collision_stem = f"{stem[:max_stem]}-{sha256}"
    candidates = [original, f"{stem[:max_stem]}-{sha256[:8]}{suffix}", f"{collision_stem}{suffix}"]
    for candidate_name in candidates:
        candidate = directory / candidate_name
        try:
            candidate.touch(exist_ok=False)
            return candidate, True
        except FileExistsError:
            if _same_content(candidate, sha256):
                return candidate, False
    raise FileExistsError(f"Unable to reserve a unique media filename for {original!r}")


def store_media_asset(
    db: Session,
    staged_path: Path,
    original_filename: str,
    storage_dir: Path,
) -> str:
    digest = hashlib.sha256()
    with staged_path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    sha256 = digest.hexdigest()

    existing = db.get(MediaAsset, sha256)
    if existing is not None:
        staged_path.unlink()
        return sha256

    target, created = _reserve_filename(storage_dir, original_filename, sha256)
    if created:
        os.replace(staged_path, target)
    else:
        staged_path.unlink()

    asset = MediaAsset(sha256=sha256, stored_filename=target.name)
    db.add(asset)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = db.get(MediaAsset, sha256)
        if existing is not None:
            if target.name != existing.stored_filename and _same_content(target, sha256):
                target.unlink()
            return sha256
        if created and target.exists():
            target.unlink()
        raise
    except Exception:
        db.rollback()
        if created and target.exists():
            target.unlink()
        raise
    return sha256


def resolve_media_path(db: Session, job_id: str, original_filename: str, storage_dir: Path) -> Path:
    job = db.get(ApiJob, job_id)
    if job is not None and job.media_sha256 is not None:
        asset = db.get(MediaAsset, job.media_sha256)
        if asset is not None:
            return storage_dir / asset.stored_filename

    # Older uploads were stored by job ID; keep existing job media accessible.
    suffix = Path(original_filename).suffix.lower()
    return storage_dir / f"{job_id}{suffix}"
