"""Component 1: frontend datetime -> UTC unix score."""
from __future__ import annotations

import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo


def to_score(iso: str, tz_name: str | None = None, *, min_lead_s: int = 0) -> float:
    dt = datetime.fromisoformat(iso)
    if dt.tzinfo is None:
        if not tz_name:
            raise ValueError("naive datetime: tz_name required")
        dt = dt.replace(tzinfo=ZoneInfo(tz_name))
    ts = dt.astimezone(timezone.utc).timestamp()
    if ts < time.time() + min_lead_s:
        raise ValueError("scheduled time is in the past")
    return ts
