from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta, timezone

from sqlalchemy import bindparam, create_engine, select

from docchat_core.sql.repositories.common import LIMIT_PLUS_ONE, page_keys, top_rows, utc
from docchat_core.sql.tables.access import projects

DIALECT = create_engine("mssql+pyodbc://").dialect


def test_a_page_binds_its_row_count_in_parentheses() -> None:
    compiled = top_rows(select(projects.c.id).order_by(projects.c.name)).compile(dialect=DIALECT)

    assert compiled.string.startswith("SELECT TOP (?) app.projects.id")
    assert compiled.positiontup == [LIMIT_PLUS_ONE]
    assert compiled.construct_params({LIMIT_PLUS_ONE: 51}) == {LIMIT_PLUS_ONE: 51}


def test_one_text_serves_every_page_size() -> None:
    statement = top_rows(select(projects.c.id))
    first = statement.compile(dialect=DIALECT)
    second = statement.compile(dialect=DIALECT)
    assert first.string == second.string
    assert "51" not in first.string


def test_a_pages_keys_keep_their_order_inside_a_derived_table() -> None:
    page = page_keys(
        select(projects.c.id).where(projects.c.status == bindparam("status")),
        projects.c.created_at.desc(),
        projects.c.id.desc(),
    ).subquery("page")
    compiled = select(page.c.id).order_by(page.c.id).compile(dialect=DIALECT)

    assert re.sub(r"\s+", " ", compiled.string) == (
        "SELECT page.id FROM (SELECT TOP (?) app.projects.id AS id FROM app.projects "
        "WHERE app.projects.status = ? "
        "ORDER BY app.projects.created_at DESC , app.projects.id DESC ) AS page ORDER BY page.id"
    )
    assert compiled.positiontup == [LIMIT_PLUS_ONE, "status"]


def test_a_sql_time_is_utc() -> None:
    naive = datetime(2026, 10, 31, 12, 0, 0, 123000)  # noqa: DTZ001 - SQL returns naive UTC
    assert utc(naive) == datetime(2026, 10, 31, 12, 0, 0, 123000, tzinfo=UTC)
    shifted = datetime(2026, 10, 31, 14, 0, tzinfo=timezone(timedelta(hours=2)))
    assert utc(shifted) == datetime(2026, 10, 31, 12, 0, tzinfo=UTC)
    assert utc(shifted).tzinfo is UTC
