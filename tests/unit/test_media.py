import hashlib
import io
import os
import subprocess

import pytest
from PIL import Image

from app.errors import MediaError
from app.media import (
    compress_video,
    inspect_media,
    make_cache_key,
    prep_image,
    prompt_hash,
    sha256_file,
)

pytestmark = pytest.mark.unit
LIM = dict(max_image_mb=10, max_video_mb=100)


def test_valid_image_and_videos(make_image, make_video):
    assert inspect_media(make_image(fmt="PNG"), **LIM).kind == "image"
    assert inspect_media(make_image(fmt="JPEG", name="a.jpg"), **LIM).mime == "image/jpeg"
    assert inspect_media(make_video(), **LIM).mime == "video/mp4"
    assert inspect_media(make_video(ext="webm"), **LIM).mime == "video/webm"


def test_missing_and_directory(tmp_path):
    with pytest.raises(MediaError, match="not found"):
        inspect_media(str(tmp_path / "nope.png"), **LIM)
    with pytest.raises(MediaError, match="not found"):
        inspect_media(str(tmp_path), **LIM)


def test_empty_file(tmp_path):
    p = tmp_path / "e.png"
    p.write_bytes(b"")
    with pytest.raises(MediaError, match="empty"):
        inspect_media(str(p), **LIM)


def test_unsupported_extension(tmp_path):
    p = tmp_path / "x.exe"
    p.write_bytes(b"MZ")
    with pytest.raises(MediaError, match="unsupported"):
        inspect_media(str(p), **LIM)
    q = tmp_path / "noext"
    q.write_bytes(b"x")
    with pytest.raises(MediaError, match="unsupported"):
        inspect_media(str(q), **LIM)


def test_too_large(tmp_path):
    p = tmp_path / "big.png"
    p.write_bytes(b"0" * (2 * 1024 * 1024))
    with pytest.raises(MediaError, match="too large"):
        inspect_media(str(p), max_image_mb=1, max_video_mb=100)
    v = tmp_path / "big.mp4"
    v.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"0" * (2 * 1024 * 1024))
    with pytest.raises(MediaError, match="too large"):
        inspect_media(str(v), max_image_mb=10, max_video_mb=1)


def test_wrong_content_for_extension(tmp_path):
    p = tmp_path / "evil.jpg"
    p.write_text("not an image at all")
    with pytest.raises(MediaError, match="corrupt"):
        inspect_media(str(p), **LIM)
    v = tmp_path / "evil.mp4"
    v.write_bytes(b"this is text, not video")
    with pytest.raises(MediaError, match="does not match"):
        inspect_media(str(v), **LIM)


def test_hash_is_content_based(make_image, tmp_path):
    a = make_image(name="a.png", color=(1, 2, 3))
    b = tmp_path / "copy_with_other_name.png"
    b.write_bytes(open(a, "rb").read())
    assert sha256_file(a) == sha256_file(str(b)) == hashlib.sha256(open(a, "rb").read()).hexdigest()
    c = tmp_path / "c.png"
    c.write_bytes(open(a, "rb").read() + b"\x00")
    assert sha256_file(a) != sha256_file(str(c))


def test_hash_chunking_matches(tmp_path):
    p = tmp_path / "f.bin"
    p.write_bytes(b"abc" * 100_000)
    assert sha256_file(str(p), chunk=7) == hashlib.sha256(p.read_bytes()).hexdigest()


def test_cache_key_parts():
    base = make_cache_key("h", "describe", "m1")
    assert base != make_cache_key("h", "summarize", "m1")
    assert base != make_cache_key("h", "describe", "m2")
    assert base != make_cache_key("h2", "describe", "m1")
    assert base == make_cache_key("h", "describe", "m1")
    assert prompt_hash("a", "x") != prompt_hash("a", "y")


def test_prep_image_shrinks_not_upscales(make_image):
    big = make_image(size=(3000, 1500), fmt="PNG")
    data, mime = prep_image(big, 1024)
    assert mime == "image/jpeg"
    w, h = Image.open(io.BytesIO(data)).size
    assert max(w, h) == 1024 and abs(w / h - 2.0) < 0.02
    small = make_image(size=(100, 50))
    w, h = Image.open(io.BytesIO(prep_image(small, 1024)[0])).size
    assert (w, h) == (100, 50)


def test_prep_image_exif_rotation(tmp_path):
    im = Image.new("RGB", (200, 100), (10, 200, 30))
    exif = Image.Exif()
    exif[0x0112] = 6  # rotate 90 CW when displayed
    p = tmp_path / "rot.jpg"
    im.save(p, "JPEG", exif=exif)
    w, h = Image.open(io.BytesIO(prep_image(str(p), 1024)[0])).size
    assert (w, h) == (100, 200)


def test_prep_image_alpha_stays_png(tmp_path):
    p = tmp_path / "a.png"
    Image.new("RGBA", (50, 50), (0, 0, 0, 0)).save(p)
    data, mime = prep_image(str(p), 1024)
    assert mime == "image/png" and Image.open(io.BytesIO(data)).mode == "RGBA"


def test_prep_image_corrupt(tmp_path):
    p = tmp_path / "bad.png"
    p.write_bytes(b"garbage")
    with pytest.raises(MediaError):
        prep_image(str(p), 1024)


def test_compress_video_retries_at_lower_quality(tmp_path, monkeypatch):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    output = tmp_path / "compressed.mp4"
    monkeypatch.setattr("app.media.shutil.which", lambda _: "/usr/bin/ffmpeg")
    commands = []

    def fake_run(command, **kwargs):
        commands.append(command)
        output.write_bytes(b"x" * (20 if len(commands) == 1 else 8))
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr("app.media.subprocess.run", fake_run)
    assert compress_video(str(source), str(output), max_bytes=10) == str(output)
    assert len(commands) == 2
    assert commands[0][commands[0].index("-crf") + 1] == "30"
    assert commands[1][commands[1].index("-crf") + 1] == "32"
    assert output.stat().st_size == 8
    assert os.path.exists(output)


def test_compress_video_requires_ffmpeg(tmp_path, monkeypatch):
    monkeypatch.setattr("app.media.shutil.which", lambda _: None)
    with pytest.raises(MediaError, match="ffmpeg is required"):
        compress_video(str(tmp_path / "source.mp4"), str(tmp_path / "out.mp4"), max_bytes=10)
