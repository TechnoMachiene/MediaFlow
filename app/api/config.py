from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from app.config import env_flag
from app.errors import ConfigError


def _int(name: str, default: int, low: int, high: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError(f"{name} must be an integer.") from None
    if not low <= value <= high:
        raise ConfigError(f"{name} must be between {low} and {high}.")
    return value


@dataclass(frozen=True)
class ApiSettings:
    database_url: str
    jwt_secret: str
    token_ttl_minutes: int
    media_storage_dir: Path
    # local | s3 (see app/storage.py)
    media_storage: str = "local"
    s3_bucket: str = ""
    s3_prefix: str = ""
    s3_endpoint_url: str = ""
    s3_region: str = ""
    # Originals idle this long are purged (drafts are kept). 0 keeps originals forever.
    media_retention_days: int = 30
    # Self-serve sign-up: anyone can create an account plus their own workspace.
    allow_signup: bool = False
    # How long an invitation link stays valid.
    invitation_ttl_hours: int = 168
    # Run a queue worker inside the API process (handy for development and demos).
    embedded_worker: bool = False
    demo_mode: bool = False

    @classmethod
    def from_env(cls) -> ApiSettings:
        demo = env_flag("DEMO_MODE")
        database_url = os.environ.get("DATABASE_URL", "").strip()
        if not database_url and demo:
            database_url = "sqlite:///demo.sqlite3"
        if not database_url:
            raise ConfigError("DATABASE_URL missing; configure PostgreSQL in .env.")
        if database_url.startswith("sqlite") and not demo:
            raise ConfigError(
                "DATABASE_URL must use postgresql+psycopg:// (SQLite needs DEMO_MODE=1)."
            )
        if not database_url.startswith(("postgresql+psycopg://", "sqlite")):
            raise ConfigError("DATABASE_URL must use postgresql+psycopg://.")

        jwt_secret = os.environ.get("JWT_SECRET", "").strip()
        if not jwt_secret and demo:
            jwt_secret = "demo-mode-only-secret-" + "x" * 32
        if len(jwt_secret.encode()) < 32:
            raise ConfigError("JWT_SECRET must be at least 32 bytes; generate one with openssl.")

        token_ttl_minutes = _int("ACCESS_TOKEN_TTL_MINUTES", 30, 1, 1440)
        storage_dir = Path(os.environ.get("MEDIA_STORAGE_DIR", "uploads")).expanduser()
        storage = os.environ.get("MEDIA_STORAGE", "local").strip().lower() or "local"
        if storage not in ("local", "s3"):
            raise ConfigError("MEDIA_STORAGE must be 'local' or 's3'.")
        bucket = os.environ.get("S3_BUCKET", "").strip()
        if storage == "s3" and not bucket:
            raise ConfigError("MEDIA_STORAGE=s3 needs S3_BUCKET.")
        return cls(
            database_url=database_url,
            jwt_secret=jwt_secret,
            token_ttl_minutes=token_ttl_minutes,
            media_storage_dir=storage_dir.resolve(),
            media_storage=storage,
            s3_bucket=bucket,
            s3_prefix=os.environ.get("S3_PREFIX", "").strip(),
            s3_endpoint_url=os.environ.get("S3_ENDPOINT_URL", "").strip(),
            s3_region=os.environ.get("S3_REGION", "").strip(),
            media_retention_days=_int("MEDIA_RETENTION_DAYS", 30, 0, 36500),
            allow_signup=env_flag("ALLOW_SIGNUP", default=demo),
            invitation_ttl_hours=_int("INVITATION_TTL_HOURS", 168, 1, 720),
            embedded_worker=env_flag("EMBEDDED_WORKER", default=demo),
            demo_mode=demo,
        )

    def build_storage(self):
        from app.storage import build_storage

        return build_storage(
            self.media_storage,
            local_dir=self.media_storage_dir,
            s3_bucket=self.s3_bucket,
            s3_prefix=self.s3_prefix,
            s3_endpoint_url=self.s3_endpoint_url,
            s3_region=self.s3_region,
        )
