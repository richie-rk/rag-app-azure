"""Unit tests of the served-chunk-set statement (`12-chat-pipeline.md` section 6.5): its text, its
bound parameters, the rows it returns and its registration for the plan audit."""

from __future__ import annotations

import re
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

from sqlalchemy import create_engine

from docchat_core.sql.engine import REQUEST_PATH_REGISTRY, SqlEngine
from docchat_core.sql.repositories import retrieval

DIALECT = create_engine("mssql+pyodbc://").dialect


def test_the_statement_seeks_by_document_and_physical_index() -> None:
    compiled = retrieval.SERVED_CHUNK_SETS_QUERY.compile(
        dialect=DIALECT, compile_kwargs={"render_postcompile": False}
    )
    text = re.sub(r"\s+", " ", compiled.string).strip()

    assert text.startswith(
        "SELECT s.document_id, s.chunk_set_id, s.document_version_id, d.source_name "
        "FROM app.served_chunk_sets AS s JOIN app.documents AS d ON d.id = s.document_id "
        "WHERE s.physical_index_id = ? AND s.document_id IN ("
    )
    assert text.endswith("AND d.is_deleted = 0")


class Engine:
    def __init__(self, rows: list[Any]) -> None:
        self.rows = rows
        self.calls: list[tuple[dict[str, Any], str]] = []

    async def fetch_all(self, statement: object, parameters: dict[str, Any], *, name: str) -> Any:
        self.calls.append((parameters, name))
        return self.rows


async def test_rows_by_document_and_distinct_identifiers() -> None:
    physical, first, second = uuid4(), uuid4(), uuid4()
    chunk_set, version = uuid4(), uuid4()
    engine = Engine(
        [
            SimpleNamespace(
                document_id=first,
                chunk_set_id=chunk_set,
                document_version_id=version,
                source_name="a.pdf",
            )
        ]
    )

    served = await retrieval.read_served_chunk_sets(
        cast("SqlEngine", engine), physical_index_id=physical, document_ids=[first, second, first]
    )

    assert served == {
        first: retrieval.ServedChunkSet(
            document_id=first,
            chunk_set_id=chunk_set,
            document_version_id=version,
            source_name="a.pdf",
        )
    }
    assert engine.calls == [
        ({"physical_index_id": physical, "document_ids": [first, second]}, "served_chunk_sets")
    ]


async def test_no_identifiers_run_no_statement() -> None:
    engine = Engine([])

    assert (
        await retrieval.read_served_chunk_sets(
            cast("SqlEngine", engine), physical_index_id=uuid4(), document_ids=[]
        )
        == {}
    )
    assert engine.calls == []


def test_the_statement_is_registered_for_the_plan_audit() -> None:
    entry = REQUEST_PATH_REGISTRY["docchat_core.sql.repositories.retrieval.read_served_chunk_sets"]

    assert set(entry.example_params) == {"physical_index_id", "document_ids"}
