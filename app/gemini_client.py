"""Gemini access behind one small interface.

GeminiClient: the only client the app uses. Automated tests use a scripted stand-in that
lives in tests/helpers.py (never importable from app code). Live check: `make live`.
"""

from __future__ import annotations

import os
import tempfile
import time
from typing import Protocol

from app.errors import MediaError, Permanent, RateLimited, Transient
from app.jobs import JobSpec
from app.media import compress_video, prep_image
from app.system_prompt import SYSTEM_PROMPT


class AnalysisClient(Protocol):
    def analyse(self, spec: JobSpec) -> dict: ...


def _map_error(e: Exception) -> Exception:
    code = getattr(e, "code", None)
    msg = f"{type(e).__name__}: {e}"[:300]
    if code == 429:
        return RateLimited(msg)
    if isinstance(code, int) and (code in (408, 409) or code >= 500):
        return Transient(msg)
    if isinstance(code, int) and 400 <= code < 500:
        return Permanent(msg)
    return Transient(msg)  # network hiccups, timeouts, unknown


class GeminiClient:
    def __init__(
        self,
        *,
        api_key: str,
        image_max_side: int = 1024,
        video_fps: float = 1.0,
        max_video_mb: int = 100,
        poll_s: float = 2.0,
        video_wait_s: float = 300.0,
        client=None,
    ):
        from google import genai
        from google.genai import types

        self._types = types
        self.client = client or genai.Client(api_key=api_key)
        self.image_max_side, self.video_fps = image_max_side, video_fps
        self.max_video_bytes = max_video_mb * 1024 * 1024
        self.poll_s, self.video_wait_s = poll_s, video_wait_s

    def analyse(self, spec: JobSpec) -> dict:
        types = self._types
        uploaded: list = []  # filled the moment upload succeeds, so cleanup always sees it
        compressed_dir = None
        try:
            if spec.kind == "image":
                data, mime = prep_image(spec.path, self.image_max_side)
                part = types.Part.from_bytes(data=data, mime_type=mime)
            else:
                video_path = spec.path
                if os.path.getsize(video_path) > self.max_video_bytes:
                    compressed_dir = tempfile.TemporaryDirectory(prefix="media-queue-video-")
                    video_path = compress_video(
                        video_path,
                        f"{compressed_dir.name}/compressed.mp4",
                        max_bytes=self.max_video_bytes,
                    )
                ready = self._upload_video(spec, uploaded, path=video_path)
                part = types.Part(
                    file_data=types.FileData(file_uri=ready.uri, mime_type=ready.mime_type),
                    video_metadata=types.VideoMetadata(fps=self.video_fps),
                )
            config = types.GenerateContentConfig(
                media_resolution=types.MediaResolution.MEDIA_RESOLUTION_LOW,
                system_instruction=SYSTEM_PROMPT,
            )
            resp = self.client.models.generate_content(
                model=spec.model, contents=[part], config=config
            )
        except (Permanent, Transient, RateLimited):
            raise
        except MediaError as e:
            raise Permanent(str(e)) from None
        except FileNotFoundError as e:
            raise Permanent(f"file missing: {e}") from None
        except Exception as e:  # SDK / network
            raise _map_error(e) from None
        finally:
            if uploaded:
                try:  # privacy: do not leave user video on Google's side
                    self.client.files.delete(name=uploaded[0].name)
                except Exception:
                    pass
            if compressed_dir is not None:
                compressed_dir.cleanup()
        text = getattr(resp, "text", None)
        if not text:
            raise Permanent("empty or blocked response")
        usage = getattr(resp, "usage_metadata", None)
        return {
            "text": text,
            "kind": spec.kind,
            "model": spec.model,
            "tokens": getattr(usage, "total_token_count", None),
        }

    def _upload_video(self, spec: JobSpec, uploaded: list, *, path: str | None = None):
        types = self._types
        f = self.client.files.upload(file=path or spec.path)
        uploaded.append(f)
        deadline = time.monotonic() + self.video_wait_s
        while True:
            state = getattr(getattr(f, "state", None), "name", None)
            if state == "ACTIVE" or state is None:
                return f
            if state == types.FileState.FAILED.name:
                raise Permanent("Gemini failed to process video")
            if time.monotonic() > deadline:
                raise Transient("video still processing; retry later")
            time.sleep(self.poll_s)
            f = self.client.files.get(name=f.name)


def build_client(settings) -> AnalysisClient:
    return GeminiClient(
        api_key=settings.api_key,
        image_max_side=settings.image_max_side,
        video_fps=settings.video_fps,
        max_video_mb=settings.max_video_mb,
    )
