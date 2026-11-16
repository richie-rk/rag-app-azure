"""The `usage` container's documents: per-user token counters, rate windows and leases
(`80-data-model.md` sections 3.9 and 3.10; `12-chat-pipeline.md` section 3.1; ADR 0015 item 6).

Every document lives in the partition `userId` = the caller's `oid`, carries its own `ttl` in
seconds and is at most 1 KB. Computed IDs keep every increment a point operation, and their
prefixes keep the seven types apart in one partition. The documents carry no `schemaVersion`:
each lives at most 400 days, and a changed shape takes a new `type` and `id` prefix.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta
from typing import Annotated, ClassVar, Final, Literal, Self

from pydantic import Field, model_validator

from docchat_core.cosmos.base import CosmosDocument
from docchat_core.models import CanonicalUuid, Count, UtcDateTime

USAGE_MAX_BYTES: Final = 1_024
DAY_COUNTER_TTL_SECONDS: Final = 3_456_000
"""40 days after the last write."""
MONTH_COUNTER_TTL_SECONDS: Final = 34_560_000
"""400 days after the last write."""
WINDOW_TTL_SECONDS: Final = 180
"""A one-minute window is read only in its own and the next minute."""
STREAM_LEASE_SLOTS_MAX: Final = 200
STREAM_LEASE_OFFSET_SECONDS: Final = 30
"""A stream lease lives `chat.max_turn_seconds` plus this, the turn deadline's offset."""
DOWNLOAD_LEASE_SLOTS_MAX: Final = 10
DOWNLOAD_LEASE_SECONDS: Final = 300

_EPOCH: Final = datetime(1970, 1, 1, tzinfo=UTC)
_DAY_PERIOD: Final = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_MONTH_PERIOD: Final = re.compile(r"^\d{4}-\d{2}$")

NonNegativeInt = Annotated[int, Field(ge=0)]


def epoch_minute(instant: datetime) -> int:
    """Whole minutes since 1970-01-01T00:00:00Z."""
    if instant.tzinfo is None:
        raise ValueError("a naive datetime has no defined instant")
    return (instant - _EPOCH) // timedelta(minutes=1)


def day_counter_id(day: date) -> str:
    """`day:{YYYY-MM-DD}` of a UTC date."""
    return f"day:{day.isoformat()}"


def month_counter_id(day: date) -> str:
    """`month:{YYYY-MM}` of the UTC month that holds `day`."""
    return f"month:{day.year:04d}-{day.month:02d}"


def rate_window_id(minute: int) -> str:
    return f"rate:{minute}"


def download_rate_window_id(minute: int) -> str:
    return f"dlrate:{minute}"


def write_rate_window_id(minute: int) -> str:
    return f"wrate:{minute}"


def stream_lease_id(slot: int) -> str:
    if not 1 <= slot <= STREAM_LEASE_SLOTS_MAX:
        raise ValueError(f"a stream lease slot is 1 to {STREAM_LEASE_SLOTS_MAX}")
    return f"lease:{slot}"


def download_lease_id(slot: int) -> str:
    if not 1 <= slot <= DOWNLOAD_LEASE_SLOTS_MAX:
        raise ValueError(f"a download lease slot is 1 to {DOWNLOAD_LEASE_SLOTS_MAX}")
    return f"dllease:{slot}"


def _id_number(document_id: str, prefix: str) -> int | None:
    # The number after `<prefix>:` in its one decimal spelling, or None.
    head, _, number = document_id.partition(":")
    if head != prefix or not number.isascii() or not number.isdigit():
        return None
    value = int(number)
    return value if number == str(value) else None


def _valid_period(period: str, pattern: re.Pattern[str], suffix: str) -> bool:
    if not pattern.fullmatch(period):
        return False
    try:
        date.fromisoformat(period + suffix)
    except ValueError:
        return False
    return True


class UsageDocument(CosmosDocument):
    """Fields every `usage` document has."""

    max_bytes: ClassVar[int] = USAGE_MAX_BYTES

    id: str
    user_id: CanonicalUuid


class _TokenCounter(UsageDocument):
    """The fields of the day and month counters (`12-chat-pipeline.md` section 15.6)."""

    period: str
    prompt_tokens: Count = 0
    completion_tokens: Count = 0
    reasoning_tokens: Count = 0
    cached_tokens: Count = 0
    cache_write_tokens: Count = 0
    total_tokens: Count = 0
    """Prompt plus completion tokens, compared with the token quota."""
    estimated_cost_micros: NonNegativeInt = 0
    calls: Count = 0
    unpriced_calls: Count = 0
    estimated_calls: Count = 0
    created_at: UtcDateTime
    updated_at: UtcDateTime


class UsageDayCounter(_TokenCounter):
    """One per user per UTC day."""

    type: Literal["usage_day"] = "usage_day"
    ttl: Literal[3_456_000] = DAY_COUNTER_TTL_SECONDS

    @model_validator(mode="after")
    def _identity(self) -> Self:
        if not _valid_period(self.period, _DAY_PERIOD, "") or self.id != f"day:{self.period}":
            raise ValueError("a day counter's id is day:{YYYY-MM-DD} of its period")
        return self


class UsageMonthCounter(_TokenCounter):
    """One per user per UTC month."""

    type: Literal["usage_month"] = "usage_month"
    ttl: Literal[34_560_000] = MONTH_COUNTER_TTL_SECONDS

    @model_validator(mode="after")
    def _identity(self) -> Self:
        if not _valid_period(self.period, _MONTH_PERIOD, "-01") or self.id != (
            f"month:{self.period}"
        ):
            raise ValueError("a month counter's id is month:{YYYY-MM} of its period")
        return self


class _MinuteWindow(UsageDocument):
    """A per-minute request count; `id` is `<prefix>:{epochMinute}`."""

    id_prefix: ClassVar[str]

    count: Annotated[int, Field(ge=1)]
    ttl: Literal[180] = WINDOW_TTL_SECONDS

    @model_validator(mode="after")
    def _identity(self) -> Self:
        if _id_number(self.id, self.id_prefix) is None:
            raise ValueError(f"the id is {self.id_prefix}:{{epochMinute}}")
        return self


class RateWindow(_MinuteWindow):
    """Counted chat requests of one user in one UTC minute."""

    id_prefix: ClassVar[str] = "rate"
    type: Literal["rate_window"] = "rate_window"


class DownloadRateWindow(_MinuteWindow):
    """Counted cited-file requests of one user in one UTC minute."""

    id_prefix: ClassVar[str] = "dlrate"
    type: Literal["download_rate_window"] = "download_rate_window"


class WriteRateWindow(_MinuteWindow):
    """Counted write requests of one user in one UTC minute; the chat pipeline never reads it."""

    id_prefix: ClassVar[str] = "wrate"
    type: Literal["write_rate_window"] = "write_rate_window"


class StreamLease(UsageDocument):
    """One running stream; it expires `chat.max_turn_seconds` + 30 s after it was acquired,
    the same offset as the turn's deadline."""

    type: Literal["stream_lease"] = "stream_lease"
    turn_id: CanonicalUuid
    session_id: CanonicalUuid
    acquired_at: UtcDateTime
    expires_at: UtcDateTime
    ttl: Annotated[int, Field(ge=1)]

    @model_validator(mode="after")
    def _identity(self) -> Self:
        slot = _id_number(self.id, "lease")
        if slot is None or not 1 <= slot <= STREAM_LEASE_SLOTS_MAX:
            raise ValueError(f"the id is lease:{{slot}}, slot 1 to {STREAM_LEASE_SLOTS_MAX}")
        if self.expires_at != self.acquired_at + timedelta(seconds=self.ttl):
            raise ValueError("a lease expires ttl seconds after it was acquired")
        return self


class DownloadLease(UsageDocument):
    """One running citation download; `turnId` and `sessionId` are both null for an
    `adminGetDocumentPdf` download."""

    type: Literal["download_lease"] = "download_lease"
    turn_id: CanonicalUuid | None
    session_id: CanonicalUuid | None
    acquired_at: UtcDateTime
    expires_at: UtcDateTime
    ttl: Literal[300] = DOWNLOAD_LEASE_SECONDS

    @model_validator(mode="after")
    def _identity(self) -> Self:
        slot = _id_number(self.id, "dllease")
        if slot is None or not 1 <= slot <= DOWNLOAD_LEASE_SLOTS_MAX:
            raise ValueError(f"the id is dllease:{{slot}}, slot 1 to {DOWNLOAD_LEASE_SLOTS_MAX}")
        if (self.turn_id is None) != (self.session_id is None):
            raise ValueError("a download lease names both the turn and the session, or neither")
        if self.expires_at != self.acquired_at + timedelta(seconds=self.ttl):
            raise ValueError("a lease expires ttl seconds after it was acquired")
        return self


USAGE_DOCUMENT_MODELS: Final[tuple[type[UsageDocument], ...]] = (
    UsageDayCounter,
    UsageMonthCounter,
    RateWindow,
    StreamLease,
    DownloadRateWindow,
    DownloadLease,
    WriteRateWindow,
)
