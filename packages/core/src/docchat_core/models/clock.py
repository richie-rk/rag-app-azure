"""The clock port (ADR 0015 item 7).

Code reads the wall clock and the monotonic clock only through a `Clock`, so leases, deadlines,
TTLs, backoff and cache lifetimes can be tested with `docchat_testing.clock.FakeClock` instead of
sleeping. Stored instants come from `now()`; elapsed-time measurements (rate buckets, timeouts)
use `monotonic()`, which never jumps when the wall clock is adjusted.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Protocol, runtime_checkable


@runtime_checkable
class Clock(Protocol):
    def now(self) -> datetime:
        """The current UTC wall-clock time, timezone-aware."""
        ...

    def monotonic(self) -> float:
        """Seconds on a clock that only moves forward; meaningful only as a difference."""
        ...


class SystemClock:
    """The production clock."""

    def now(self) -> datetime:
        return datetime.now(UTC)

    def monotonic(self) -> float:
        return time.monotonic()
