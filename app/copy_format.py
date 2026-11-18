"""Turn the copywriter prompt's labelled reply into the draft structure the app works with.

The system prompt (prompts/system_prompt.md, part I) asks Gemini for labelled plain text:

    POST TYPE: Construction update
    FACEBOOK CAPTION:
    ...
    INSTAGRAM CAPTION:
    ...
    INSTAGRAM HASHTAGS (max 5):
    #MGCDevelopments #MGCJewel
    REEL ON-SCREEN TEXT / COVER TITLE: ...
    CAROUSEL SLIDE TITLES: ...
    ALT TEXT:
    ...
    FIRST COMMENT (optional): ...
    NOTES: ...

The draft editor, review screen and publishing read a JSON object keyed by platform, so the
reply is converted to:

    {"facebook":  {"caption"},
     "instagram": {"caption", "hashtags", "reel_on_screen_text", "cover_title",
                   "carousel_slide_titles"},
     "content_summary": {"post_type", "alt_text", "first_comment", "notes"}}

Empty sections are left out. A reply with neither caption (for example a refusal) is kept
as plain text instead, which the app shows as it is.
"""

from __future__ import annotations

import json
import re

_LABELS = {
    "POST TYPE": "post_type",
    "FACEBOOK CAPTION": "facebook",
    "INSTAGRAM CAPTION": "instagram",
    "INSTAGRAM HASHTAGS": "hashtags",
    "REEL ON-SCREEN TEXT": "reel",
    "COVER TITLE": "cover_title",
    "CAROUSEL SLIDE TITLES": "carousel",
    "ALT TEXT": "alt_text",
    "FIRST COMMENT": "first_comment",
    "NOTES": "notes",
}
# A label at the start of a line, optionally wrapped in markdown (#, **), with any qualifier
# before its colon ("(max 5)", "/ COVER TITLE", "(optional)").
_LABEL_LINE = re.compile(
    r"^[ \t]*(?:#+[ \t]*)?\**[ \t]*(?P<label>" + "|".join(re.escape(k) for k in _LABELS)
    + r")\b[^:\n]*:\**[ \t]*(?P<rest>.*)$",
    re.IGNORECASE | re.MULTILINE,
)
# What the prompt prints when a section doesn't apply.
_PLACEHOLDER = re.compile(
    r"^\(?\s*(n/?a|none|not applicable|-+|\(?reels only\)?|\(?carousels only\)?)\s*\)?\.?$",
    re.IGNORECASE,
)
_BULLET = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+")


def _sections(text: str) -> dict[str, str]:
    found: dict[str, str] = {}
    matches = list(_LABEL_LINE.finditer(text))
    for i, m in enumerate(matches):
        key = _LABELS[m.group("label").upper()]
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = (m.group("rest") + text[m.end():end]).strip()
        if key not in found and body and not _PLACEHOLDER.match(body):
            found[key] = body
    return found


def _lines(body: str) -> list[str]:
    out = []
    for line in body.splitlines():
        line = _BULLET.sub("", line).strip()
        if line and not _PLACEHOLDER.match(line):
            out.append(line)
    return out


def to_draft(text: str) -> dict | None:
    """The structured draft, or None when the reply isn't in the labelled format."""
    s = _sections(text)
    if "facebook" not in s and "instagram" not in s:
        return None

    instagram: dict = {}
    if "instagram" in s:
        instagram["caption"] = s["instagram"]
    if "hashtags" in s:
        instagram["hashtags"] = re.findall(r"#\w+", s["hashtags"])
    if "reel" in s:
        screen = []
        for line in _lines(s["reel"]):
            cover = re.match(r"cover title\s*:\s*(.+)", line, re.IGNORECASE)
            if cover:
                s.setdefault("cover_title", cover.group(1).strip())
            else:
                screen.append(re.sub(r"^on-screen text\s*:\s*", "", line, flags=re.IGNORECASE))
        if screen:
            instagram["reel_on_screen_text"] = screen
    if "cover_title" in s:
        instagram["cover_title"] = s["cover_title"]
    if "carousel" in s:
        instagram["carousel_slide_titles"] = _lines(s["carousel"])

    summary: dict = {}
    if "post_type" in s:
        summary["post_type"] = s["post_type"]
    if "alt_text" in s:
        summary["alt_text"] = _lines(s["alt_text"])
    if "first_comment" in s:
        summary["first_comment"] = s["first_comment"]
    if "notes" in s:
        summary["notes"] = _lines(s["notes"])

    draft: dict = {}
    if "facebook" in s:
        draft["facebook"] = {"caption": s["facebook"]}
    if instagram:
        draft["instagram"] = instagram
    if summary:
        draft["content_summary"] = summary
    return draft


def normalize(text: str) -> str:
    """The text to store as the draft: the structured JSON when the reply is in the labelled
    format, otherwise the reply unchanged."""
    draft = to_draft(text)
    return text if draft is None else json.dumps(draft, ensure_ascii=False, indent=2)
