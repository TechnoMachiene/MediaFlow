import dataclasses
import os
import uuid

import pytest
import redis
from PIL import Image

from app.config import Settings
from app.wiring import build_runtime
from tests.helpers import ScriptedClient


@pytest.fixture(autouse=True)
def no_dotenv(monkeypatch):
    """Tests must not depend on a developer's local .env file."""
    monkeypatch.setattr("app.cli.load_dotenv", lambda *a, **k: False)
    monkeypatch.setattr("app.api.main.load_dotenv", lambda *a, **k: False)


def base_settings(**over) -> Settings:
    s = Settings(
        redis_url=os.environ.get("REDIS_URL", "redis://localhost:6379/0"),
        prefix="t" + uuid.uuid4().hex[:8], api_key="test-key", model="test-model",
        rpm=6000, rpd=100000, burst=100, lease_s=900, max_attempts=3, result_ttl_s=3600,
        job_ttl_s=3600, inflight_ttl_s=3600, max_image_mb=10, max_video_mb=100,
        max_video_upload_mb=1024,
        image_max_side=1024, video_fps=1.0,
    )
    return dataclasses.replace(s, **over)


@pytest.fixture
def redis_client():
    url = os.environ.get("REDIS_URL")
    if not url:
        pytest.skip("REDIS_URL not set")
    r = redis.Redis.from_url(url)
    try:
        r.ping()
    except redis.exceptions.ConnectionError:
        pytest.fail(f"Redis not reachable at {url}. Start it: sudo service redis-server start")
    return r


@pytest.fixture
def make_rt(redis_client):
    """Runtime factory. Own key prefix per test; cleanup touches only that prefix."""
    prefixes: list[str] = []

    def _make(client=None, worker_kw=None, **over):
        s = base_settings(**over)
        prefixes.append(s.prefix)
        return build_runtime(s, client=client or ScriptedClient(), r=redis_client,
                             **(worker_kw or {}))

    yield _make
    for p in prefixes:
        for k in redis_client.scan_iter(match=f"{p}:*", count=500):
            redis_client.delete(k)


@pytest.fixture
def make_image(tmp_path):
    counter = {"n": 0}

    def _make(name=None, color=None, size=(64, 48), fmt="PNG"):
        counter["n"] += 1
        n = counter["n"]
        color = color or (n % 256, (n * 7) % 256, (n * 13) % 256)
        path = tmp_path / (name or f"img{n}.{fmt.lower()}")
        Image.new("RGB", size, color).save(path, fmt)
        return str(path)

    return _make


@pytest.fixture
def make_video(tmp_path):
    counter = {"n": 0}

    def _make(name=None, ext="mp4"):
        counter["n"] += 1
        path = tmp_path / (name or f"vid{counter['n']}.{ext}")
        body = b"\x00\x00\x00\x18ftypmp42" + os.urandom(256)
        if ext == "webm":
            body = b"\x1a\x45\xdf\xa3" + os.urandom(256)
        path.write_bytes(body)
        return str(path)

    return _make
