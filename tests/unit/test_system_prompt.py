import pytest

from app.errors import ConfigError
from app.system_prompt import DEFAULT_PROMPT_FILE, SYSTEM_PROMPT, load_system_prompt

pytestmark = pytest.mark.unit


def test_the_default_prompt_is_the_copywriter_file():
    assert DEFAULT_PROMPT_FILE.name == "system_prompt.md"
    assert SYSTEM_PROMPT == DEFAULT_PROMPT_FILE.read_text(encoding="utf-8").strip()
    assert SYSTEM_PROMPT.startswith("# ROLE")
    for part in ("# G. FACT SHEET", "# I. OUTPUT FORMAT", "FACEBOOK CAPTION:"):
        assert part in SYSTEM_PROMPT


def test_another_file_can_be_used(tmp_path, monkeypatch):
    other = tmp_path / "campaign.md"
    other.write_text("  Write captions.\n", encoding="utf-8")
    assert load_system_prompt(other) == "Write captions."
    monkeypatch.setenv("SYSTEM_PROMPT_FILE", str(other))
    assert load_system_prompt() == "Write captions."


def test_a_missing_or_empty_file_is_a_config_error(tmp_path):
    with pytest.raises(ConfigError, match="Cannot read"):
        load_system_prompt(tmp_path / "missing.md")
    empty = tmp_path / "empty.md"
    empty.write_text("   ", encoding="utf-8")
    with pytest.raises(ConfigError, match="empty"):
        load_system_prompt(empty)
