import json

import pytest

from app.copy_format import normalize, to_draft

pytestmark = pytest.mark.unit

REEL = """POST TYPE: Construction update

FACEBOOK CAPTION:
MGC Jewel has reached its 25th of 27 floors.

On-ground construction continues at Bahria Town Phase 8. Filmed on site, November 2026.

📍 Book a site visit at MGC Jewel, Bahria Town Phase 8.
#MGCDevelopments #MGCJewel

INSTAGRAM CAPTION:
25th of 27 floors at MGC Jewel. 🏗️
Filmed on site, November 2026. Link in bio to book a site visit.

INSTAGRAM HASHTAGS (max 5):
#MGCDevelopments #MGCJewel #ConstructionUpdate #BahriaTownPhase8

REEL ON-SCREEN TEXT / COVER TITLE:
- 25th floor reached
- 2 floors to go
- Filmed on site
Cover title: 25 of 27 floors

CAROUSEL SLIDE TITLES: N/A

ALT TEXT:
A concrete tower under construction with cranes on its top floor.

FIRST COMMENT (optional): Which unit type are you looking at?

NOTES:
- [NEEDS: confirm the phone number for Meta posts]
- Plot size not stated: unconfirmed in the fact sheet.
"""


def test_a_full_reply_becomes_the_structured_draft():
    draft = to_draft(REEL)
    assert draft["facebook"]["caption"].startswith("MGC Jewel has reached its 25th of 27 floors.")
    assert "#MGCJewel" in draft["facebook"]["caption"]  # Facebook tags stay in its caption
    ig = draft["instagram"]
    assert ig["caption"].startswith("25th of 27 floors at MGC Jewel.")
    assert ig["hashtags"] == ["#MGCDevelopments", "#MGCJewel", "#ConstructionUpdate",
                              "#BahriaTownPhase8"]
    assert ig["reel_on_screen_text"] == ["25th floor reached", "2 floors to go", "Filmed on site"]
    assert ig["cover_title"] == "25 of 27 floors"
    assert "carousel_slide_titles" not in ig  # "N/A" is left out
    summary = draft["content_summary"]
    assert summary["post_type"] == "Construction update"
    assert summary["alt_text"] == [
        "A concrete tower under construction with cranes on its top floor."]
    assert summary["first_comment"] == "Which unit type are you looking at?"
    assert summary["notes"] == ["[NEEDS: confirm the phone number for Meta posts]",
                                "Plot size not stated: unconfirmed in the fact sheet."]


def test_markdown_labels_and_a_carousel():
    reply = ("**POST TYPE:** Project or unit spotlight\n\n**FACEBOOK CAPTION:**\nHotel suites at "
             "MGC Jewel.\n\n**INSTAGRAM CAPTION:**\nHotel suites.\n\n## CAROUSEL SLIDE TITLES:\n"
             "1. The lobby\n2. A suite\n\n**REEL ON-SCREEN TEXT / COVER TITLE:** (Reels only)")
    draft = to_draft(reply)
    assert draft["content_summary"]["post_type"] == "Project or unit spotlight"
    assert draft["instagram"]["carousel_slide_titles"] == ["The lobby", "A suite"]
    assert "reel_on_screen_text" not in draft["instagram"]


def test_a_facebook_only_post():
    draft = to_draft("POST TYPE: Iqbal Day\nFACEBOOK CAPTION:\nRemembering Allama Iqbal.")
    assert draft == {"facebook": {"caption": "Remembering Allama Iqbal."},
                     "content_summary": {"post_type": "Iqbal Day"}}


@pytest.mark.parametrize("reply", [
    "I only write for MGC's Facebook Page and Instagram, so I can't write a TikTok post.",
    '{"instagram": {"caption": "already JSON"}}',
    "",
])
def test_anything_else_is_kept_as_it_came(reply):
    assert to_draft(reply) is None
    assert normalize(reply) == reply


def test_normalize_writes_json_the_app_reads():
    assert json.loads(normalize(REEL))["instagram"]["cover_title"] == "25 of 27 floors"


@pytest.mark.parametrize(("reply", "sections"), [
    ("POST TYPE: Spotlight\nINSTAGRAM CAPTION:\nHotel suites at MGC Jewel.\n"
     "INSTAGRAM HASHTAGS (max 5):\n#MGCDevelopments #MGCJewel\nALT TEXT:\nA hotel suite.\n",
     {"instagram", "content_summary"}),
    ("POST TYPE: Spotlight\nFACEBOOK CAPTION:\nHotel suites at MGC Jewel.\n"
     "ALT TEXT:\nA hotel suite.\nNOTES: Instagram not requested.\n",
     {"facebook", "content_summary"}),
])
def test_a_brief_for_one_platform_gives_a_draft_for_that_platform_only(reply, sections):
    assert set(to_draft(reply)) == sections
