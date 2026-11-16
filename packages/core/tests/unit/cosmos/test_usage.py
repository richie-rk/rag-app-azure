from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from pydantic import ValidationError

from docchat_core.cosmos.usage import (
    USAGE_DOCUMENT_MODELS,
    DownloadLease,
    DownloadRateWindow,
    RateWindow,
    StreamLease,
    UsageDayCounter,
    UsageDocument,
    UsageMonthCounter,
    WriteRateWindow,
    day_counter_id,
    download_lease_id,
    download_rate_window_id,
    epoch_minute,
    month_counter_id,
    rate_window_id,
    stream_lease_id,
    write_rate_window_id,
)

_OID = UUID("0b8f3c1e-6a2d-4f7b-9c55-1d2e3f405162")
_SESSION_ID = UUID("b6061e08-59bc-576e-a78d-b2d375c588e7")
_TURN_ID = UUID("445c1c2a-be4c-5d17-a92d-c4efcb094740")
_NOW = datetime(2026, 10, 14, 9, 30, 15, tzinfo=UTC)
_INT_MAX = 2_147_483_647


def test_epoch_minutes() -> None:
    assert epoch_minute(datetime(1970, 1, 1, tzinfo=UTC)) == 0
    assert epoch_minute(datetime(1970, 1, 1, 0, 1, 59, 999_999, tzinfo=UTC)) == 1
    assert epoch_minute(_NOW) == 29_866_170 == int(_NOW.timestamp()) // 60
    with pytest.raises(ValueError, match="naive"):
        epoch_minute(datetime(2026, 10, 14))  # noqa: DTZ001 - naive on purpose


def test_the_id_forms() -> None:
    assert day_counter_id(date(2026, 1, 5)) == "day:2026-01-05"
    assert month_counter_id(date(2026, 1, 5)) == "month:2026-01"
    assert rate_window_id(29_339_850) == "rate:29339850"
    assert download_rate_window_id(29_339_850) == "dlrate:29339850"
    assert write_rate_window_id(29_339_850) == "wrate:29339850"
    assert stream_lease_id(1) == "lease:1"
    assert stream_lease_id(200) == "lease:200"
    assert download_lease_id(10) == "dllease:10"


@pytest.mark.parametrize(
    "make",
    [lambda: stream_lease_id(0), lambda: stream_lease_id(201), lambda: download_lease_id(11)],
)
def test_lease_slots_have_their_ranges(make: Any) -> None:
    with pytest.raises(ValueError, match="slot"):
        make()


def _day(**changes: Any) -> UsageDayCounter:
    values: dict[str, Any] = {
        "id": "day:2026-10-14",
        "user_id": _OID,
        "period": "2026-10-14",
        "created_at": _NOW,
        "updated_at": _NOW,
    }
    return UsageDayCounter(**(values | changes))


def _month(**changes: Any) -> UsageMonthCounter:
    values: dict[str, Any] = {
        "id": "month:2026-10",
        "user_id": _OID,
        "period": "2026-10",
        "created_at": _NOW,
        "updated_at": _NOW,
    }
    return UsageMonthCounter(**(values | changes))


def _lease(**changes: Any) -> StreamLease:
    values: dict[str, Any] = {
        "id": "lease:1",
        "user_id": _OID,
        "turn_id": _TURN_ID,
        "session_id": _SESSION_ID,
        "acquired_at": _NOW,
        "expires_at": _NOW + timedelta(seconds=210),
        "ttl": 210,
    }
    return StreamLease(**(values | changes))


def _download_lease(**changes: Any) -> DownloadLease:
    values: dict[str, Any] = {
        "id": "dllease:1",
        "user_id": _OID,
        "turn_id": _TURN_ID,
        "session_id": _SESSION_ID,
        "acquired_at": _NOW,
        "expires_at": _NOW + timedelta(seconds=300),
    }
    return DownloadLease(**(values | changes))


def test_a_day_counter() -> None:
    item = _day(prompt_tokens=10, completion_tokens=5, total_tokens=15, calls=1).to_item()

    assert item == {
        "id": "day:2026-10-14",
        "userId": str(_OID),
        "period": "2026-10-14",
        "promptTokens": 10,
        "completionTokens": 5,
        "reasoningTokens": 0,
        "cachedTokens": 0,
        "cacheWriteTokens": 0,
        "totalTokens": 15,
        "estimatedCostMicros": 0,
        "calls": 1,
        "unpricedCalls": 0,
        "estimatedCalls": 0,
        "createdAt": "2026-10-14T09:30:15.000Z",
        "updatedAt": "2026-10-14T09:30:15.000Z",
        "type": "usage_day",
        "ttl": 3_456_000,
    }


def test_a_month_counter() -> None:
    item = _month().to_item()

    assert (item["id"], item["type"], item["period"], item["ttl"]) == (
        "month:2026-10",
        "usage_month",
        "2026-10",
        34_560_000,
    )


@pytest.mark.parametrize(
    ("make", "changes"),
    [
        (_day, {"id": "day:2026-10-15"}),
        (_day, {"period": "2026-02-30", "id": "day:2026-02-30"}),
        (_day, {"period": "2026-10", "id": "day:2026-10"}),
        (_day, {"ttl": 180}),
        (_day, {"total_tokens": -1}),
        (_month, {"id": "month:2026-11"}),
        (_month, {"period": "2026-13", "id": "month:2026-13"}),
        (_month, {"period": "2026-10-14", "id": "month:2026-10-14"}),
        (_lease, {"id": "lease:0"}),
        (_lease, {"id": "lease:201"}),
        (_lease, {"id": "lease:01"}),
        (_lease, {"id": "dllease:1"}),
        (_lease, {"expires_at": _NOW + timedelta(seconds=209)}),
        (_download_lease, {"id": "dllease:11"}),
        (_download_lease, {"turn_id": None}),
        (_download_lease, {"expires_at": _NOW + timedelta(seconds=210)}),
    ],
)
def test_ids_periods_and_lifetimes_are_validated(make: Any, changes: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        make(**changes)


@pytest.mark.parametrize(
    ("model", "prefix", "type_name"),
    [
        (RateWindow, "rate", "rate_window"),
        (DownloadRateWindow, "dlrate", "download_rate_window"),
        (WriteRateWindow, "wrate", "write_rate_window"),
    ],
)
def test_minute_windows(model: type[RateWindow], prefix: str, type_name: str) -> None:
    window = model(id=f"{prefix}:29339850", user_id=_OID, count=1)

    assert window.to_item() == {
        "id": f"{prefix}:29339850",
        "userId": str(_OID),
        "count": 1,
        "ttl": 180,
        "type": type_name,
    }
    for wrong in ({"count": 0}, {"id": f"{prefix}:x"}, {"id": "other:29339850"}):
        with pytest.raises(ValidationError):
            model.model_validate(window.model_dump() | wrong)


def test_a_stream_lease_lives_as_long_as_the_turn_deadline() -> None:
    item = _lease().to_item()

    assert item["type"] == "stream_lease"
    assert item["expiresAt"] == "2026-10-14T09:33:45.000Z"
    assert item["ttl"] == 210


def test_a_download_lease_of_an_admin_pdf_names_no_turn() -> None:
    item = _download_lease(turn_id=None, session_id=None).to_item()

    assert (item["type"], item["turnId"], item["sessionId"], item["ttl"]) == (
        "download_lease",
        None,
        None,
        300,
    )


def test_the_seven_types_have_distinct_prefixes_and_type_names() -> None:
    types = {model.model_fields["type"].default for model in USAGE_DOCUMENT_MODELS}

    assert types == {
        "usage_day",
        "usage_month",
        "rate_window",
        "stream_lease",
        "download_rate_window",
        "download_lease",
        "write_rate_window",
    }


def test_no_usage_document_carries_a_schema_version() -> None:
    for model in USAGE_DOCUMENT_MODELS:
        assert "schema_version" not in model.model_fields
        assert "ttl" in model.model_fields


def test_t093_every_usage_document_at_its_maximums_fits_one_kilobyte() -> None:
    counters = dict.fromkeys(
        [
            "prompt_tokens",
            "completion_tokens",
            "reasoning_tokens",
            "cached_tokens",
            "cache_write_tokens",
            "total_tokens",
            "calls",
            "unpriced_calls",
            "estimated_calls",
        ],
        _INT_MAX,
    )
    documents: list[UsageDocument] = [
        _day(**counters, estimated_cost_micros=10**15),
        _month(**counters, estimated_cost_micros=10**15),
        RateWindow(id="rate:99999999999", user_id=_OID, count=_INT_MAX),
        DownloadRateWindow(id="dlrate:99999999999", user_id=_OID, count=_INT_MAX),
        WriteRateWindow(id="wrate:99999999999", user_id=_OID, count=_INT_MAX),
        _lease(id="lease:200"),
        _download_lease(id="dllease:10"),
    ]

    for document in documents:
        assert document.json_size() <= document.max_bytes == 1_024
        document.to_item()
