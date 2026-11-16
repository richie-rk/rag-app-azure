"""What the request-path repositories share: the bound `TOP (:limit_plus_one)` of a keyset page,
a page as a derived table, the integer literals a `CASE` returns, and UTC times from `DATETIME2`
columns (`11-api.md` sections 1.4.4 and 1.8; `80-data-model.md` section 2.1).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Final

from sqlalchemy import Integer, Select, bindparam, literal_column
from sqlalchemy.sql.elements import ColumnClause, ColumnElement, Grouping

LIMIT_PLUS_ONE: Final = "limit_plus_one"
"""The bound parameter of a page's row count: the operation's `limit` plus one, so the extra
row tells whether a next page exists."""

ZERO: Final[ColumnClause[Any]] = literal_column("0")
ONE: Final[ColumnClause[Any]] = literal_column("1")

# SQLAlchemy renders `.limit()` on SQL Server as a literal `TOP n`, which makes every page size
# its own statement text and plan. `TOP (?)` keeps one constant text per statement shape with the
# row count as a bound parameter, which SQL Server accepts in parentheses.
_TOP: Final[ColumnClause[Any]] = literal_column("TOP")
_TOP_ROWS: Final = Grouping(bindparam(LIMIT_PLUS_ONE, type_=Integer))


def top_rows[S: Select[Any]](statement: S) -> S:
    """`statement` as `SELECT TOP (:limit_plus_one) …`."""
    return statement.prefix_with(_TOP, _TOP_ROWS)


# SQLAlchemy leaves the `ORDER BY` of a subquery out on SQL Server unless it renders the `TOP`
# itself, which it does only as a literal; so a page writes its order after the statement, as
# `top_rows` writes the `TOP` before its columns.
_ORDER_BY: Final[ColumnClause[Any]] = literal_column("ORDER BY")
_COMMA: Final[ColumnClause[Any]] = literal_column(",")

LOOP_JOIN: Final = "OPTION (LOOP JOIN)"
"""The statement hint of a page that fetches its rows' other columns by key: each of the page's
at most `limit_plus_one` keys is joined by a seek, never by a scan of the table it came from."""


def page_keys[S: Select[Any]](statement: S, *order: ColumnElement[Any]) -> S:
    """`statement` as `SELECT TOP (:limit_plus_one) … ORDER BY <order>`, to use as a derived
    table: a keyset page read in order from the index that holds the columns it selects
    (`80-data-model.md` section 2), so the `TOP` reads that index directly and the page reads at
    most `limit_plus_one` of its rows however large the table grows (`84-testing.md` section 2.11
    and T-067); the statement around it joins the page's other tables."""
    terms: list[ColumnElement[Any]] = []
    for term in order:
        terms.extend((_COMMA, term) if terms else (term,))
    return top_rows(statement).suffix_with(_ORDER_BY, *terms)


def utc(value: datetime) -> datetime:
    """A `DATETIME2` value read from SQL, which holds UTC without an offset, as an aware UTC
    datetime."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
