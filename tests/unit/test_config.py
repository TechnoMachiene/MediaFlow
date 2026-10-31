import pytest

from app.config import Settings
from app.errors import ConfigError

pytestmark = pytest.mark.unit

ENV = ["GEMINI_API_KEY", "GEMINI_MODEL", "GEMINI_RPM", "GEMINI_RPD", "GEMINI_BURST",
       "QUEUE_LEASE_S", "VIDEO_FPS", "IMAGE_MAX_SIDE", "REDIS_URL", "QUEUE_PREFIX",
       "MAX_VIDEO_MB", "MAX_VIDEO_UPLOAD_MB"]


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for k in ENV:
        monkeypatch.delenv(k, raising=False)


@pytest.fixture
def creds(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    monkeypatch.setenv("GEMINI_MODEL", "some-model")


def test_requires_key_and_model(monkeypatch):
    with pytest.raises(ConfigError, match="GEMINI_API_KEY"):
        Settings.from_env()
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    with pytest.raises(ConfigError, match="GEMINI_MODEL"):
        Settings.from_env()
    monkeypatch.setenv("GEMINI_MODEL", "some-model")
    s = Settings.from_env()
    assert s.model == "some-model" and s.api_key == "k"


def test_blank_key_counts_as_missing(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "   ")
    monkeypatch.setenv("GEMINI_MODEL", "m")
    with pytest.raises(ConfigError, match="GEMINI_API_KEY"):
        Settings.from_env()


def test_safe_low_defaults(creds):
    s = Settings.from_env()
    assert (s.rpm, s.rpd, s.burst) == (5, 100, 1)
    assert s.redis_url.startswith("redis://") and s.prefix == "ma"


def test_numbers_validated(creds, monkeypatch):
    monkeypatch.setenv("GEMINI_RPM", "fast")
    with pytest.raises(ConfigError, match="GEMINI_RPM"):
        Settings.from_env()
    monkeypatch.setenv("GEMINI_RPM", "0")
    with pytest.raises(ConfigError, match=">= 1"):
        Settings.from_env()
    monkeypatch.setenv("GEMINI_RPM", "12")
    monkeypatch.setenv("GEMINI_RPD", "450")
    monkeypatch.setenv("VIDEO_FPS", "0.5")
    s = Settings.from_env()
    assert (s.rpm, s.rpd, s.video_fps) == (12, 450, 0.5)


def test_blank_value_uses_default(creds, monkeypatch):
    monkeypatch.setenv("GEMINI_RPM", "  ")
    assert Settings.from_env().rpm == 5


def test_video_upload_and_processing_limits(creds, monkeypatch):
    settings = Settings.from_env()
    assert settings.max_video_mb == 100
    assert settings.max_video_upload_mb == 1024
    monkeypatch.setenv("MAX_VIDEO_MB", "50")
    monkeypatch.setenv("MAX_VIDEO_UPLOAD_MB", "2048")
    settings = Settings.from_env()
    assert (settings.max_video_mb, settings.max_video_upload_mb) == (50, 2048)
