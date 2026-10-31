import json
import threading
import uuid

import pytest

from app.cli import main
from tests.helpers import ScriptedClient

pytestmark = pytest.mark.integration


def run_workers(rt, n):
    ts = [threading.Thread(target=rt.worker.drain) for _ in range(n)]
    [t.start() for t in ts]
    [t.join(timeout=60) for t in ts]


def test_rate_is_respected_across_workers(make_rt, make_image):
    fc = ScriptedClient()
    rt = make_rt(client=fc, rpm=300, burst=1, worker_kw=dict(max_inline_wait_s=10.0))
    ids = [rt.store.submit(i, make_image(), "d")["job_id"] for i in range(10)]
    run_workers(rt, 3)
    assert all(rt.store.status(i)["status"] == "done" for i in ids)
    assert len(fc.calls) == 10 and rt.limiter.used_today() == 10
    times = sorted(t for t, _ in fc.calls)
    gaps = [b - a for a, b in zip(times, times[1:], strict=False)]
    assert min(gaps) >= 0.2 * 0.7, f"calls too close together: {min(gaps):.3f}s"  # 300 rpm = 0.2s


def test_ten_users_same_image_one_call(make_rt, make_image):
    fc = ScriptedClient()
    rt = make_rt(client=fc)
    p = make_image()
    ids = [rt.store.submit(u, p, "d")["job_id"] for u in range(10)]
    run_workers(rt, 2)
    texts = {rt.store.status(i)["result"]["text"] for i in ids}
    assert len(fc.calls) == 1 and len(texts) == 1
    assert all(rt.store.status(i)["status"] == "done" for i in ids)


def test_mixed_duplicates(make_rt, make_image):
    fc = ScriptedClient()
    rt = make_rt(client=fc)
    uniq = [make_image() for _ in range(5)]
    for p in uniq + uniq:
        rt.store.submit(1, p, "d")
    run_workers(rt, 3)
    assert len(fc.calls) == 5


def test_stats_show_gemini_savings(make_rt, make_image):
    rt = make_rt(client=ScriptedClient())
    p = make_image()
    for u in range(4):
        rt.store.submit(u, p, "d")
    run_workers(rt, 1)
    assert rt.limiter.used_today() == 1  # 4 uploads, 1 API call


@pytest.fixture
def cli_env(monkeypatch, tmp_path, redis_client):
    """CLI runs the real wiring, but Gemini is replaced so no API call is ever made."""
    prefix = "tcli" + uuid.uuid4().hex[:8]
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("GEMINI_MODEL", "test-model")
    monkeypatch.setenv("QUEUE_PREFIX", prefix)
    monkeypatch.setenv("GEMINI_RPM", "600")
    monkeypatch.setenv("GEMINI_RPD", "1000")
    monkeypatch.setenv("GEMINI_BURST", "10")
    monkeypatch.setattr("app.wiring.build_client", lambda settings: ScriptedClient())
    yield prefix
    for k in redis_client.scan_iter(match=f"{prefix}:*", count=500):
        redis_client.delete(k)


def test_cli_roundtrip(cli_env, make_image, capsys):
    p = make_image()
    assert main(["submit", p, "--user", "7"]) == 0
    sub = json.loads(capsys.readouterr().out)
    assert sub["status"] == "queued"
    assert main(["stats"]) == 0
    assert json.loads(capsys.readouterr().out)["ready"] == 1
    assert main(["worker", "--drain"]) == 0
    assert json.loads(capsys.readouterr().out)["done"] == 1
    assert main(["status", sub["job_id"]]) == 0
    view = json.loads(capsys.readouterr().out)
    assert view["status"] == "done" and view["result"]["text"]


def test_cli_rejects_bad_file(cli_env, tmp_path, capsys):
    bad = tmp_path / "x.png"
    bad.write_bytes(b"")
    assert main(["submit", str(bad)]) == 1
    assert "rejected" in capsys.readouterr().err


def test_cli_config_errors_are_friendly(monkeypatch, capsys):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("GEMINI_MODEL", "m")
    assert main(["stats"]) == 2
    assert "GEMINI_API_KEY missing" in capsys.readouterr().err
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    monkeypatch.delenv("GEMINI_MODEL")
    assert main(["stats"]) == 2
    assert "GEMINI_MODEL missing" in capsys.readouterr().err
