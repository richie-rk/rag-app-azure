"""What every table module shares: the `app` metadata, the column-type helpers, the literals that
filtered indexes and their statements keep in the text, and the database roles with the shape of
their grants (`80-data-model.md` sections 2.1 and 2.22; ADR 0025).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final, Literal

from sqlalchemy import Column, MetaData, literal_column
from sqlalchemy.dialects.mssql import DATETIME2, UNIQUEIDENTIFIER
from sqlalchemy.sql.elements import ColumnClause
from sqlalchemy.types import TypeEngine

SCHEMA: Final = "app"

metadata: Final = MetaData(schema=SCHEMA)
"""The one metadata of every application table; `db/migrations/env.py` compares it with the
migrated database, names included."""

# --- Literals kept in statement text ---------------------------------------------------------

BIT_TRUE: Final[ColumnClause[Any]] = literal_column("1")
"""`1` in `is_default = 1`: SQL Server matches a filtered index only to a constant in the text."""

BIT_FALSE: Final[ColumnClause[Any]] = literal_column("0")

SYSUTCDATETIME: Final[ColumnClause[Any]] = literal_column("SYSUTCDATETIME()")
"""The default of every time column: UTC with SQL Server's own clock."""

EMPTY_NVARCHAR: Final[ColumnClause[Any]] = literal_column("N''")

# Closed-set values: states, statuses, catalogue entries and media types. No quote can appear,
# so the literal can't end early.
_LITERAL_RE: Final = re.compile(r"^[A-Za-z0-9_./+-]{1,128}$")


def nvarchar_literal(value: str) -> ColumnClause[str]:
    """`N'<value>'` for a closed-set value (a state, a status), kept in the text so a predicate
    matches its filtered index. Only constants of the schema's closed sets pass; a value from a
    request is always a bound parameter."""
    if not _LITERAL_RE.fullmatch(value):
        raise ValueError("not a closed-set value")
    return literal_column(f"N'{value}'")


# --- Column helpers ---------------------------------------------------------------------------


# The dialect's type classes have untyped constructors.
_DATETIME2_3: Final[TypeEngine[datetime]] = DATETIME2(precision=3)  # type: ignore[no-untyped-call]


def utc_time(name: str, *, nullable: bool, defaulted: bool = False) -> Column[datetime]:
    """A `DATETIME2(3)` UTC time; `defaulted` adds the `SYSUTCDATETIME()` default."""
    return Column(
        name,
        _DATETIME2_3,
        nullable=nullable,
        server_default=SYSUTCDATETIME if defaulted else None,
    )


def actor(name: str, *, nullable: bool) -> Column[Any]:
    """An actor column: an Entra object ID with no foreign key, marked for the user-data
    export (`80-data-model.md` section 2.23)."""
    return Column(name, UNIQUEIDENTIFIER, nullable=nullable, info={"actor": True})


def in_list(column: str, values: tuple[str, ...]) -> str:
    """The body of a closed-set `CHECK`: `<column> IN (N'a', N'b')`."""
    for value in values:
        if not _LITERAL_RE.fullmatch(value):
            raise ValueError("not a closed-set value")
    return f"{column} IN ({', '.join(f"N'{value}'" for value in values)})"


# --- Roles and grants -------------------------------------------------------------------------

type DatabaseRole = Literal[
    "docchat_api",
    "docchat_ingestion",
    "docchat_config_operator",
    "docchat_conversation_reader",
    "docchat_user_data_operator",
]

DATABASE_ROLES: Final[tuple[DatabaseRole, ...]] = (
    "docchat_api",
    "docchat_ingestion",
    "docchat_config_operator",
    "docchat_conversation_reader",
    "docchat_user_data_operator",
)
"""The five roles the first migration creates; rights belong to them and to nothing else."""

type Verb = Literal["SELECT", "INSERT", "UPDATE", "DELETE"]

VERBS: Final[tuple[Verb, ...]] = ("SELECT", "INSERT", "UPDATE", "DELETE")

_LETTERS: Final[Mapping[str, Verb]] = {
    "S": "SELECT",
    "I": "INSERT",
    "U": "UPDATE",
    "D": "DELETE",
}


@dataclass(frozen=True)
class RoleRights:
    """One cell of the grant matrix: table-level verbs and, for the two column grants on
    `app.document_versions`, the columns of a column-level `UPDATE`."""

    verbs: frozenset[Verb]
    update_columns: tuple[str, ...] = ()


def rights(letters: str, *, update_columns: tuple[str, ...] = ()) -> RoleRights:
    """A cell written as in the matrix: `"SIU"` is SELECT, INSERT, UPDATE."""
    return RoleRights(
        verbs=frozenset(_LETTERS[letter] for letter in letters), update_columns=update_columns
    )


type GrantMatrix = Mapping[str, Mapping[DatabaseRole, RoleRights]]
"""Table name (in schema `app`) to each role's rights; a role absent from a row holds none."""
