import json

import pytest

from app import draft_checks
from app.draft_checks import blocking, check

pytestmark = pytest.mark.unit


def draft(**sections):
    return json.dumps(sections)


def messages(issues):
    return [i["message"] for i in issues]


def test_a_clean_draft_has_nothing_to_check():
    text = draft(facebook={"caption": "25th of 27 floors at MGC Jewel. 📞 +92 304 1111642"},
                 instagram={"caption": "Floor 25 done.", "hashtags": ["#MGCDevelopments"]})
    assert check(text) == []


def test_placeholders_block_approval_but_notes_may_name_them():
    text = draft(facebook={"caption": "Plot size: [NEEDS: confirmed kanal]. Visit us."},
                 content_summary={"notes": ["[NEEDS: confirmed kanal] for MGC Jewel"]})
    [issue] = check(text)
    assert issue == {"level": "block", "field": "facebook.caption",
                     "message": "Fill in [NEEDS: confirmed kanal] before approving"}
    assert blocking(check(text)) == [issue]


def test_risky_words_styled_letters_hashtags_and_phones_are_flagged():
    text = draft(
        facebook={"caption": "𝐁𝐞𝐬𝐭 deal. Guaranteed returns! #a #b #c #d Call 0304 1111642 "
                             "or +92 325 4441111"},
        instagram={"caption": "Hello", "hashtags": ["#1", "#2", "#3", "#4", "#5", "#6"]},
    )
    found = messages(check(text))
    assert any("bold or italic" in m for m in found)
    assert 'Says "guaranteed": check it\'s allowed and true' in found
    assert any("4 hashtags" in m and "facebook" in m for m in found)
    assert any("6 hashtags" in m and "instagram" in m for m in found)
    assert any("2 different phone numbers" in m for m in found)
    assert blocking(check(text)) == []  # warnings only


def test_phrases_match_whole_words_only():
    assert check(draft(facebook={"caption": "A grand landmarking ceremony"})) == []
    assert messages(check(draft(facebook={"caption": "A new landmark."}))) == [
        'Says "landmark": check it\'s allowed and true']


def test_instagram_caption_length_blocks():
    issues = check(draft(instagram={"caption": "x" * 2201}))
    assert blocking(issues) and "2200" in issues[0]["message"]


def test_plain_text_drafts_are_checked_too():
    assert blocking(check("Price: [NEEDS: price list date]"))[0]["field"] == "draft"


def test_the_phrase_list_comes_from_a_file(tmp_path, monkeypatch):
    phrases = tmp_path / "phrases.txt"
    phrases.write_text("# comment\nfree gift\n")
    monkeypatch.setenv("FLAGGED_PHRASES_FILE", str(phrases))
    assert draft_checks.flagged_phrases() == ("free gift",)
    assert messages(check(draft(facebook={"caption": "Free gift inside"}))) == [
        'Says "free gift": check it\'s allowed and true']
    monkeypatch.setenv("FLAGGED_PHRASES_FILE", str(tmp_path / "missing.txt"))
    assert draft_checks.flagged_phrases() == ()


def test_one_number_written_two_ways_is_one_number():
    text = draft(facebook={"caption": "Call +92 304 1111642."},
                 instagram={"caption": "WhatsApp 0304-1111642"})
    assert check(text) == []
