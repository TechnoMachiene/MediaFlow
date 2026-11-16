from pathlib import Path

import pytest

from app.api.audit import ACTIONS, CATEGORIES

pytestmark = pytest.mark.unit

DOC = Path(__file__).resolve().parents[2] / "docs" / "AUDIT.md"


def documented() -> dict[str, str]:
    block = DOC.read_text().split("<!-- actions:start -->")[1].split("<!-- actions:end -->")[0]
    rows = block.strip().splitlines()[2:]  # header and separator
    return {
        cells[0].strip("`"): cells[1]
        for cells in ([c.strip() for c in row.strip("|").split("|")] for row in rows)
    }


def test_documented_actions_match_the_code():
    assert documented() == ACTIONS


def test_every_action_belongs_to_a_category():
    assert {action.split(".")[0] for action in ACTIONS} == set(CATEGORIES)
