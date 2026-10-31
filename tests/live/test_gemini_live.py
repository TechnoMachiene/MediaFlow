"""Smoke test against REAL Gemini. Run: make live   (reads .env; never shell-sources it)
Uses a throwaway generated image. Free tier may train on it."""
import os

import pytest
from dotenv import load_dotenv
from PIL import Image

from app.gemini_client import GeminiClient
from app.jobs import JobSpec

load_dotenv()
pytestmark = pytest.mark.live


@pytest.mark.skipif(
    not (os.environ.get("RUN_LIVE") == "1" and os.environ.get("GEMINI_API_KEY")
         and os.environ.get("GEMINI_MODEL")),
    reason="needs RUN_LIVE=1 (make live) plus GEMINI_API_KEY and GEMINI_MODEL in .env",
)
def test_live_image(tmp_path):
    p = tmp_path / "red.png"
    Image.new("RGB", (64, 64), (255, 0, 0)).save(p)
    spec = JobSpec("live1", str(p), "image", "image/png", "What colour is this image? One word.",
                   os.environ["GEMINI_MODEL"], "0" * 64)
    out = GeminiClient(api_key=os.environ["GEMINI_API_KEY"]).analyse(spec)
    print(out)
    assert out["text"]
