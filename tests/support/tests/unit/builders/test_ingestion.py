from __future__ import annotations

import json
import re
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import Table, insert
from sqlalchemy.dialects import mssql
from sqlalchemy.dialects.mssql import ROWVERSION

from docchat_core.sql.tables.ingestion import (
    CONTENT_TYPES,
    ITEM_OPERATIONS,
    chunk_sets,
    document_versions,
    documents,
    ingestion_dispatcher_state,
    ingestion_methods,
    ingestion_run_items,
    ingestion_runs,
    physical_indexes,
    scan_canaries,
    search_indexes,
    served_chunk_sets,
)
from docchat_testing.builders.canaries import is_canary
from docchat_testing.builders.ingestion import (
    EMBEDDING_DIMENSIONS,
    chunk_set_row,
    dispatcher_state_row,
    document_row,
    document_version_row,
    ingestion_method_row,
    ingestion_run_item_row,
    ingestion_run_row,
    physical_index_name,
    physical_index_row,
    scan_canary_row,
    search_index_row,
    served_chunk_set_row,
    source_path,
)

NAME_PATTERN = re.compile(r"^docchat-[0-9a-f]{32}-g[1-9][0-9]{0,4}$")


def assert_insertable(table: Table, row: dict[str, Any]) -> None:
    """Every column the server can't fill is given, and the insert compiles for SQL Server."""
    required = {
        column.name
        for column in table.columns
        if not column.nullable
        and column.server_default is None
        and column.identity is None
        and not isinstance(column.type, ROWVERSION)
    }
    assert required <= set(row), required - set(row)
    insert(table).values(**row).compile(dialect=mssql.dialect())  # type: ignore[no-untyped-call]


def test_method_and_index_rows() -> None:
    method = ingestion_method_row()
    assert_insertable(ingestion_methods, method)
    assert re.fullmatch(r"[a-z0-9_]+", method["method_key"])
    assert json.loads(method["supported_content_types"]) == ["application/pdf"]
    index = search_index_row()
    assert_insertable(search_indexes, index)
    assert (index["method_key"], index["method_version"]) == ("pagewise", 1)
    assert index["embedding_dimensions"] in (256, 1024, 3072)
    assert len(index["method_parameter_hash"]) == 32
    assert is_canary(index["display_name"])


def test_physical_index_row_is_named_as_section_6_2() -> None:
    index_id = uuid4()
    row = physical_index_row(index_id, generation=2)
    assert_insertable(physical_indexes, row)
    assert row["name"] == f"docchat-{index_id.hex}-g2"
    assert NAME_PATTERN.fullmatch(row["name"])
    assert len(row["name"]) <= 47
    assert row["embedding_dimensions"] == EMBEDDING_DIMENSIONS
    assert physical_index_name(index_id, 1).endswith("-g1")


def test_document_and_version_rows() -> None:
    index_id, project_id = uuid4(), uuid4()
    document = document_row(index_id)
    assert_insertable(documents, document)
    assert document["normalized_name"] == document["source_name"].casefold()
    version = document_version_row(document["id"], index_id, project_id, version_number=3)
    assert_insertable(document_versions, version)
    assert version["content_type"] in CONTENT_TYPES
    assert version["blob_path"] == source_path(index_id, document["id"], 3)
    assert (version["scan_required"], version["scan_status"]) == (False, "not_required")
    for column in ("scan_time", "scan_raw_result", "converter_version", "converted_sha256"):
        assert column not in version


def test_chunk_set_and_served_rows() -> None:
    document, version, physical = uuid4(), uuid4(), uuid4()
    chunk_set = chunk_set_row(document, version, physical)
    assert_insertable(chunk_sets, chunk_set)
    served = served_chunk_set_row(document, physical, chunk_set["id"], version)
    assert_insertable(served_chunk_sets, served)


def test_run_rows() -> None:
    index_id = uuid4()
    run = ingestion_run_row(index_id)
    assert_insertable(ingestion_runs, run)
    assert (run["kind"], run["priority"]) == ("upload", 0)


@pytest.mark.parametrize(
    ("targets", "operation"),
    [
        (
            {"document_id": uuid4(), "document_version_id": uuid4(), "physical_index_id": uuid4()},
            "ingest",
        ),
        ({"document_id": uuid4()}, "cleanup_document"),
        ({"physical_index_id": uuid4()}, "create_physical_index"),
        ({}, "purge_index_blobs"),
    ],
)
def test_run_item_operation_follows_its_targets(targets: dict[str, Any], operation: str) -> None:
    row = ingestion_run_item_row(uuid4(), uuid4(), **targets)
    assert_insertable(ingestion_run_items, row)
    assert row["operation"] == operation
    assert operation in ITEM_OPERATIONS
    assert "queue_seq" not in row


def test_run_item_operation_can_be_overridden() -> None:
    row = ingestion_run_item_row(
        uuid4(), uuid4(), physical_index_id=uuid4(), operation="delete_physical_index"
    )
    assert row["operation"] == "delete_physical_index"


def test_singletons_and_canaries() -> None:
    state = dispatcher_state_row(running_items=2)
    assert_insertable(ingestion_dispatcher_state, state)
    assert state == {"id": 1, "running_items": 2}
    canary = scan_canary_row()
    assert_insertable(scan_canaries, canary)
    assert canary["blob_path"] == f"_scanner-canary/{canary['id']}.pdf"
