import json
import threading

import pytest

from app.errors import MediaError

pytestmark = pytest.mark.integration


def test_new_upload_is_queued(make_rt, make_image):
    rt = make_rt()
    out = rt.store.submit(1, make_image(), "describe")
    assert out["status"] == "queued" and not out["cached"]
    assert rt.queue.depths()["ready"] == 1
    job = rt.store.get(out["job_id"])
    assert job.status == "queued" and job.kind == "image" and job.user_id == "1"
    assert len(job.sha256) == 64


def test_same_file_same_prompt_dedups(make_rt, make_image):
    rt = make_rt()
    p = make_image()
    a = rt.store.submit(1, p, "describe")
    b = rt.store.submit(2, p, "describe")
    assert (a["status"], b["status"]) == ("queued", "waiting")
    assert a["cache_key"] == b["cache_key"] and rt.queue.depths()["ready"] == 1


def test_same_bytes_different_filename_dedups(make_rt, make_image, tmp_path):
    rt = make_rt()
    p = make_image(name="one.png")
    copy = tmp_path / "totally_different_name.png"
    copy.write_bytes(open(p, "rb").read())
    assert rt.store.submit(1, p, "d")["status"] == "queued"
    assert rt.store.submit(2, str(copy), "d")["status"] == "waiting"


def test_different_prompt_or_content_not_deduped(make_rt, make_image):
    rt = make_rt()
    p = make_image()
    assert rt.store.submit(1, p, "describe")["status"] == "queued"
    assert rt.store.submit(1, p, "summarize")["status"] == "queued"
    assert rt.store.submit(1, make_image(), "describe")["status"] == "queued"
    assert rt.queue.depths()["ready"] == 3


def test_cached_result_skips_queue(make_rt, make_image):
    rt = make_rt()
    p = make_image()
    first = rt.store.submit(1, p, "d")
    job = rt.store.get(first["job_id"])
    rt.store.finish_ok(job, {"text": "answer"})
    rt.queue.claim()
    again = rt.store.submit(9, p, "d")
    assert again["status"] == "done" and again["cached"]
    assert rt.queue.depths()["ready"] == 0
    view = rt.store.status(again["job_id"])
    assert view["status"] == "done" and view["result"]["text"] == "answer" and view["cached"]


def test_concurrent_identical_uploads_enqueue_exactly_once(make_rt, make_image):
    rt = make_rt()
    p = make_image()
    outs, lock = [], threading.Lock()
    barrier = threading.Barrier(20)

    def run(i):
        barrier.wait()
        o = rt.store.submit(i, p, "d")
        with lock:
            outs.append(o["status"])

    ts = [threading.Thread(target=run, args=(i,)) for i in range(20)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert outs.count("queued") == 1 and outs.count("waiting") == 19
    assert rt.queue.depths()["ready"] == 1


def test_rejected_upload_leaves_no_trace(make_rt, tmp_path, redis_client):
    rt = make_rt()
    bad = tmp_path / "x.jpg"
    bad.write_text("nope")
    with pytest.raises(MediaError):
        rt.store.submit(1, str(bad), "d")
    assert rt.queue.depths()["ready"] == 0
    assert list(redis_client.scan_iter(match=f"{rt.settings.prefix}:job:*")) == []


def test_status_unknown_job(make_rt):
    assert make_rt().store.status("nope")["status"] == "unknown"


def test_waiting_resolves_when_result_lands(make_rt, make_image):
    rt = make_rt()
    p = make_image()
    a = rt.store.submit(1, p, "d")
    b = rt.store.submit(2, p, "d")
    assert rt.store.status(b["job_id"])["status"] == "waiting"
    rt.store.finish_ok(rt.store.get(a["job_id"]), {"text": "x"})
    view = rt.store.status(b["job_id"])
    assert view["status"] == "done" and view["result"] == {"text": "x"}


def test_waiting_fails_if_primary_vanished(make_rt, make_image):
    rt = make_rt()
    p = make_image()
    a = rt.store.submit(1, p, "d")
    b = rt.store.submit(2, p, "d")
    rt.store.finish_failed(rt.store.get(a["job_id"]), "failed", "boom")
    view = rt.store.status(b["job_id"])
    assert view["status"] == "failed" and "resubmit" in view["error"]
    assert rt.store.submit(3, p, "d")["status"] == "queued"  # retry path open again


def test_result_json_roundtrip(make_rt, make_image):
    rt = make_rt()
    a = rt.store.submit(1, make_image(), "d")
    payload = {"text": "héllo ✓", "tokens": 7}
    rt.store.finish_ok(rt.store.get(a["job_id"]), payload)
    assert rt.store.status(a["job_id"])["result"] == json.loads(json.dumps(payload))
