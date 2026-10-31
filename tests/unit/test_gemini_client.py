import io
import os
from types import SimpleNamespace

import pytest
from google.genai import errors, types
from PIL import Image

from app.config import Settings
from app.errors import Permanent, RateLimited, Transient
from app.gemini_client import GeminiClient, build_client
from app.jobs import JobSpec
from app.system_prompt import SYSTEM_PROMPT
from tests.helpers import ScriptedClient

pytestmark = pytest.mark.unit


class StubModels:
    def __init__(self, outcome):
        self.outcome, self.calls = outcome, []

    def generate_content(self, *, model, contents, config):
        self.calls.append((model, contents, config))
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


class StubFiles:
    def __init__(self, states=("ACTIVE",)):
        self.states, self.deleted, self.uploaded = list(states), [], []

    def _f(self, state):
        return SimpleNamespace(
            name="files/abc",
            uri="https://x/files/abc",
            mime_type="video/mp4",
            state=SimpleNamespace(name=state),
        )

    def upload(self, *, file):
        self.uploaded.append(file)
        return self._f(self.states.pop(0))

    def get(self, *, name):
        return self._f(self.states.pop(0))

    def delete(self, *, name):
        self.deleted.append(name)


def stub(outcome, states=("ACTIVE",)):
    return SimpleNamespace(models=StubModels(outcome), files=StubFiles(states))


def ok_resp(text="hello"):
    return SimpleNamespace(text=text, usage_metadata=SimpleNamespace(total_token_count=42))


def spec(path, kind="image", mime="image/png"):
    return JobSpec("j1", path, kind, mime, "describe", "m1", "a" * 64)


def client(stub_client, **kw):
    return GeminiClient(api_key="unused", client=stub_client, poll_s=0, **kw)


def test_image_path(make_image):
    path = make_image(size=(3000, 2000))
    c = stub(ok_resp())
    out = client(c).analyse(spec(path))
    assert out == {"text": "hello", "kind": "image", "model": "m1", "tokens": 42}
    model, contents, config = c.models.calls[0]
    assert model == "m1" and len(contents) == 1
    assert config.system_instruction == SYSTEM_PROMPT
    inline = contents[0].inline_data
    assert inline.mime_type == "image/jpeg"
    assert max(Image.open(io.BytesIO(inline.data)).size) == 1024
    assert config.media_resolution == types.MediaResolution.MEDIA_RESOLUTION_LOW
    assert c.files.uploaded == []  # images go inline, no Files API


def test_video_polls_then_deletes_upload(make_video):
    c = stub(ok_resp("vid"), states=["PROCESSING", "PROCESSING", "ACTIVE"])
    out = client(c, video_fps=0.5).analyse(spec(make_video(), "video", "video/mp4"))
    assert out["text"] == "vid"
    part = c.models.calls[0][1][0]
    assert part.video_metadata.fps == 0.5 and part.file_data.file_uri == "https://x/files/abc"
    assert c.files.deleted == ["files/abc"]


def test_large_video_is_compressed_before_upload(tmp_path, monkeypatch):
    source = tmp_path / "large.mp4"
    source.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"x" * (1024 * 1024))
    compressed_path = []

    def fake_compress(path, output_path, *, max_bytes):
        assert path == str(source)
        assert max_bytes == 1024 * 1024
        with open(output_path, "wb") as output:
            output.write(b"small compressed video")
        compressed_path.append(output_path)
        return output_path

    monkeypatch.setattr("app.gemini_client.compress_video", fake_compress)
    c = stub(ok_resp("compressed"))
    assert client(c, max_video_mb=1).analyse(spec(str(source), "video", "video/mp4"))["text"] == (
        "compressed"
    )
    assert c.files.uploaded == compressed_path
    assert c.files.deleted == ["files/abc"]
    assert not os.path.exists(compressed_path[0])


def test_video_upload_deleted_even_when_generate_fails(make_video):
    c = stub(errors.ServerError(503, {"error": {"message": "overloaded"}}))
    with pytest.raises(Transient):
        client(c).analyse(spec(make_video(), "video", "video/mp4"))
    assert c.files.deleted == ["files/abc"]


def test_video_processing_failed(make_video):
    c = stub(ok_resp(), states=["FAILED"])
    with pytest.raises(Permanent, match="failed to process"):
        client(c).analyse(spec(make_video(), "video", "video/mp4"))
    assert c.files.deleted == ["files/abc"]


def test_video_processing_timeout(make_video):
    c = stub(ok_resp(), states=["PROCESSING"] * 50)
    with pytest.raises(Transient, match="still processing"):
        client(c, video_wait_s=0.0).analyse(spec(make_video(), "video", "video/mp4"))
    assert c.files.deleted == ["files/abc"]


@pytest.mark.parametrize(
    "exc,expected",
    [
        (errors.ClientError(429, {"error": {"message": "quota"}}), RateLimited),
        (errors.ServerError(503, {"error": {"message": "busy"}}), Transient),
        (errors.ClientError(400, {"error": {"message": "bad"}}), Permanent),
        (errors.ClientError(403, {"error": {"message": "key"}}), Permanent),
        (errors.ClientError(408, {"error": {"message": "timeout"}}), Transient),
        (ConnectionError("reset"), Transient),
    ],
)
def test_error_mapping(make_image, exc, expected):
    with pytest.raises(expected):
        client(stub(exc)).analyse(spec(make_image()))


def test_empty_or_blocked_response(make_image):
    with pytest.raises(Permanent, match="empty or blocked"):
        client(stub(ok_resp(text=None))).analyse(spec(make_image()))


def test_missing_or_corrupt_file_is_permanent(tmp_path):
    with pytest.raises(Permanent):
        client(stub(ok_resp())).analyse(spec(str(tmp_path / "gone.png")))
    bad = tmp_path / "bad.png"
    bad.write_bytes(b"junk")
    with pytest.raises(Permanent):
        client(stub(ok_resp())).analyse(spec(str(bad)))


def test_scripted_client_plan_and_calls():
    sc = ScriptedClient(plan=[RateLimited("x"), None])
    s = spec("p")
    with pytest.raises(RateLimited):
        sc.analyse(s)
    assert "scripted analysis" in sc.analyse(s)["text"] and len(sc.calls) == 2


def test_build_client_always_returns_real_client(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "dummy")
    monkeypatch.setenv("GEMINI_MODEL", "m")
    assert isinstance(build_client(Settings.from_env()), GeminiClient)
