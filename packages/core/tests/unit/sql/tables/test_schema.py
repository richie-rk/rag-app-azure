"""Unit tests of the shared table helpers, and of the metadata as a whole against
`80-data-model.md` sections 2.1 to 2.20 and 2.23 (T-084)."""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path

import pytest
from sqlalchemy import CheckConstraint, ForeignKeyConstraint, Index, UniqueConstraint
from sqlalchemy.dialects import mssql

from docchat_core.sql.tables import ACTOR_COLUMNS, DATABASE_ROLES, GRANT_MATRIX, metadata
from docchat_core.sql.tables.schema import (
    BIT_TRUE,
    actor,
    in_list,
    nvarchar_literal,
    rights,
    utc_time,
)

type SpecTable = Callable[[str, str], list[list[str]]]

_NAME_RE = re.compile(r"`((?:PK|UQ|UX|FK|CK|IX)_[A-Za-z0-9_]+)`")

# SQLAlchemy's dialect classes have untyped constructors.
DIALECT = mssql.dialect()  # type: ignore[no-untyped-call]


def spec_text() -> str:
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "docs" / "spec" / "80-data-model.md"
        if candidate.is_file():
            return candidate.read_text(encoding="utf-8")
    raise FileNotFoundError("80-data-model.md")


def metadata_names() -> set[str]:
    names: set[str] = set()
    for table in metadata.tables.values():
        for constraint in table.constraints:
            assert isinstance(constraint.name, str), (table.name, constraint)
            names.add(constraint.name)
        for index in table.indexes:
            assert isinstance(index.name, str)
            names.add(index.name)
    return names


def test_nvarchar_literal() -> None:
    compiled = nvarchar_literal("pending").compile(dialect=DIALECT)
    assert str(compiled) == "N'pending'"
    for bad in ("it's", "a b", "x)--", ""):
        with pytest.raises(ValueError, match="closed-set"):
            nvarchar_literal(bad)


def test_in_list() -> None:
    assert in_list("state", ("a", "b_c")) == "state IN (N'a', N'b_c')"
    with pytest.raises(ValueError, match="closed-set"):
        in_list("state", ("ok", "x'"))


def test_rights_letters() -> None:
    cell = rights("SIUD", update_columns=("state",))
    assert cell.verbs == {"SELECT", "INSERT", "UPDATE", "DELETE"}
    assert cell.update_columns == ("state",)


def test_column_helpers() -> None:
    column = utc_time("created_at", nullable=False, defaulted=True)
    assert str(column.type.compile(dialect=DIALECT)) == "DATETIME2(3)"
    assert column.server_default is not None
    assert utc_time("t", nullable=True).server_default is None
    marked = actor("created_by", nullable=False)
    assert marked.info == {"actor": True}
    assert BIT_TRUE.name == "1"


def test_eighteen_tables_in_schema_app() -> None:
    assert {table.schema for table in metadata.tables.values()} == {"app"}
    assert len(metadata.tables) == 18


def test_every_name_of_the_data_model_is_declared() -> None:
    text = spec_text()
    tables_part = text[text.index("### 2.2 Table `app.users`") : text.index("### 2.21 ")]
    documented = set(_NAME_RE.findall(tables_part))
    declared = metadata_names()
    assert declared == documented


def test_every_constraint_kind_follows_its_prefix() -> None:
    for table in metadata.tables.values():
        for constraint in table.constraints:
            name = str(constraint.name)
            if isinstance(constraint, CheckConstraint):
                assert name.startswith("CK_")
            elif isinstance(constraint, ForeignKeyConstraint):
                assert name.startswith("FK_")
            elif isinstance(constraint, UniqueConstraint):
                assert name.startswith("UQ_")
            else:
                assert name.startswith("PK_")
        for index in table.indexes:
            assert isinstance(index, Index)
            assert str(index.name).startswith("UX_" if index.unique else "IX_")


def test_t084_actor_columns(spec_table: SpecTable) -> None:
    rows = spec_table("80-data-model.md", "### 2.23 Actor columns and a user's data")
    expected = {row[0]: {name.strip().strip("`") for name in row[1].split(",")} for row in rows}
    assert {table: set(columns) for table, columns in ACTOR_COLUMNS.items()} == expected
    for table in metadata.tables.values():
        for column in table.columns:
            if column.name.endswith("_by"):
                assert column.info.get("actor") is True, f"{table.name}.{column.name}"
    audit = metadata.tables["app.audit_events"]
    assert not audit.c.actor_object_id.info
    assert not audit.c.subject_object_id.info


def test_grant_matrix_covers_every_table_with_known_roles() -> None:
    assert {f"app.{name}" for name in GRANT_MATRIX} == set(metadata.tables)
    for row in GRANT_MATRIX.values():
        assert set(row) <= set(DATABASE_ROLES)
        for cell in row.values():
            assert cell.verbs or cell.update_columns
