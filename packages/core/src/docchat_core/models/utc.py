"""UTC instants with millisecond precision (ADR 0015 item 7).

Every stored or returned time is UTC, written as ISO 8601 with exactly three fractional digits
and `Z` (`2026-10-14T09:30:00.000Z`). SQL keeps `DATETIME2(3)`, so values are cut to the
millisecond when validated: a time read from one store then compares equal with the same time
read from another.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Final

from pydantic import AfterValidator, AwareDatetime, PlainSerializer

UTC_TIME_PATTERN: Final = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$"


def to_utc(value: datetime) -> datetime:
    """Convert an aware datetime to UTC, cut to the millisecond; refuse a naive one."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("a naive datetime has no defined instant")
    utc = value.astimezone(UTC)
    return utc.replace(microsecond=utc.microsecond - utc.microsecond % 1000)


def format_utc(value: datetime) -> str:
    """The ISO 8601 text with milliseconds and `Z` that JSON and Cosmos DB documents carry."""
    return to_utc(value).isoformat(timespec="milliseconds").replace("+00:00", "Z")


UtcDateTime = Annotated[
    AwareDatetime,
    AfterValidator(to_utc),
    PlainSerializer(format_utc, return_type=str, when_used="json"),
]
"""An aware datetime normalised to UTC milliseconds; JSON output is the `Z` form."""
