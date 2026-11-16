"""Unit tests of the `Retry-After` reading (`14-ingestion.md` sections 4.7, 11.7 and 12.9)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from docchat_ingestion.methods.retry import retry_after_seconds

NOW = datetime(2026, 10, 31, 12, 0, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        ({}, None),
        ({"retry-after": "20"}, 20),
        ({"retry-after": "1.2"}, 2),
        ({"retry-after-ms": "1500"}, 2),
        ({"x-ms-retry-after-ms": "200", "retry-after": "9"}, 1),
        ({"retry-after-ms": "soon", "retry-after": "3"}, 3),
        ({"retry-after": "Sat, 31 Oct 2026 12:00:30 GMT"}, 30),
        ({"retry-after": "Sat, 31 Oct 2026 11:00:00 GMT"}, 0),
        ({"retry-after": "Sat, 31 Oct 2026 12:00:30"}, 30),
        ({"retry-after": "later"}, None),
        ({"retry-after": "-4"}, 0),
    ],
)
def test_the_wait_a_response_names(headers: dict[str, str], expected: int | None) -> None:
    assert retry_after_seconds(headers, now=NOW) == expected


def test_an_http_date_is_read_against_the_current_time() -> None:
    assert retry_after_seconds({"retry-after": "Mon, 01 Jan 2001 00:00:00 GMT"}) == 0
