from datetime import date, datetime, timezone

import pytest

from app.clock import FixedClock


def test_today_uses_business_timezone():
    # 20:00 UTC on 1 Oct is already 2 Oct in India.
    clock = FixedClock(datetime(2026, 10, 1, 20, 0, tzinfo=timezone.utc))
    assert clock.today() == date(2026, 10, 2)


def test_advance_moves_time():
    clock = FixedClock(datetime(2026, 10, 1, tzinfo=timezone.utc))
    clock.advance(days=1, seconds=1)
    assert clock.now() == datetime(2026, 10, 2, 0, 0, 1, tzinfo=timezone.utc)


def test_naive_datetime_rejected():
    with pytest.raises(ValueError):
        FixedClock(datetime(2026, 10, 1))
