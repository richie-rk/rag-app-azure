"""Unit tests of the ingestion tables (`80-data-model.md` sections 2.9 to 2.20 and 2.22)."""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path

from sqlalchemy.dialects import mssql
from sqlalchemy.schema import CreateIndex, CreateTable

from docchat_core.sql.tables import metadata
from docchat_core.sql.tables.ingestion import (
    API_DOCUMENT_VERSION_COLUMNS,
    INGESTION_GRANTS,
    INGESTION_TABLES,
    OPERATOR_DOCUMENT_VERSION_COLUMNS,
    chunk_sets,
    document_versions,
    documents,
    ingestion_run_items,
    search_indexes,
)

type SpecTable = Callable[[str, str], list[list[str]]]

LETTERS = {"S": "SELECT", "I": "INSERT", "U": "UPDATE", "D": "DELETE"}
ROLES = (
    "docchat_api",
    "docchat_ingestion",
    "docchat_config_operator",
    "docchat_conversation_reader",
    "docchat_user_data_operator",
)

# SQLAlchemy's dialect classes have untyped constructors.
DIALECT = mssql.dialect()  # type: ignore[no-untyped-call]


def spec_lines() -> list[str]:
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "docs" / "spec" / "80-data-model.md"
        if candidate.is_file():
            return candidate.read_text(encoding="utf-8").splitlines()
    raise FileNotFoundError("80-data-model.md")


def index_sql(name: str) -> str:
    for table in INGESTION_TABLES:
        for index in table.indexes:
            if index.name == name:
                return str(CreateIndex(index).compile(dialect=DIALECT))
    raise KeyError(name)


def test_eleven_ingestion_tables() -> None:
    assert sorted(table.name for table in INGESTION_TABLES) == sorted(
        [
            "ingestion_methods",
            "search_indexes",
            "physical_indexes",
            "documents",
            "document_versions",
            "chunk_sets",
            "served_chunk_sets",
            "ingestion_runs",
            "ingestion_run_items",
            "ingestion_dispatcher_state",
            "scan_canaries",
        ]
    )


def test_foreign_keys_equal_the_data_model(spec_table: SpecTable) -> None:
    rows = spec_table("80-data-model.md", "### 2.20 Foreign keys and delete behaviour")
    expected: dict[str, tuple[str, str, str | None, str | None]] = {}
    for name, source, target, on_delete in rows:
        from_table, from_columns = re.fullmatch(r"`?(\w+) \(([^)]+)\)`?", source).groups()  # type: ignore[union-attr]
        to_table, to_columns = re.match(r"`?(\w+) \(([^)]+)\)", target).groups()  # type: ignore[union-attr]
        expected[name] = (
            f"{from_table}({from_columns})",
            f"{to_table}({to_columns})",
            "CASCADE" if on_delete == "cascade" else None,
            "alter" if "created after" in on_delete else None,
        )
    declared: dict[str, tuple[str, str, str | None, str | None]] = {}
    for table in metadata.tables.values():
        for constraint in table.foreign_key_constraints:
            target = constraint.elements[0].column.table.name
            declared[str(constraint.name)] = (
                f"{table.name}({', '.join(constraint.column_keys)})",
                f"{target}({', '.join(element.column.name for element in constraint.elements)})",
                constraint.ondelete,
                "alter" if constraint.use_alter else None,
            )
    assert len(declared) == 30
    assert declared == expected


def test_versions_table_never_uses_output_without_into() -> None:
    assert document_versions.implicit_returning is False


def test_filtered_indexes_keep_their_constants() -> None:
    assert index_sql("UX_search_indexes_display_name").endswith("WHERE status != N'deleted'")
    assert index_sql("UX_document_versions_document_sha").endswith(
        "(document_id, sha256) WHERE state IN (N'pending', N'ingesting', N'ingested')"
    )
    assert index_sql("IX_chunk_sets_cleanup") == (
        "CREATE INDEX [IX_chunk_sets_cleanup] ON app.chunk_sets (retired_at) "
        "INCLUDE (document_id, physical_index_id, chunk_count) "
        "WHERE state = N'retired' AND chunks_deleted_at IS NULL"
    )
    assert index_sql("IX_run_items_claim") == (
        "CREATE INDEX [IX_run_items_claim] ON app.ingestion_run_items (priority, queue_seq) "
        "INCLUDE (document_id, document_version_id, physical_index_id, operation, not_before, "
        "created_at) WHERE state = N'queued'"
    )
    assert index_sql("IX_ingestion_runs_created").endswith(
        "(created_at DESC, id DESC) INCLUDE (kind, search_index_id, priority, state, "
        "requested_by, started_at, completed_at)"
    )
    assert index_sql("IX_ingestion_runs_index_created").endswith(
        "(search_index_id, created_at DESC, id DESC) "
        "INCLUDE (kind, priority, state, requested_by, started_at, completed_at)"
    )
    assert index_sql("IX_documents_index_live").endswith(
        "(search_index_id, is_deleted) INCLUDE (current_version_id, normalized_name)"
    )


def test_ddl_compiles_for_sql_server() -> None:
    dialect = DIALECT
    for table in INGESTION_TABLES:
        ddl = str(CreateTable(table).compile(dialect=dialect))
        assert f"CONSTRAINT [PK_{table.name}] PRIMARY KEY CLUSTERED" in ddl
    items = str(CreateTable(ingestion_run_items).compile(dialect=dialect))
    assert "queue_seq BIGINT NOT NULL IDENTITY(1,1)" in items
    assert "CONSTRAINT [CK_run_items_shape] CHECK" in items
    # The circular keys are added after both tables exist.
    assert "FK_search_indexes_active_physical" not in str(
        CreateTable(search_indexes).compile(dialect=dialect)
    )
    assert "FK_documents_current_version" not in str(
        CreateTable(documents).compile(dialect=dialect)
    )
    assert "rendition_rules_version INTEGER NULL" in str(
        CreateTable(chunk_sets).compile(dialect=dialect)
    )


def test_ingestion_grants_equal_the_matrix() -> None:
    lines = spec_lines()
    start = next(i for i, line in enumerate(lines) if line.startswith("| Table (owning area) |"))
    for line in lines[start + 2 :]:
        if not line.startswith("|"):
            break
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        table = cells[0].split("`")[1]
        if table not in INGESTION_GRANTS:
            continue
        for role, cell in zip(ROLES, cells[1:], strict=True):
            tokens = re.sub(r"[¹²]", "", cell).split()
            expected = {LETTERS.get(token, token) for token in tokens if token != "\N{EN DASH}"}
            declared = INGESTION_GRANTS[table].get(role)  # type: ignore[call-overload]
            got: set[str] = set(declared.verbs) if declared else set()
            if declared and declared.update_columns:
                got.add("U(cols)")
            assert got == expected, (table, role)


def test_document_version_column_grants() -> None:
    text = "\n".join(spec_lines())
    footnote_1 = re.search(
        r"¹ `docchat_api` on `app.document_versions`: `UPDATE \(([^)]+)\)`", text
    )
    footnote_2 = re.search(
        r"² `docchat_config_operator` on `app.document_versions`: `UPDATE \(([^)]+)\)`", text
    )
    assert footnote_1 is not None
    assert footnote_2 is not None
    assert tuple(footnote_1.group(1).split(", ")) == API_DOCUMENT_VERSION_COLUMNS
    assert tuple(footnote_2.group(1).split(", ")) == OPERATOR_DOCUMENT_VERSION_COLUMNS
    scan_columns = {
        "scan_required",
        "scan_status",
        "scan_time",
        "scan_raw_result",
        "converter_version",
        "converted_sha256",
        "document_pdf_status",
    }
    assert not scan_columns & set(API_DOCUMENT_VERSION_COLUMNS)
    assert not scan_columns & set(OPERATOR_DOCUMENT_VERSION_COLUMNS)
    for column in API_DOCUMENT_VERSION_COLUMNS:
        assert column in document_versions.c


def test_config_operator_deletes_no_ingestion_row() -> None:
    for row in INGESTION_GRANTS.values():
        cell = row.get("docchat_config_operator")
        assert cell is None or "DELETE" not in cell.verbs
