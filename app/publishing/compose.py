"""Turn a draft into the text one channel posts: the platform's section of the JSON draft
(caption or post copy, plus hashtags), or the whole text for a plain-text draft."""

from __future__ import annotations

import json

BODY_FIELDS = ("caption", "post_copy", "translated_caption", "hook")


def _parse(text: str) -> dict | None:
    body = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        value = json.loads(body)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def compose(text: str, channel: str) -> tuple[str, tuple[str, ...]]:
    draft = _parse(text)
    if draft is None:
        return text.strip(), ()
    section = draft.get(channel)
    if not isinstance(section, dict):
        raise ValueError(f"This draft has nothing for {channel}")
    body = next((str(section[f]).strip() for f in BODY_FIELDS if section.get(f)), "")
    if not body:
        raise ValueError(f"The {channel} part of this draft has no caption or post text")
    tags = section.get("hashtags")
    if not isinstance(tags, list):
        tags = []
    hashtags = tuple(str(t).strip() for t in tags if str(t).strip())
    return body, hashtags
