"""Injectable clock so expiry logic is testable and evaluations use the snapshot time."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class FixedClock:
    def __init__(self, at: datetime):
        if at.tzinfo is None:
            raise ValueError("FixedClock requires a timezone-aware datetime")
        self.at = at

    def now(self) -> datetime:
        return self.at

    def advance(self, **kw) -> None:
        from datetime import timedelta

        self.at = self.at + timedelta(**kw)
