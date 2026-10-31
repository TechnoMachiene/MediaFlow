from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfoNotFoundError

import pytest

from app.timeutil import to_score

pytestmark = pytest.mark.unit

FUTURE = (datetime.now(timezone.utc) + timedelta(days=2)).replace(microsecond=0)


def test_offset_string():
    iso = FUTURE.astimezone(timezone(timedelta(hours=5))).isoformat()
    assert to_score(iso) == FUTURE.timestamp()


def test_z_suffix():
    assert to_score(FUTURE.isoformat().replace("+00:00", "Z")) == FUTURE.timestamp()


def test_naive_with_tz_name():
    local = (FUTURE + timedelta(hours=5)).replace(tzinfo=None).isoformat()
    assert to_score(local, "Asia/Karachi") == FUTURE.timestamp()


def test_naive_without_tz_rejected():
    with pytest.raises(ValueError, match="tz_name"):
        to_score("2099-01-01T10:00:00")


def test_past_rejected():
    with pytest.raises(ValueError, match="past"):
        to_score("2020-01-01T00:00:00+00:00")


def test_min_lead_enforced():
    soon = (datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat()
    with pytest.raises(ValueError):
        to_score(soon, min_lead_s=300)


def test_bad_zone_name():
    with pytest.raises(ZoneInfoNotFoundError):
        to_score("2099-01-01T10:00:00", "Mars/Olympus")


def test_dst_gap_does_not_crash():
    # Europe/London spring-forward 2099-03-29: 01:30 does not exist
    assert isinstance(to_score("2099-03-29T01:30:00", "Europe/London"), float)
