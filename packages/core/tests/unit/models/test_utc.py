from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import BaseModel, ConfigDict, ValidationError

from docchat_core.models.utc import UTC_TIME_PATTERN, UtcDateTime, format_utc, to_utc

_PLUS_TWO = timezone(timedelta(hours=2))


class _Stamped(BaseModel):
    model_config = ConfigDict(extra="forbid")

    at: UtcDateTime


def test_to_utc_converts_and_cuts_to_milliseconds() -> None:
    local = datetime(2026, 10, 14, 11, 30, 0, 123456, tzinfo=_PLUS_TWO)

    result = to_utc(local)

    assert result == datetime(2026, 10, 14, 9, 30, 0, 123000, tzinfo=UTC)
    assert result.tzinfo is UTC


def test_to_utc_refuses_a_naive_datetime() -> None:
    naive = datetime(2026, 10, 14, 9, 30)  # noqa: DTZ001 - the refused input

    with pytest.raises(ValueError, match="naive"):
        to_utc(naive)


@pytest.mark.parametrize(
    ("value", "text"),
    [
        (datetime(2026, 10, 14, 9, 30, tzinfo=UTC), "2026-10-14T09:30:00.000Z"),
        (datetime(2026, 10, 21, 11, 15, 2, 123999, tzinfo=_PLUS_TWO), "2026-10-21T09:15:02.123Z"),
    ],
)
def test_format_utc_writes_milliseconds_and_z(value: datetime, text: str) -> None:
    assert format_utc(value) == text
    assert re.fullmatch(UTC_TIME_PATTERN, text)


def test_utc_datetime_parses_any_offset_and_serialises_z() -> None:
    model = _Stamped.model_validate({"at": "2026-10-14T11:30:00.123456+02:00"})

    assert model.at == datetime(2026, 10, 14, 9, 30, 0, 123000, tzinfo=UTC)
    assert model.model_dump(mode="json") == {"at": "2026-10-14T09:30:00.123Z"}
    assert model.model_dump() == {"at": datetime(2026, 10, 14, 9, 30, 0, 123000, tzinfo=UTC)}
    assert _Stamped.model_validate_json(model.model_dump_json()) == model


def test_utc_datetime_refuses_a_naive_time() -> None:
    with pytest.raises(ValidationError) as caught:
        _Stamped.model_validate({"at": "2026-10-14T09:30:00.000"})

    assert caught.value.errors()[0]["type"] == "timezone_aware"
