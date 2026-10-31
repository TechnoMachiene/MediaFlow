"""All settings come from environment variables. No secrets in code."""
from __future__ import annotations

import os
from dataclasses import dataclass

from app.errors import ConfigError


def _num(name: str, default, cast, minimum=None):
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = cast(raw)
    except ValueError:
        raise ConfigError(f"{name} must be {cast.__name__}, got {raw!r}") from None
    if minimum is not None and value < minimum:
        raise ConfigError(f"{name} must be >= {minimum}, got {value}")
    return value


@dataclass(frozen=True)
class Settings:
    redis_url: str
    prefix: str
    api_key: str
    model: str
    rpm: int
    rpd: int
    burst: int
    lease_s: int
    max_attempts: int
    result_ttl_s: int
    job_ttl_s: int
    inflight_ttl_s: int
    max_image_mb: int
    max_video_mb: int
    max_video_upload_mb: int
    image_max_side: int
    video_fps: float

    @classmethod
    def from_env(cls) -> Settings:
        api_key = os.environ.get("GEMINI_API_KEY", "").strip()
        model = os.environ.get("GEMINI_MODEL", "").strip()
        if not api_key:
            raise ConfigError("GEMINI_API_KEY missing. Put it in .env (see .env.example).")
        if not model:
            raise ConfigError("GEMINI_MODEL missing. Copy exact model id from AI Studio.")
        return cls(
            redis_url=os.environ.get("REDIS_URL", "redis://localhost:6379/0"),
            prefix=os.environ.get("QUEUE_PREFIX", "ma"),
            api_key=api_key,
            model=model,
            # Defaults are deliberately LOW. Replace with real numbers from AI Studio.
            rpm=_num("GEMINI_RPM", 5, int, 1),
            rpd=_num("GEMINI_RPD", 100, int, 1),
            burst=_num("GEMINI_BURST", 1, int, 1),
            lease_s=_num("QUEUE_LEASE_S", 900, int, 5),
            max_attempts=_num("QUEUE_MAX_ATTEMPTS", 5, int, 1),
            result_ttl_s=_num("RESULT_TTL_S", 7 * 86400, int, 60),
            job_ttl_s=_num("JOB_TTL_S", 8 * 86400, int, 60),
            inflight_ttl_s=_num("INFLIGHT_TTL_S", 2 * 86400, int, 60),
            max_image_mb=_num("MAX_IMAGE_MB", 10, int, 1),
            max_video_mb=_num("MAX_VIDEO_MB", 100, int, 1),
            max_video_upload_mb=_num("MAX_VIDEO_UPLOAD_MB", 1024, int, 1),
            image_max_side=_num("IMAGE_MAX_SIDE", 1024, int, 64),
            video_fps=_num("VIDEO_FPS", 1.0, float, 0.01),
        )
