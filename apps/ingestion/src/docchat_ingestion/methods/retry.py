"""The wait a throttled or failing service asks for, carried to the orchestrator's backoff as
`retryAfterSeconds` (`14-ingestion.md` sections 4.7, 11.7 and 12.9; ADR 0050)."""

from __future__ import annotations

import math
from collections.abc import Mapping
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Final

MILLISECOND_HEADERS: Final = ("retry-after-ms", "x-ms-retry-after-ms")
SECOND_HEADER: Final = "retry-after"


def _seconds(value: str) -> float | None:
    try:
        return float(value)
    except ValueError:
        return None


def retry_after_seconds(headers: Mapping[str, str], *, now: datetime | None = None) -> int | None:
    """Whole seconds from `retry-after-ms`, `x-ms-retry-after-ms` or `retry-after` (seconds or
    an HTTP date), rounded up; `None` when the response names no usable wait."""
    for name in MILLISECOND_HEADERS:
        raw = headers.get(name)
        milliseconds = None if raw is None else _seconds(raw)
        if milliseconds is not None and milliseconds >= 0:
            return math.ceil(milliseconds / 1000)
    raw = headers.get(SECOND_HEADER)
    if raw is None:
        return None
    seconds = _seconds(raw)
    if seconds is None:
        try:
            moment = parsedate_to_datetime(raw)
        except (TypeError, ValueError):
            return None
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=UTC)
        seconds = (moment - (now or datetime.now(UTC))).total_seconds()
    return max(0, math.ceil(seconds))
