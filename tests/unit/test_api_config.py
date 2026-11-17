import pytest

from app.api.config import ApiSettings
from app.errors import ConfigError

pytestmark = pytest.mark.unit


def test_api_config_requires_postgres_url_and_long_jwt_secret(monkeypatch, tmp_path):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("JWT_SECRET", raising=False)
    with pytest.raises(ConfigError, match="DATABASE_URL"):
        ApiSettings.from_env()

    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://user:pass@localhost/db")
    monkeypatch.setenv("JWT_SECRET", "x" * 31)
    with pytest.raises(ConfigError, match="32 bytes"):
        ApiSettings.from_env()

    monkeypatch.setenv("JWT_SECRET", "x" * 32)
    monkeypatch.setenv("ACCESS_TOKEN_TTL_MINUTES", "1441")
    with pytest.raises(ConfigError, match="between 1 and 1440"):
        ApiSettings.from_env()

    monkeypatch.setenv("ACCESS_TOKEN_TTL_MINUTES", "45")
    monkeypatch.setenv("MEDIA_STORAGE_DIR", str(tmp_path / "assets"))
    cfg = ApiSettings.from_env()
    assert cfg.token_ttl_minutes == 45
    assert cfg.media_storage_dir == tmp_path / "assets"


def test_api_config_rejects_non_postgres_database(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "sqlite:///local.db")
    monkeypatch.setenv("JWT_SECRET", "x" * 32)
    with pytest.raises(ConfigError, match="postgresql\\+psycopg"):
        ApiSettings.from_env()


@pytest.mark.parametrize("url", [
    "postgresql+psycopg://mediaflow:mediaflow@postgres:5432/mediaflow",  # the old default
    "postgresql+psycopg://app:short-but-real@db.internal/app",
    "postgresql+psycopg://app@db.internal/app",  # no password at all
    "postgresql+psycopg://app:CHANGEME@db.internal/app",
])
def test_a_networked_database_needs_a_strong_password(url):
    from app.api.config import check_database_credentials

    with pytest.raises(ConfigError, match="weak password"):
        check_database_credentials(url)


@pytest.mark.parametrize("url", [
    "postgresql+psycopg://app:3f9c1a7e5b2d8c4f6a0e9b1d@db.internal/app",
    "postgresql+psycopg://media_queue:replace-me@localhost:5432/media_queue",  # local dev
    "postgresql+psycopg://app:x@127.0.0.1/app",
    "postgresql+psycopg:///app",  # Unix socket
    "sqlite:///demo.sqlite3",
])
def test_strong_or_local_databases_are_accepted(url):
    from app.api.config import check_database_credentials

    check_database_credentials(url)


def test_settings_refuse_a_weak_networked_database(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://mediaflow:mediaflow@postgres/mf")
    monkeypatch.setenv("JWT_SECRET", "s" * 40)
    monkeypatch.delenv("DEMO_MODE", raising=False)
    with pytest.raises(ConfigError, match="weak password"):
        ApiSettings.from_env()
