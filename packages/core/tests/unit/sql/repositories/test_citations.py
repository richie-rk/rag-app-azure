"""Unit tests of the file and citation reads (`12-chat-pipeline.md` section 16.2 step 5;
`11-api.md` section 3.11.10): their statement text compiled for SQL Server, their bound
parameters and the rows they return, on a data-access layer without SQL. The plans are proven by
the plan integration test (T-067), the answers by the citation integration tests (T-276)."""

from __future__ import annotations

import re
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.sql.base import Executable

from docchat_core.ingestion.models import ContentType, DocumentPdfStatus
from docchat_core.sql.engine import REQUEST_PATH_REGISTRY
from docchat_core.sql.repositories import citations
from docchat_core.sql.repositories.citations import (
    CitedDocument,
    CurrentVersion,
    DocumentPdfLookup,
    LiveDocument,
)

DIALECT = create_engine("mssql+pyodbc://").dialect
DOCUMENT = uuid4()
VERSION = uuid4()
CHUNK_SET = uuid4()
PROJECT = uuid4()
INDEX = uuid4()
SHA = bytes(range(32))
CONVERTED = bytes(range(1, 33))


def sql(statement: Executable) -> str:
    return re.sub(r"\s+", " ", statement.compile(dialect=DIALECT).string).strip()  # type: ignore[attr-defined]


def binds(statement: Executable) -> list[str]:
    positions: list[str] = statement.compile(dialect=DIALECT).positiontup  # type: ignore[attr-defined]
    return positions


def cited_row(**changes: Any) -> SimpleNamespace:
    values: dict[str, Any] = {
        "search_index_id": INDEX,
        "is_deleted": False,
        "current_version_id": VERSION,
        "source_name": "Travel Policy.pdf",
        "content_type": ContentType.PDF.value,
        "version_number": 3,
        "sha256": SHA,
        "converted_sha256": None,
        "document_pdf_status": "built",
        "pdf_sha256": SHA,
        "index_mapped": 1,
    }
    return SimpleNamespace(**(values | changes))


def test_the_cited_document_statement_is_section_16_2s() -> None:
    assert sql(citations.CITED_DOCUMENT_QUERY) == (
        "SELECT d.search_index_id, d.is_deleted, d.current_version_id, d.source_name, "
        "v.content_type, v.version_number, v.sha256, v.converted_sha256, "
        "v.document_pdf_status, cs.pdf_sha256, "
        "CASE WHEN (pi.project_id IS NULL) THEN 0 ELSE 1 END AS index_mapped "
        "FROM app.documents AS d "
        "JOIN app.document_versions AS v ON v.id = ? AND v.document_id = d.id "
        "JOIN app.chunk_sets AS cs ON cs.id = ? AND cs.document_version_id = v.id "
        "LEFT OUTER JOIN app.project_indexes AS pi "
        "ON pi.project_id = ? AND pi.search_index_id = d.search_index_id "
        "WHERE d.id = ?"
    )
    assert binds(citations.CITED_DOCUMENT_QUERY) == [
        "document_version_id",
        "chunk_set_id",
        "project_id",
        "document_id",
    ]


def test_the_document_pdf_statement_left_joins_the_live_document_and_its_current_version() -> None:
    assert sql(citations.DOCUMENT_PDF_QUERY) == (
        "SELECT s.status, d.id AS document_id, d.source_name, v.id AS version_id, "
        "v.version_number, v.document_pdf_status "
        "FROM app.search_indexes AS s "
        "LEFT OUTER JOIN app.documents AS d "
        "ON d.id = ? AND d.search_index_id = s.id AND d.is_deleted = 0 "
        "LEFT OUTER JOIN app.document_versions AS v ON v.id = d.current_version_id "
        "WHERE s.id = ?"
    )
    assert binds(citations.DOCUMENT_PDF_QUERY) == ["document_id", "index_id"]


def test_both_reads_are_registered_for_the_plan_audit() -> None:
    registered = {
        key.rsplit(".", 1)[1]: entry
        for key, entry in REQUEST_PATH_REGISTRY.items()
        if key.startswith("docchat_core.sql.repositories.citations.")
    }
    assert set(registered) == {"read_cited_document", "read_document_pdf"}
    assert registered["read_cited_document"].example_params.keys() == {
        "document_id",
        "document_version_id",
        "chunk_set_id",
        "project_id",
    }
    assert registered["read_document_pdf"].example_params.keys() == {"index_id", "document_id"}


async def test_the_cited_document_comes_back_typed(fake_sql: Any) -> None:
    fake_sql.queue(
        "cited_document",
        cited_row(sha256=bytearray(SHA), pdf_sha256=bytearray(SHA)),
        None,
    )

    found = await citations.read_cited_document(
        fake_sql,
        document_id=DOCUMENT,
        document_version_id=VERSION,
        chunk_set_id=CHUNK_SET,
        project_id=PROJECT,
    )
    assert found == CitedDocument(
        search_index_id=INDEX,
        is_deleted=False,
        current_version_id=VERSION,
        source_name="Travel Policy.pdf",
        content_type=ContentType.PDF,
        version_number=3,
        sha256=SHA,
        converted_sha256=None,
        document_pdf_status=DocumentPdfStatus.BUILT,
        pdf_sha256=SHA,
        index_mapped=True,
    )
    assert isinstance(found.sha256, bytes)
    assert (
        await citations.read_cited_document(
            fake_sql,
            document_id=DOCUMENT,
            document_version_id=VERSION,
            chunk_set_id=CHUNK_SET,
            project_id=PROJECT,
        )
        is None
    )
    call = fake_sql.calls[0]
    assert (call.method, call.statement, call.retry) == (
        "fetch_one",
        citations.CITED_DOCUMENT_QUERY,
        "transient",
    )
    assert call.parameters == {
        "document_id": DOCUMENT,
        "document_version_id": VERSION,
        "chunk_set_id": CHUNK_SET,
        "project_id": PROJECT,
    }


async def test_an_unmapped_index_and_an_unrendered_version_read_as_such(fake_sql: Any) -> None:
    fake_sql.queue(
        "cited_document",
        cited_row(
            index_mapped=0,
            is_deleted=1,
            current_version_id=None,
            document_pdf_status=None,
            pdf_sha256=None,
        ),
    )

    found = await citations.read_cited_document(
        fake_sql,
        document_id=DOCUMENT,
        document_version_id=VERSION,
        chunk_set_id=CHUNK_SET,
        project_id=PROJECT,
    )
    assert found is not None
    assert found.index_mapped is False
    assert found.is_deleted is True
    assert found.current_version_id is None
    assert found.document_pdf_status is None
    assert found.pdf_sha256 is None


@pytest.mark.parametrize(
    ("content_type", "expected"),
    [
        (ContentType.PDF, SHA),
        (ContentType.DOCX, CONVERTED),
        (ContentType.PPTX, CONVERTED),
    ],
)
async def test_the_renditions_come_from_the_source_or_the_converted_pdf(
    fake_sql: Any, content_type: ContentType, expected: bytes
) -> None:
    fake_sql.queue(
        "cited_document", cited_row(content_type=content_type.value, converted_sha256=CONVERTED)
    )

    found = await citations.read_cited_document(
        fake_sql,
        document_id=DOCUMENT,
        document_version_id=VERSION,
        chunk_set_id=CHUNK_SET,
        project_id=PROJECT,
    )
    assert found is not None
    assert found.rendition_pdf_sha256 == expected


async def test_a_docx_without_a_converted_pdf_has_no_rendition_hash(fake_sql: Any) -> None:
    fake_sql.queue("cited_document", cited_row(content_type=ContentType.DOCX.value))

    found = await citations.read_cited_document(
        fake_sql,
        document_id=DOCUMENT,
        document_version_id=VERSION,
        chunk_set_id=CHUNK_SET,
        project_id=PROJECT,
    )
    assert found is not None
    assert found.rendition_pdf_sha256 is None


def pdf_row(**changes: Any) -> SimpleNamespace:
    values: dict[str, Any] = {
        "status": "ready",
        "document_id": DOCUMENT,
        "source_name": "Board Pack.pptx",
        "version_id": VERSION,
        "version_number": 2,
        "document_pdf_status": "built",
    }
    return SimpleNamespace(**(values | changes))


@pytest.mark.parametrize(
    ("row", "expected"),
    [
        (None, None),
        (pdf_row(status="deleted"), None),
        (
            pdf_row(status="deleting", document_id=None, source_name=None, version_id=None),
            DocumentPdfLookup(document=None),
        ),
        (
            pdf_row(version_id=None, version_number=None, document_pdf_status=None),
            DocumentPdfLookup(LiveDocument(DOCUMENT, "Board Pack.pptx", current_version=None)),
        ),
        (
            pdf_row(document_pdf_status="too_large"),
            DocumentPdfLookup(
                LiveDocument(
                    DOCUMENT,
                    "Board Pack.pptx",
                    CurrentVersion(VERSION, 2, DocumentPdfStatus.TOO_LARGE),
                )
            ),
        ),
        (
            pdf_row(document_pdf_status=None),
            DocumentPdfLookup(
                LiveDocument(DOCUMENT, "Board Pack.pptx", CurrentVersion(VERSION, 2, None))
            ),
        ),
        (
            pdf_row(),
            DocumentPdfLookup(
                LiveDocument(
                    DOCUMENT, "Board Pack.pptx", CurrentVersion(VERSION, 2, DocumentPdfStatus.BUILT)
                )
            ),
        ),
    ],
)
async def test_the_document_pdf_lookup_answers_each_step(
    fake_sql: Any, row: SimpleNamespace | None, expected: DocumentPdfLookup | None
) -> None:
    fake_sql.queue("document_pdf", row)

    found = await citations.read_document_pdf(fake_sql, index_id=INDEX, document_id=DOCUMENT)

    assert found == expected
    (call,) = fake_sql.calls
    assert call.statement is citations.DOCUMENT_PDF_QUERY
    assert call.parameters == {"index_id": INDEX, "document_id": DOCUMENT}
