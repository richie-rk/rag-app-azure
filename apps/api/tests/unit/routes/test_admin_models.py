from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, get_args
from uuid import UUID, uuid4

import pytest

from docchat_api.routes import admin_models as schemas
from docchat_core.ingestion.models import (
    ContentType,
    DocumentPdfStatus,
    DocumentVersionState,
    IndexStatus,
    ItemOperation,
    ItemStage,
    ItemState,
    ItemWarning,
    PhysicalIndexState,
    RunKind,
    RunPriority,
    RunState,
    ScanStatus,
)
from docchat_core.sql.repositories.documents import DocumentDetailRow, DocumentRow, VersionRow
from docchat_core.sql.repositories.indexes import (
    DocumentCounts,
    IndexDetailRow,
    IndexSummaryRow,
    OpenRunRow,
    PhysicalIndexRow,
    ProjectRefRow,
    RebuildProgress,
)
from docchat_core.sql.repositories.runs import ItemTarget, RunCounts, RunItemRow, RunRow

AT = datetime(2026, 10, 21, 9, 15, 2, 123000, tzinfo=UTC)
TEXT_AT = "2026-10-21T09:15:02.123Z"
INDEX = UUID("8b3c4d5e-6f7a-4b9c-8d1e-2f3a4b5c6d62")
PROJECT = UUID("7a2b3c4d-5e6f-4a8b-9c0d-1e2f3a4b5c61")
ADMIN = UUID("0ca20100-0000-4000-8000-000000000003")
ROW_VERSION = b"\x00\x00\x00\x00\x00\x00\x07\xd1"


def summary_row(status: IndexStatus = IndexStatus.READY) -> IndexSummaryRow:
    return IndexSummaryRow(
        id=INDEX,
        display_name="Policy library",
        status=status,
        method_key="recursive",
        method_version=1,
        method_display_name="By headings and paragraphs",
        embedding_model="text-embedding-3-large",
        embedding_dimensions=1024,
        language_analyzer="zh-Hans.microsoft",
        live_document_count=3,
        updated_at=AT,
        row_version=ROW_VERSION,
    )


def physical_row(state: PhysicalIndexState) -> PhysicalIndexRow:
    return PhysicalIndexRow(
        id=uuid4(),
        generation=2 if state is PhysicalIndexState.BUILDING else 1,
        schema_version=1,
        embedding_model="text-embedding-3-large",
        embedding_dimensions=1024,
        language_analyzer="standard.lucene",
        state=state,
        created_at=AT,
        activated_at=None if state is PhysicalIndexState.BUILDING else AT,
    )


def version_row(**overrides: Any) -> VersionRow:
    values: dict[str, Any] = {
        "id": uuid4(),
        "version_number": 1,
        "state": DocumentVersionState.INGESTED,
        "size_bytes": 482113,
        "sha256": bytes(range(32)),
        "content_type": ContentType.DOCX,
        "scan_status": ScanStatus.CLEAN,
        "failure_code": None,
        "uploaded_by": ADMIN,
        "uploaded_by_name": None,
        "project_id": PROJECT,
        "project_name": "Field notes",
        "reserved_at": AT,
        "committed_at": AT,
        "ingested_at": None,
        "document_pdf_status": DocumentPdfStatus.TOO_LARGE,
    }
    return VersionRow(**(values | overrides))


def test_an_index_summary_shows_the_reingest_banner_exactly_when_required() -> None:
    ready = schemas.index_summary(summary_row()).model_dump(mode="json", by_alias=True)
    required = schemas.index_summary(summary_row(IndexStatus.REINGEST_REQUIRED))

    assert ready == {
        "indexId": str(INDEX),
        "displayName": "Policy library",
        "status": "ready",
        "reingestRequired": False,
        "method": {"key": "recursive", "version": 1, "displayName": "By headings and paragraphs"},
        "embedding": {"model": "text-embedding-3-large", "dimensions": 1024},
        "languageAnalyzer": "zh-Hans.microsoft",
        "liveDocumentCount": 3,
        "updatedAt": TEXT_AT,
        "rowVersion": "AAAAAAAAB9E=",
    }
    assert required.reingest_required is True


def test_an_index_detail_carries_every_member_and_no_physical_name() -> None:
    run = uuid4()
    row = IndexDetailRow(
        summary=summary_row(IndexStatus.REBUILDING),
        description=None,
        method_parameters={"targetTokens": 512},
        active=physical_row(PhysicalIndexState.ACTIVE),
        building=physical_row(PhysicalIndexState.BUILDING),
        document_counts=DocumentCounts(3, 2, 1, 0),
        rebuild_progress=RebuildProgress(served_current=1, live_documents=3),
        mapped_projects=(ProjectRefRow(PROJECT, "Field notes"),),
        open_run=OpenRunRow(run, RunKind.INDEX_REBUILD, RunState.RUNNING),
        created_at=AT,
        created_by=ADMIN,
        updated_by=ADMIN,
    )

    detail = schemas.index_detail(row).model_dump(mode="json", by_alias=True)

    assert detail["method"]["parameters"] == {"targetTokens": 512}
    assert detail["description"] is None
    assert detail["buildingPhysicalIndex"] == {
        "generation": 2,
        "schemaVersion": 1,
        "embeddingModel": "text-embedding-3-large",
        "embeddingDimensions": 1024,
        "languageAnalyzer": "standard.lucene",
        "state": "building",
        "createdAt": TEXT_AT,
        "activatedAt": None,
    }
    assert detail["documentCounts"] == {"live": 3, "ingested": 2, "inProgress": 1, "failed": 0}
    assert detail["rebuildProgress"] == {"servedCurrent": 1, "liveDocuments": 3}
    assert detail["mappedProjects"] == [{"projectId": str(PROJECT), "name": "Field notes"}]
    assert detail["openRun"] == {"runId": str(run), "kind": "index_rebuild", "state": "running"}
    assert "name" not in detail["activePhysicalIndex"]


def test_a_document_without_versions_has_no_content_type() -> None:
    row = DocumentRow(
        id=uuid4(),
        source_name="Notes.pdf",
        normalized_name="notes.pdf",
        display_state="no_content",
        current_version=None,
        latest_version=None,
        updated_at=AT,
        row_version=ROW_VERSION,
    )

    summary = schemas.document_summary(row)

    assert (summary.content_type, summary.current_version, summary.latest_version) == (
        None,
        None,
        None,
    )
    assert summary.row_version == "AAAAAAAAB9E="


def _item(**overrides: Any) -> RunItemRow:
    values: dict[str, Any] = {
        "id": uuid4(),
        "queue_seq": 4,
        "operation": ItemOperation.INGEST,
        "document_id": uuid4(),
        "document_name": "Notes.pdf",
        "version_number": 3,
        "target": ItemTarget(1, PhysicalIndexState.ACTIVE),
        "state": ItemState.SUCCEEDED,
        "stage": ItemStage.FLIP,
        "attempts": 1,
        "outcome_code": None,
        "outcome_message": None,
        "warnings": (ItemWarning(code="table_summary_filtered", page=3),),
        "embedding_tokens": 10,
        "summary_input_tokens": None,
        "summary_output_tokens": None,
        "docintel_pages": 2,
        "created_at": AT,
        "started_at": AT,
        "completed_at": AT,
    }
    return RunItemRow(**(values | overrides))


def test_a_document_detail_adds_its_versions_and_items() -> None:
    latest = version_row(version_number=2)
    document = DocumentRow(
        id=uuid4(),
        source_name="Notes.pdf",
        normalized_name="notes.pdf",
        display_state="ingested",
        current_version=latest,
        latest_version=latest,
        updated_at=AT,
        row_version=ROW_VERSION,
    )

    detail = schemas.document_detail(
        DocumentDetailRow(
            document=document, versions=(latest, version_row()), recent_items=(_item(),)
        )
    ).model_dump(mode="json", by_alias=True)

    assert [version["versionNumber"] for version in detail["versions"]] == [2, 1]
    assert detail["recentItems"][0]["warnings"] == [{"code": "table_summary_filtered", "page": 3}]
    assert detail["recentItems"][0]["usage"] == {
        "embeddingTokens": 10,
        "summaryInputTokens": None,
        "summaryOutputTokens": None,
        "docIntelPages": 2,
    }
    assert detail["contentType"] == ContentType.DOCX.value


def test_a_run_summary_shows_its_priority_and_counts() -> None:
    row = RunRow(
        id=uuid4(),
        kind=RunKind.INDEX_DELETE,
        state=RunState.QUEUED,
        index_id=INDEX,
        index_display_name="Policy library",
        priority=RunPriority.BULK,
        requested_by=None,
        requested_by_name=None,
        created_at=AT,
        started_at=None,
        completed_at=None,
        counts=RunCounts(total=3, queued=3),
    )

    summary = schemas.run_summary(row).model_dump(mode="json", by_alias=True)

    assert summary["priority"] == "bulk"
    assert summary["index"] == {"indexId": str(INDEX), "displayName": "Policy library"}
    assert summary["counts"] == {
        "total": 3,
        "queued": 3,
        "running": 0,
        "succeeded": 0,
        "skipped": 0,
        "failed": 0,
        "cancelled": 0,
    }


def test_an_index_item_has_no_document() -> None:
    item = schemas.run_item(
        _item(
            operation=ItemOperation.PURGE_INDEX_BLOBS,
            document_id=None,
            document_name=None,
            version_number=None,
            target=None,
            stage=None,
            warnings=(),
        )
    )

    assert (item.document, item.target, item.stage) == (None, None, None)


@pytest.mark.parametrize(
    ("value", "enum"),
    [
        (schemas.IndexStatusValue, IndexStatus),
        (schemas.PhysicalIndexStateValue, PhysicalIndexState),
        (schemas.RunKindValue, RunKind),
        (schemas.ItemStageValue, ItemStage),
    ],
)
def test_closed_sets_are_published_inline_with_the_ledgers_values(
    value: Any, enum: type[StrEnum]
) -> None:
    (_, schema) = get_args(value)
    assert schema.json_schema == {"type": "string", "enum": [member.value for member in enum]}


@pytest.mark.parametrize(
    ("literal", "enum"),
    [
        (schemas.IndexStatusFilter, IndexStatus),
        (schemas.RunStateFilter, RunState),
        (schemas.RunKindFilter, RunKind),
        (schemas.ItemStateFilter, ItemState),
    ],
)
def test_the_query_filters_name_the_ledgers_values(literal: Any, enum: type[StrEnum]) -> None:
    assert list(get_args(literal)) == [member.value for member in enum]


def test_the_analyzer_schema_lists_the_allowlist_in_order() -> None:
    (_, schema) = get_args(schemas.AnalyzerValue)

    assert schema.json_schema["enum"][:3] == ["standard.lucene", "en.microsoft", "en.lucene"]
    assert len(schema.json_schema["enum"]) == 20
