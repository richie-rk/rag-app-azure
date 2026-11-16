"""Unit tests of the user search's paging (`11-api.md` sections 1.8 and 3.8.1)."""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from docchat_core.sql.repositories.users import UserPosition
from docchat_core.users.search import MAX_LIMIT, search_users


def user(name: str) -> SimpleNamespace:
    return SimpleNamespace(
        object_id=uuid4(),
        display_name=name,
        email="",
        last_known_role="User",
        first_seen_at=datetime(2026, 10, 1),  # noqa: DTZ001 - naive UTC from SQL
        last_seen_at=datetime(2026, 10, 2),  # noqa: DTZ001 - naive UTC from SQL
        is_member=None,
    )


async def test_a_full_page_carries_the_next_position(fake_sql: Any) -> None:
    rows = [user("Ada"), user("Bo"), user("Cy")]
    fake_sql.queue("search_users", rows)

    page = await search_users(fake_sql, q="a", project_id=None, after=None, limit=2)

    assert [item.display_name for item in page.items] == ["Ada", "Bo"]
    assert page.next_position == UserPosition("Bo", rows[1].object_id)
    assert fake_sql.calls[0].parameters["limit_plus_one"] == 3


async def test_the_last_page_has_no_position(fake_sql: Any) -> None:
    fake_sql.queue("search_users", [user("Cy")])
    after = UserPosition("Bo", uuid4())

    page = await search_users(fake_sql, q=None, project_id=None, after=after, limit=2)

    assert [item.display_name for item in page.items] == ["Cy"]
    assert page.next_position is None
    assert fake_sql.calls[0].parameters["after_display_name"] == "Bo"


@pytest.mark.parametrize("limit", [0, MAX_LIMIT + 1])
async def test_the_limit_lies_within_1_and_100(fake_sql: Any, limit: int) -> None:
    with pytest.raises(ValueError, match="limit"):
        await search_users(fake_sql, q=None, project_id=None, after=None, limit=limit)
