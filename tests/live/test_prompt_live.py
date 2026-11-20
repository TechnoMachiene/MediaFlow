"""Does real Gemini follow the copywriter prompt's checkable rules? Run: make live

One call with a generated "building site" picture (no real photo leaves the machine). The
reply must parse into the app's draft, and pass the rules a program can check:

    format          labelled sections the app reads (POST TYPE, captions, ALT TEXT, NOTES)
    hashtags        Instagram at most 5, Facebook at most 3 (prompt parts C and E)
    plain text      no Unicode bold or italic letters (rule A9)
    no promises     none of prompts/flagged_phrases.txt (rules A3, A4, B)
    no phone        while part G says the number is not confirmed, none is written
    no invention    with no brief, missing facts appear as [NEEDS: ...], listed under NOTES

Tone, accuracy against the fact sheet and judgement still need a person: this catches the
mechanical failures before anyone reads the draft.
"""

import os
import re

import pytest
from dotenv import load_dotenv
from PIL import Image, ImageDraw

from app.copy_format import to_draft
from app.draft_checks import PHONE, STYLED, check
from app.gemini_client import GeminiClient
from app.jobs import JobSpec
from app.system_prompt import load_system_prompt

load_dotenv()
pytestmark = pytest.mark.live

needs_gemini = pytest.mark.skipif(
    not (os.environ.get("RUN_LIVE") == "1" and os.environ.get("GEMINI_API_KEY")
         and os.environ.get("GEMINI_MODEL")),
    reason="needs RUN_LIVE=1 (make live) plus GEMINI_API_KEY and GEMINI_MODEL in .env",
)


def building_site(path):
    """A crude tower under construction: grey floors, a crane, blue sky."""
    im = Image.new("RGB", (640, 800), (135, 190, 235))
    d = ImageDraw.Draw(im)
    d.rectangle((0, 700, 640, 800), fill=(120, 100, 80))
    for floor in range(14):
        top = 680 - floor * 42
        d.rectangle((200, top, 440, top + 38), fill=(170, 170, 170), outline=(90, 90, 90))
    d.line((470, 760, 470, 60), fill=(240, 190, 20), width=10)
    d.line((300, 70, 620, 70), fill=(240, 190, 20), width=8)
    im.save(path)


@needs_gemini
def test_a_real_draft_follows_the_prompts_checkable_rules(tmp_path):
    photo = tmp_path / "site.png"
    building_site(photo)
    prompt = load_system_prompt()
    spec = JobSpec("live-prompt", str(photo), "image", "image/png", prompt,
                   os.environ["GEMINI_MODEL"], "1" * 64)
    out = GeminiClient(api_key=os.environ["GEMINI_API_KEY"]).analyse(spec)
    reply = out["source_text"]
    print(reply)

    draft = to_draft(reply)
    assert draft is not None, "the reply isn't in the labelled format the app reads"
    assert {"facebook", "instagram"} & set(draft), "no caption at all"
    summary = draft.get("content_summary", {})
    assert summary.get("alt_text"), "alt text missing"

    instagram = draft.get("instagram", {})
    ig_tags = len(instagram.get("hashtags", [])) + len(re.findall(r"#\w+",
                                                                  instagram.get("caption", "")))
    assert ig_tags <= 5, f"{ig_tags} Instagram hashtags (max 5)"
    fb_caption = draft.get("facebook", {}).get("caption", "")
    assert len(re.findall(r"#\w+", fb_caption)) <= 3, "more than 3 Facebook hashtags"

    copy = "\n".join(str(v) for section in draft.values() for v in section.values())
    assert not STYLED.search(copy), "Unicode bold or italic letters"
    risky = [i["message"] for i in check(out["text"]) if i["message"].startswith("Says")]
    assert not risky, f"flagged phrases: {risky}"

    if "NOT YET CONFIRMED" in prompt:
        captions = fb_caption + "\n" + instagram.get("caption", "")
        assert not PHONE.search(captions), "wrote a phone number that isn't confirmed"

    # No brief: whatever the picture can't tell (floors, dates, prices) must be a gap, not a
    # guess, and the team must be told.
    assert "[NEEDS" in reply.upper() or summary.get("notes"), "no gaps flagged without a brief"
