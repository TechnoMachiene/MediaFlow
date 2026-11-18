"""The system prompt Gemini drafts with, loaded from a file so it can be edited without code.

Default: `prompts/system_prompt.md` (MGC Developments' Meta copywriter, with its fact sheet).
Set SYSTEM_PROMPT_FILE to use another file. The prompt is read once, when the process starts:
restart the API and the workers after editing it.

The prompt's text is part of every job's cache key (`app/media.py: make_cache_key`), so a
changed prompt never reuses drafts written under the old one.
"""

from __future__ import annotations

import os
from pathlib import Path

from app.errors import ConfigError

DEFAULT_PROMPT_FILE = Path(__file__).resolve().parents[1] / "prompts" / "system_prompt.md"


def load_system_prompt(path: str | Path | None = None) -> str:
    source = Path(path or os.environ.get("SYSTEM_PROMPT_FILE", "").strip() or DEFAULT_PROMPT_FILE)
    try:
        text = source.expanduser().read_text(encoding="utf-8").strip()
    except OSError as err:
        raise ConfigError(f"Cannot read the system prompt at {source}: {err}") from None
    if not text:
        raise ConfigError(f"The system prompt at {source} is empty")
    return text


SYSTEM_PROMPT = load_system_prompt()

# Sent with the media as the user's turn. The prompt expects a post brief (its part H); uploads
# don't carry one yet, so the model is told to work from the media and the fact sheet.
NO_BRIEF = (
    "No post brief was provided. Write the post for the attached media using only what is "
    "visible and the fact sheet; mark every missing fact as [NEEDS: ...] and list it under NOTES."
)
