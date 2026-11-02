from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from app.errors import ConfigError


@dataclass(frozen=True)
class ApiSettings:
    database_url: str
    jwt_secret: str
    token_ttl_minutes: int
    media_storage_dir: Path

    @classmethod
    def from_env(cls) -> ApiSettings:
        database_url = os.environ.get("DATABASE_URL", "").strip()
        if not database_url:
            raise ConfigError("DATABASE_URL missing; configure PostgreSQL in .env.")
        if not database_url.startswith("postgresql+psycopg://"):
            raise ConfigError("DATABASE_URL must use postgresql+psycopg://.")

        jwt_secret = os.environ.get("JWT_SECRET", "").strip()
        if len(jwt_secret.encode()) < 32:
            raise ConfigError("JWT_SECRET must be at least 32 bytes; generate one with openssl.")

        try:
            token_ttl_minutes = int(os.environ.get("ACCESS_TOKEN_TTL_MINUTES", "30"))
        except ValueError:
            raise ConfigError("ACCESS_TOKEN_TTL_MINUTES must be an integer.") from None
        if not 1 <= token_ttl_minutes <= 1440:
            raise ConfigError("ACCESS_TOKEN_TTL_MINUTES must be between 1 and 1440.")

        storage_dir = Path(os.environ.get("MEDIA_STORAGE_DIR", "uploads")).expanduser()
        return cls(
            database_url=database_url,
            jwt_secret=jwt_secret,
            token_ttl_minutes=token_ttl_minutes,
            media_storage_dir=storage_dir.resolve(),
        )
