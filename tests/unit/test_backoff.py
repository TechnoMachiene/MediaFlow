import pytest

from app.limiter import GeminiLimiter, backoff_s

pytestmark = pytest.mark.unit


def test_backoff_bounds_and_growth():
    for attempt in range(1, 10):
        for _ in range(50):
            v = backoff_s(attempt, base=2.0, cap=60.0)
            assert 1.0 <= v <= 60.0
    assert max(backoff_s(1) for _ in range(200)) <= 4.0
    assert max(backoff_s(6) for _ in range(200)) > 20


def test_seconds_to_reset_in_range():
    assert 0 < GeminiLimiter.seconds_to_reset() <= 86400
