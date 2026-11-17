from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from app.config import env_flag
from app.errors import ConfigError

DEMO_JWT_SECRET = "demo-mode-only-secret-" + "x" * 32

# Passwords nobody should be allowed to deploy with (docs/DEPLOYMENT.md).
WEAK_DB_PASSWORDS = {"", "mediaflow", "postgres", "password", "replace-me", "changeme",
                     "change-me", "admin", "secret", "root", "123456", "12345678", "test"}
MIN_DB_PASSWORD = 16
LOCAL_DB_HOSTS = {"", "localhost", "127.0.0.1", "::1"}


def check_database_credentials(database_url: str) -> None:
    """A database reached over a network needs a strong password. Local development
    (localhost or a Unix socket) and SQLite are exempt."""
    from sqlalchemy.engine import make_url

    url = make_url(database_url)
    if url.get_backend_name() == "sqlite" or (url.host or "") in LOCAL_DB_HOSTS:
        return
    password = url.password or ""
    if password.lower() in WEAK_DB_PASSWORDS or len(password) < MIN_DB_PASSWORD:
        raise ConfigError(
            f"DATABASE_URL uses a weak password for {url.host}: use at least "
            f"{MIN_DB_PASSWORD} random characters (openssl rand -hex 24) and set the same "
            "value in POSTGRES_PASSWORD. See docs/DEPLOYMENT.md."
        )


def publishing_env() -> tuple[tuple[str, ...], str]:
    """(enabled providers, PUBLISHING_KEY). Shared by the API and the worker, which must agree.
    PUBLISHING_PROVIDERS is a comma list; it defaults to the sandbox, and "none" disables."""
    raw = os.environ.get("PUBLISHING_PROVIDERS", "mock").strip().lower()
    names = () if raw in ("", "none") else tuple(n.strip() for n in raw.split(",") if n.strip())
    return names, os.environ.get("PUBLISHING_KEY", "").strip()


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
    # Host browsers use for presigned links, when it differs from S3_ENDPOINT_URL (e.g. MinIO
    # reached as http://minio:9000 inside Compose but https://media.example.com outside).
    s3_public_endpoint_url: str = ""
    # Server-side encryption for new uploads: "" (bucket default), "AES256" or "aws:kms".
    s3_sse: str = ""
    s3_sse_kms_key_id: str = ""
    # Default number of days a workspace keeps originals (drafts are kept). 0 keeps them
    # forever. Workspaces may choose their own value (docs/STORAGE.md).
    media_retention_days: int = 30
    # Operator ceiling on what a workspace may choose; 0 means no ceiling.
    media_retention_max_days: int = 0
    # Lifetime of an expiring media link (presigned or signed URL).
    media_link_ttl_s: int = 300
    # Sign-in and signup throttling (app/api/throttle.py); the proxy limits at the edge too.
    login_max_failures: int = 10
    login_ip_max_failures: int = 50
    login_window_s: int = 900
    signup_ip_max: int = 10
    # Self-serve sign-up: anyone can create an account plus their own workspace.
    allow_signup: bool = False
    # How long an invitation link stays valid.
    invitation_ttl_hours: int = 168
    # Publishing providers this installation enables (see docs/PUBLISHING.md), and the key that
    # encrypts their credentials at rest (derived from jwt_secret when empty).
    publishing_providers: tuple[str, ...] = ("mock",)
    publishing_key: str = ""
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
        if not demo:
            check_database_credentials(database_url)

        jwt_secret = os.environ.get("JWT_SECRET", "").strip()
        if not jwt_secret and demo:
            jwt_secret = DEMO_JWT_SECRET
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
        sse = os.environ.get("S3_SSE", "").strip()
        if sse not in ("", "AES256", "aws:kms"):
            raise ConfigError("S3_SSE must be empty, 'AES256' or 'aws:kms'.")
        retention = _int("MEDIA_RETENTION_DAYS", 30, 0, 36500)
        ceiling = _int("MEDIA_RETENTION_MAX_DAYS", 0, 0, 36500)
        if ceiling and not 1 <= retention <= ceiling:
            raise ConfigError(
                "MEDIA_RETENTION_DAYS must be between 1 and MEDIA_RETENTION_MAX_DAYS."
            )
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
            s3_public_endpoint_url=os.environ.get("S3_PUBLIC_ENDPOINT_URL", "").strip(),
            s3_sse=sse,
            s3_sse_kms_key_id=os.environ.get("S3_SSE_KMS_KEY_ID", "").strip(),
            media_retention_days=retention,
            media_retention_max_days=ceiling,
            media_link_ttl_s=_int("MEDIA_LINK_TTL_S", 300, 30, 3600),
            login_max_failures=_int("LOGIN_MAX_FAILURES", 10, 3, 1000),
            login_ip_max_failures=_int("LOGIN_IP_MAX_FAILURES", 50, 3, 100000),
            login_window_s=_int("LOGIN_WINDOW_S", 900, 60, 86400),
            signup_ip_max=_int("SIGNUP_IP_MAX", 10, 1, 100000),
            allow_signup=env_flag("ALLOW_SIGNUP", default=demo),
            invitation_ttl_hours=_int("INVITATION_TTL_HOURS", 168, 1, 720),
            publishing_providers=publishing_env()[0],
            publishing_key=publishing_env()[1],
            embedded_worker=env_flag("EMBEDDED_WORKER", default=demo),
            demo_mode=demo,
        )

    def build_cipher(self):
        from app.publishing.crypto import Cipher

        return Cipher(publishing_key=self.publishing_key, jwt_secret=self.jwt_secret)

    def build_storage(self):
        from app.storage import build_storage

        return build_storage(
            self.media_storage,
            local_dir=self.media_storage_dir,
            s3_bucket=self.s3_bucket,
            s3_prefix=self.s3_prefix,
            s3_endpoint_url=self.s3_endpoint_url,
            s3_region=self.s3_region,
            s3_public_endpoint_url=self.s3_public_endpoint_url,
            s3_sse=self.s3_sse,
            s3_sse_kms_key_id=self.s3_sse_kms_key_id,
        )
