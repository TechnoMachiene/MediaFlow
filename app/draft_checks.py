"""Checks a person reads before approving a draft. Gemini's drafts are suggestions: it can
get names, prices, dates and places wrong, so the app points at what to verify and refuses
to approve a draft that still has gaps.

    block  [NEEDS: ...] placeholders left in (the prompt writes these for missing facts),
           an Instagram caption over Instagram's 2,200 characters
    warn   flagged phrases (prompts/flagged_phrases.txt), Unicode "bold" or "italic" letters,
           more than one phone number, more hashtags than the house rules allow

Approving also needs the reviewer to confirm they checked the facts (`checked: true`), which
the audit trail records.
"""

from __future__ import annotations

import json
import os
import re
from functools import lru_cache
from pathlib import Path

DEFAULT_PHRASES_FILE = Path(__file__).resolve().parents[1] / "prompts" / "flagged_phrases.txt"
NEEDS = re.compile(r"\[\s*NEEDS\s*:[^\]]*\]?", re.IGNORECASE)
# Mathematical Alphanumeric Symbols: the "𝐁𝐨𝐥𝐝" letters screen readers can't read.
STYLED = re.compile("[\U0001D400-\U0001D7FF]")
PHONE = re.compile(r"(?<![\w+])\+?\d[\d \-()]{8,}\d")
HASHTAG = re.compile(r"#\w+")
HASHTAG_LIMITS = {"instagram": 5, "facebook": 3}  # the copywriter prompt's rules (A, E)
IG_CAPTION_MAX = 2200


@lru_cache(maxsize=4)
def _phrases(path: str) -> tuple[str, ...]:
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return ()
    return tuple(p.strip().lower() for p in lines if p.strip() and not p.lstrip().startswith("#"))


def flagged_phrases() -> tuple[str, ...]:
    return _phrases(os.environ.get("FLAGGED_PHRASES_FILE", "").strip()
                    or str(DEFAULT_PHRASES_FILE))


def _fields(text: str) -> list[tuple[str, str]]:
    """(path, text) for every piece of copy: "instagram.caption", or "draft" for plain text."""
    body = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        data = json.loads(body)
    except ValueError:
        return [("draft", text)]
    if not isinstance(data, dict):
        return [("draft", text)]
    out = []
    for section, fields in data.items():
        if not isinstance(fields, dict):
            continue
        for name, value in fields.items():
            if isinstance(value, list):
                value = "\n".join(str(v) for v in value)
            if isinstance(value, str) and value.strip():
                out.append((f"{section}.{name}", value))
    return out


def check(text: str) -> list[dict]:
    issues: list[dict] = []
    fields = _fields(text)
    phrases = flagged_phrases()
    phones: set[str] = set()
    for path, value in fields:
        if path.endswith("notes"):  # the team's own notes may name what's missing
            continue
        for gap in dict.fromkeys(m.group(0) for m in NEEDS.finditer(value)):
            issues.append({"level": "block", "field": path,
                           "message": f"Fill in {gap} before approving"})
        if STYLED.search(value):
            issues.append({"level": "warn", "field": path,
                           "message": "Uses Unicode bold or italic letters, which screen "
                                      "readers and search can't read"})
        low = value.lower()
        for phrase in phrases:
            if re.search(rf"(?<!\w){re.escape(phrase)}(?!\w)", low):
                issues.append({"level": "warn", "field": path,
                               "message": f"Says \"{phrase}\": check it's allowed and true"})
        # The same number written two ways (+92 304..., 0304...) is one number.
        phones |= {re.sub(r"\D", "", p)[-10:] for p in PHONE.findall(value)}
        section = path.split(".", 1)[0]
        if path.endswith(("caption", "hashtags")) and section in HASHTAG_LIMITS:
            tags = (len([t for t in value.splitlines() if t.strip()]) if path.endswith("hashtags")
                    else len(HASHTAG.findall(value)))
            if tags > HASHTAG_LIMITS[section]:
                issues.append({"level": "warn", "field": path,
                               "message": f"{tags} hashtags; the house rule for {section} is "
                                          f"at most {HASHTAG_LIMITS[section]}"})
        if path == "instagram.caption" and len(value) > IG_CAPTION_MAX:
            issues.append({"level": "block", "field": path,
                           "message": f"Instagram captions are at most {IG_CAPTION_MAX} "
                                      f"characters; this one has {len(value)}"})
    if len(phones) > 1:
        issues.append({"level": "warn", "field": None,
                       "message": f"{len(phones)} different phone numbers; use one, exactly as "
                                  "on the fact sheet"})
    return issues


def blocking(issues: list[dict]) -> list[dict]:
    return [i for i in issues if i["level"] == "block"]
