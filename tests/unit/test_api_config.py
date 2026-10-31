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
