from __future__ import annotations

from datetime import UTC, datetime

import pytest

from docchat_core.sql.tables.access import users
from docchat_testing.builders.rows import (
    build_row,
    compact_json,
    new_id,
    random_sha256,
    sha256_of,
    sql_now,
)


def test_build_row_applies_overrides_over_defaults() -> None:
    row = build_row(users, {"email": "a", "display_name": "b"}, {"email": "c"})
    assert row == {"email": "c", "display_name": "b"}


def test_build_row_refuses_unknown_columns() -> None:
    with pytest.raises(KeyError, match="users has no column emial"):
        build_row(users, {"email": "a"}, {"emial": "c"})
    with pytest.raises(KeyError, match="nickname"):
        build_row(users, {"nickname": "a"}, {})


def test_sql_now_is_naive_utc_to_the_millisecond() -> None:
    now = sql_now()
    assert now.tzinfo is None
    assert now.microsecond % 1000 == 0
    assert abs((datetime.now(UTC).replace(tzinfo=None) - now).total_seconds()) < 5


def test_value_helpers() -> None:
    assert compact_json({"a": [1, "é"]}) == '{"a":[1,"é"]}'
    assert len(sha256_of("x")) == 32
    assert sha256_of("x") == sha256_of("x")
    assert random_sha256() != random_sha256()
    assert new_id().version == 4
