import re
from pathlib import Path

import pytest

from app.api.lifecycle import (
    ACTIVE_STATUSES,
    ALL_STATUSES,
    TRANSITIONS,
    allowed_from,
    can_transition,
    set_status,
)

pytestmark = pytest.mark.unit

DOC = Path(__file__).resolve().parents[2] / "docs" / "JOB_LIFECYCLE.md"


def documented_transitions() -> dict[str, set[str]]:
    text = DOC.read_text()
    block = text.split("<!-- transitions:start -->")[1].split("<!-- transitions:end -->")[0]
    table: dict[str, set[str]] = {}
    for line in block.strip().splitlines()[2:]:  # skip header and separator
        source, targets = (cell.strip() for cell in line.strip("|").split("|"))
        table[source.strip("`")] = set(re.findall(r"`(\w+)`", targets))
    return table


def test_documented_transitions_match_the_code():
    assert documented_transitions() == {k: set(v) for k, v in TRANSITIONS.items()}


def test_final_states_never_change():
    for final in ("done", "cancelled"):
        assert not any(can_transition(final, new) for new in ALL_STATUSES - {final})


def test_failed_jobs_accept_only_a_real_result():
    for state in ("failed", "dead"):
        assert {new for new in ALL_STATUSES if can_transition(state, new)} == {state, "done"}


def test_active_jobs_can_reach_every_state():
    for state in ACTIVE_STATUSES:
        assert all(can_transition(state, new) for new in ALL_STATUSES)


def test_allowed_from_is_the_inverse_of_the_table():
    for new in ALL_STATUSES:
        assert allowed_from(new) == {new} | {old for old in TRANSITIONS if new in TRANSITIONS[old]}
    assert allowed_from("processing") == ACTIVE_STATUSES
    assert allowed_from("done") == ACTIVE_STATUSES | {"failed", "dead", "done"}


def test_set_status_refuses_forbidden_moves():
    class Row:
        status = "done"

    row = Row()
    assert not set_status(row, "processing") and row.status == "done"
    row.status = "queued"
    assert set_status(row, "processing") and row.status == "processing"
    assert not set_status(row, "unknown")  # Redis forgetting a job is never written down
