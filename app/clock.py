"""Injected clock so tests and /admin/run-due control time."""

from datetime import date, datetime, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo


class Clock(Protocol):
    def now(self) -> datetime:
        """Current time, timezone-aware UTC."""
        ...

    def today(self) -> date:
        """Current calendar date in the business timezone."""
        ...


class SystemClock:
    def __init__(self, tz: str = "Asia/Kolkata") -> None:
        self._tz = ZoneInfo(tz)

    def now(self) -> datetime:
        return datetime.now(ZoneInfo("UTC"))

    def today(self) -> date:
        return self.now().astimezone(self._tz).date()


class FixedClock:
    """A clock that only moves when told to."""

    def __init__(self, now: datetime, tz: str = "Asia/Kolkata") -> None:
        if now.tzinfo is None:
            raise ValueError("FixedClock needs a timezone-aware datetime")
        self._now = now
        self._tz = ZoneInfo(tz)

    def now(self) -> datetime:
        return self._now

    def today(self) -> date:
        return self._now.astimezone(self._tz).date()

    def set(self, now: datetime) -> None:
        if now.tzinfo is None:
            raise ValueError("FixedClock needs a timezone-aware datetime")
        self._now = now

    def advance(self, **kwargs: float) -> None:
        self._now += timedelta(**kwargs)


class OffsetClock:
    """Wraps the real (or test) clock with an adjustable forward offset.

    Used by POST /admin/clock so a demo can move past a quote's TTL without waiting. The
    offset lives in memory only: it is cleared by /admin/reset and by a restart.
    """

    def __init__(self, base: Clock, tz: str = "Asia/Kolkata") -> None:
        self._base = base
        self._tz = ZoneInfo(tz)
        self.offset = timedelta(0)

    def now(self) -> datetime:
        return self._base.now() + self.offset

    def today(self) -> date:
        return self.now().astimezone(self._tz).date()

    def advance(self, seconds: int) -> None:
        self.offset += timedelta(seconds=seconds)

    def reset(self) -> None:
        self.offset = timedelta(0)
