from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo


class SystemClock:
    def __init__(self, timezone: str = "UTC") -> None:
        self._tz = ZoneInfo(timezone)

    def now(self) -> datetime:
        return datetime.now(UTC).astimezone(self._tz)


class FixedClock:
    """Deterministic clock for tests; `advance` simulates the passage of time."""

    def __init__(self, now: datetime) -> None:
        if now.tzinfo is None:
            raise ValueError("FixedClock requires a timezone-aware datetime")
        self._now = now

    def now(self) -> datetime:
        return self._now

    def set(self, now: datetime) -> None:
        self._now = now
