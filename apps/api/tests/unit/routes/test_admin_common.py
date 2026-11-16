from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, get_args
from uuid import UUID

import pytest

from docchat_api.http.paging import CursorModel, decode_cursor, encode_cursor
from docchat_api.routes.admin_common import (
    ContentTypeValue,
    DocumentPdfStatusValue,
    DocumentRef,
    IndexRef,
    PageQuery,
    ProjectRef,
    ScanStatusValue,
    UserRef,
    VersionStateValue,
    failure_message,
    index_ref,
    keyset_page,
    next_cursor,
    user_ref,
    version_summary,
)
from docchat_core.ingestion.models import (
    ContentType,
    DocumentPdfStatus,
    DocumentVersionState,
    ScanStatus,
)
from docchat_core.sql.repositories.documents import VersionRow
from docchat_core.sql.repositories.projects import IndexRefRow, UserRefRow

USER = UUID("2b3c4d5e-6f7a-4b8c-9d0e-1f2a3b4c5d6e")
PROJECT = UUID("7a2b3c4d-5e6f-4a8b-9c0d-1e2f3a4b5c61")
AT = datetime(2026, 10, 21, 9, 15, 2, 123000, tzinfo=UTC)


def version_row(**overrides: Any) -> VersionRow:
    values: dict[str, Any] = {
        "id": UUID("3c4d5e6f-7a8b-4c9d-8e0f-1a2b3c4d5e6f"),
        "version_number": 1,
        "state": DocumentVersionState.INGESTED,
        "size_bytes": 482113,
        "sha256": bytes(range(32)),
        "content_type": ContentType.DOCX,
        "scan_status": ScanStatus.CLEAN,
        "failure_code": None,
        "uploaded_by": USER,
        "uploaded_by_name": None,
        "project_id": PROJECT,
        "project_name": "Field notes",
        "reserved_at": AT,
        "committed_at": AT,
        "ingested_at": None,
        "document_pdf_status": DocumentPdfStatus.TOO_LARGE,
    }
    return VersionRow(**(values | overrides))


class NameCursor(CursorModel):
    n: str


def test_the_references_carry_ids_and_display_names() -> None:
    assert user_ref(UserRefRow(user_id=USER, display_name="")).model_dump(by_alias=True) == {
        "userId": USER,
        "displayName": "",
    }
    assert index_ref(IndexRefRow(index_id=USER, display_name="Handbook")) == IndexRef(
        index_id=USER, display_name="Handbook"
    )
    assert UserRef.model_json_schema()["properties"]["displayName"]["maxLength"] == 256


def test_the_page_query_defaults_to_50() -> None:
    query = PageQuery.model_validate({})
    assert (query.limit, query.cursor) == (50, None)


def test_a_last_page_has_no_cursor() -> None:
    assert keyset_page(["a", "b"], 2, lambda name: NameCursor(n=name)) == (["a", "b"], None)


def test_an_extra_row_gives_the_cursor_of_the_last_item() -> None:
    items, cursor = keyset_page(["a", "b", "c"], 2, lambda name: NameCursor(n=name))

    assert items == ["a", "b"]
    assert cursor is not None
    assert decode_cursor(cursor, NameCursor).n == "b"


def test_a_page_ends_early_while_its_cursor_would_be_too_long() -> None:
    # A 256-code-point name of three-byte characters encodes past 512 characters.
    long_name = "中" * 256
    with pytest.raises(ValueError, match="512"):
        encode_cursor(NameCursor(n=long_name))

    items, cursor = next_cursor(["a", long_name], True, lambda name: NameCursor(n=name))

    assert items == ["a"]
    assert cursor == encode_cursor(NameCursor(n="a"))


def test_a_page_whose_items_carry_no_cursor_fails() -> None:
    with pytest.raises(ValueError, match="no item"):
        next_cursor(["中" * 256], True, lambda name: NameCursor(n=name))


def test_the_project_and_document_references_bound_their_names() -> None:
    project_name = ProjectRef.model_json_schema()["properties"]["name"]
    assert (project_name["minLength"], project_name["maxLength"]) == (1, 80)
    assert DocumentRef(document_id=USER, name="a.pdf").model_dump(by_alias=True) == {
        "documentId": USER,
        "name": "a.pdf",
    }
    assert DocumentRef.model_json_schema()["properties"]["name"]["maxLength"] == 255


def test_a_version_summary_carries_its_failure_message() -> None:
    summary = version_summary(
        version_row(state=DocumentVersionState.FAILED, failure_code="malware_detected")
    ).model_dump(mode="json", by_alias=True)

    assert summary["sha256"] == bytes(range(32)).hex()
    assert summary["failureCode"] == "malware_detected"
    assert summary["failureMessage"] == (
        "The file was blocked because a security scan found a threat."
    )
    assert summary["uploadedByName"] is None
    assert summary["uploadedViaProject"] == {"projectId": str(PROJECT), "name": "Field notes"}
    assert summary["documentPdfStatus"] == "too_large"
    assert summary["contentType"] == ContentType.DOCX.value


@pytest.mark.parametrize(
    ("code", "message"),
    [
        (None, None),
        ("document_deleted", "Not processed because the document was deleted."),
        ("content_unchanged", "This file is already in the index with the same content."),
        ("not_a_code", None),
    ],
)
def test_failure_messages_come_from_their_catalogue(code: str | None, message: str | None) -> None:
    assert failure_message(code) == message


@pytest.mark.parametrize(
    ("value", "enum"),
    [
        (VersionStateValue, DocumentVersionState),
        (ScanStatusValue, ScanStatus),
        (ContentTypeValue, ContentType),
        (DocumentPdfStatusValue, DocumentPdfStatus),
    ],
)
def test_a_versions_closed_sets_are_published_inline(value: Any, enum: type[StrEnum]) -> None:
    (_, schema) = get_args(value)
    assert schema.json_schema == {"type": "string", "enum": [member.value for member in enum]}
