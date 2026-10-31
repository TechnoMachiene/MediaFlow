"""Validate uploads, hash ORIGINAL bytes, shrink images by pixels."""
from __future__ import annotations

import hashlib
import io
import os
import shutil
import subprocess
from dataclasses import dataclass

from PIL import Image, ImageOps, UnidentifiedImageError

from app.errors import MediaError

ALLOWED = {
    ".jpg": ("image", "image/jpeg"),
    ".jpeg": ("image", "image/jpeg"),
    ".png": ("image", "image/png"),
    ".webp": ("image", "image/webp"),
    ".mp4": ("video", "video/mp4"),
    ".mov": ("video", "video/quicktime"),
    ".webm": ("video", "video/webm"),
}


@dataclass(frozen=True)
class MediaInfo:
    path: str
    kind: str  # "image" | "video"
    mime: str
    size: int


def _sniff_video(path: str, ext: str) -> bool:
    with open(path, "rb") as f:
        head = f.read(12)
    if ext in (".mp4", ".mov"):
        return head[4:8] == b"ftyp"
    if ext == ".webm":
        return head[:4] == b"\x1a\x45\xdf\xa3"
    return False


def inspect_media(path: str, *, max_image_mb: int, max_video_mb: int) -> MediaInfo:
    if not os.path.isfile(path):
        raise MediaError(f"file not found: {path}")
    ext = os.path.splitext(path)[1].lower()
    if ext not in ALLOWED:
        allowed = ", ".join(sorted(ALLOWED))
        raise MediaError(f"unsupported type {ext or '(none)'}; allowed: {allowed}")
    kind, mime = ALLOWED[ext]
    size = os.path.getsize(path)
    if size == 0:
        raise MediaError("file is empty")
    limit_mb = max_image_mb if kind == "image" else max_video_mb
    if size > limit_mb * 1024 * 1024:
        raise MediaError(f"{kind} too large: {size / 1048576:.1f} MB > {limit_mb} MB")
    if kind == "image":
        try:
            with Image.open(path) as im:
                im.verify()
        except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as e:
            raise MediaError(f"corrupt or not a real image: {e}") from None
    elif not _sniff_video(path, ext):
        raise MediaError(f"file content does not match {ext}")
    return MediaInfo(path=path, kind=kind, mime=mime, size=size)


def sha256_file(path: str, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while block := f.read(chunk):
            h.update(block)
    return h.hexdigest()


def prompt_hash(prompt: str, params: str = "") -> str:
    return hashlib.sha256(f"{prompt}\x00{params}".encode()).hexdigest()


def make_cache_key(content_sha: str, prompt: str, model: str, params: str = "") -> str:
    return f"{content_sha}:{prompt_hash(prompt, params)}:{model}"


def prep_image(path: str, max_side: int) -> tuple[bytes, str]:
    """Respect EXIF rotation, shrink so longest side <= max_side (never upscale).
    Alpha images stay PNG, others become JPEG."""
    try:
        with Image.open(path) as im:
            im = ImageOps.exif_transpose(im)
            im.thumbnail((max_side, max_side))
            buf = io.BytesIO()
            if im.mode in ("RGBA", "LA", "P") and (im.mode != "P" or "transparency" in im.info):
                im.convert("RGBA").save(buf, "PNG", optimize=True)
                return buf.getvalue(), "image/png"
            im.convert("RGB").save(buf, "JPEG", quality=85, optimize=True)
            return buf.getvalue(), "image/jpeg"
    except (UnidentifiedImageError, OSError) as e:
        raise MediaError(f"cannot read image: {e}") from None


def compress_video(path: str, output_path: str, *, max_bytes: int) -> str:
    """Transcode a large video to a smaller, Gemini-friendly MP4."""
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise MediaError("ffmpeg is required to compress large videos; install it on the worker")

    attempts = ((1280, 30), (960, 32), (720, 34))
    for max_side, crf in attempts:
        if os.path.exists(output_path):
            os.unlink(output_path)
        command = [
            ffmpeg,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            path,
            "-map",
            "0:v:0",
            "-map",
            "0:a:0?",
            "-vf",
            (
                f"scale='min({max_side},iw)':'min({max_side},ih)'"
                ":force_original_aspect_ratio=decrease:force_divisible_by=2"
            ),
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-crf",
            str(crf),
            "-c:a",
            "aac",
            "-b:a",
            "64k",
            "-movflags",
            "+faststart",
            "-f",
            "mp4",
            output_path,
        ]
        try:
            result = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=3600,
            )
        except subprocess.TimeoutExpired:
            raise MediaError("video compression exceeded the 60-minute time limit") from None
        if result.returncode != 0:
            detail = result.stderr.strip()[-500:]
            raise MediaError(f"video compression failed: {detail or 'ffmpeg returned an error'}")
        if os.path.getsize(output_path) <= max_bytes:
            return output_path

    os.unlink(output_path)
    raise MediaError("compressed video still exceeds MAX_VIDEO_MB; reduce the video duration")
