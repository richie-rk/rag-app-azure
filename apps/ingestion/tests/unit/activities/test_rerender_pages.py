"""Unit tests of `rerender_pages` (`14-ingestion.md` section 6.15; T-623 at unit level)."""

from __future__ import annotations

import dataclasses
import json
from types import SimpleNamespace
from typing import Any

import pytest

from docchat_core.ingestion.names import converted_path, rendition_page_path, source_path
from docchat_ingestion.activities import rerender_pages
from docchat_ingestion.isolation.runner import RenditionOutcome
from docchat_ingestion.workfiles import RenditionMarker

DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def served(
    kit: SimpleNamespace,
    ids: Any,
    data: bytes,
    *,
    state: str = "served",
    rules: int | None = 1,
    content_type: str = "application/pdf",
    deleted: bool = False,
    status: str = "ready",
) -> Any:
    return kit.Result(
        rows=[
            kit.Row(
                id=ids.chunk_set,
                state=state,
                pdf_sha256=bytes.fromhex(kit.sha(data)),
                rendition_rules_version=rules,
                search_index_id=ids.index,
                document_id=ids.document,
                version_id=ids.version,
                version_number=1,
                content_type=content_type,
                blob_path=source_path(ids.index, ids.document, 1),
                page_count=3,
                is_deleted=deleted,
                status=status,
            )
        ]
    )


@pytest.fixture
def deployed_two(world: Any) -> Any:
    world.services = dataclasses.replace(world.services, rendition_rules_version=2)
    return world


async def test_t623_rerender_overwrites_from_the_stored_pdf(
    kit: SimpleNamespace, deployed_two: Any
) -> None:
    world = deployed_two
    ids = kit.Ids()
    world.documents.blobs[converted_path(ids.index, ids.document, 1)] = kit.CONVERTED_BYTES
    world.sql.script(
        kit.fence_ok(),
        served(kit, ids, kit.CONVERTED_BYTES, content_type=DOCX),
        kit.Result(),
        kit.Result(),
    )
    output = await rerender_pages.run(kit.envelope(ids), world.provider)
    assert output == {
        **output,
        "ok": True,
        "outcome": "rendered",
        "code": None,
        "pages": 3,
        "documentPdf": "built",
    }
    assert world.pool.commands() == ["render"]
    marker = json.loads(world.renditions.blobs[f"{ids.index}/{ids.document}/1/complete.json"])
    assert marker["renditionRulesVersion"] == 2
    statements = [entry.sql for entry in world.sql.connection.executed]
    assert not any(
        sql.startswith("UPDATE app.document_versions SET converted_sha256=") for sql in statements
    )
    status = world.sql.connection.executed[2]
    assert "converted_sha256" in status.sql
    assert "coalesce(" in status.sql


@pytest.mark.parametrize(
    ("row", "code"),
    [
        ({"deleted": True}, "document_deleted"),
        ({"status": "deleting"}, "document_deleted"),
        ({"state": "retired"}, "renditions_current"),
        ({"rules": 2}, "renditions_current"),
    ],
)
async def test_skips(
    kit: SimpleNamespace, deployed_two: Any, row: dict[str, Any], code: str
) -> None:
    world = deployed_two
    ids = kit.Ids()
    world.sql.script(kit.fence_ok(), served(kit, ids, kit.PDF_BYTES, **row))
    output = await rerender_pages.run(kit.envelope(ids), world.provider)
    assert output["outcome"] == "skipped"
    assert output["code"] == code
    assert world.pool.calls == []


async def test_a_current_marker_is_recorded_then_skipped(
    kit: SimpleNamespace, deployed_two: Any
) -> None:
    world = deployed_two
    ids = kit.Ids()
    world.renditions.blobs[f"{ids.index}/{ids.document}/1/complete.json"] = RenditionMarker(
        page_count=3,
        pdf_sha256=kit.sha(kit.PDF_BYTES),
        rendition_rules_version=2,
        document_pdf="built",
    ).encode()
    world.sql.script(kit.fence_ok(), served(kit, ids, kit.PDF_BYTES), kit.Result(), kit.Result())
    output = await rerender_pages.run(kit.envelope(ids), world.provider)
    assert output["code"] == "renditions_current"
    (rules,) = world.sql.connection.statements("UPDATE app.chunk_sets SET rendition_rules_version")
    assert rules.values["rendition_rules_version"] == 2


async def test_t623_render_too_large_deletes_no_page(
    kit: SimpleNamespace, deployed_two: Any
) -> None:
    world = deployed_two
    ids = kit.Ids()
    world.documents.blobs[source_path(ids.index, ids.document, 1)] = kit.PDF_BYTES
    world.pool.rendition = RenditionOutcome("render_too_large", uploaded_pages=(1, 2))
    world.pool.render_uploads = [1, 2]
    world.sql.script(kit.fence_ok(), served(kit, ids, kit.PDF_BYTES))
    output = await rerender_pages.run(kit.envelope(ids), world.provider)
    assert output["errorCode"] == "render_too_large"
    assert rendition_page_path(ids.index, ids.document, 1, 1) in world.renditions.blobs


async def test_a_whole_document_too_large_sets_the_status_before_deleting(
    kit: SimpleNamespace, deployed_two: Any
) -> None:
    world = deployed_two
    ids = kit.Ids()
    world.documents.blobs[source_path(ids.index, ids.document, 1)] = kit.PDF_BYTES
    document = f"{ids.index}/{ids.document}/1/document.pdf"
    world.renditions.blobs[document] = b"old"
    world.pool.rendition = RenditionOutcome(None, 3, "too_large", (1, 2, 3))
    world.pool.render_uploads = [1, 2, 3]
    world.sql.script(
        kit.fence_ok(), served(kit, ids, kit.PDF_BYTES), kit.Result(), kit.Result(), kit.Result()
    )
    output = await rerender_pages.run(kit.envelope(ids), world.provider)
    assert output["documentPdf"] == "too_large"
    assert document not in world.renditions.blobs
    too_large = world.sql.connection.executed[2]
    assert too_large.sql.startswith(
        "UPDATE app.document_versions SET document_pdf_status=N'too_large'"
    )


async def test_t623_a_storage_error_on_the_marker_uploads_no_page(
    kit: SimpleNamespace, deployed_two: Any
) -> None:
    world = deployed_two
    ids = kit.Ids()
    world.renditions.blobs[f"{ids.index}/{ids.document}/1/complete.json"] = b"{}"
    world.renditions.fail.add("delete")
    world.sql.script(kit.fence_ok(), served(kit, ids, kit.PDF_BYTES))
    output = await rerender_pages.run(kit.envelope(ids), world.provider)
    assert output["errorCode"] == "storage_unavailable"
    assert world.pool.calls == []


def test_still_due() -> None:
    base = rerender_pages.ServedSet(
        chunk_set_id=None,  # type: ignore[arg-type]
        chunk_set_state="served",
        pdf_sha256="0" * 64,
        rendition_rules_version=None,
        index_id=None,  # type: ignore[arg-type]
        document_id=None,  # type: ignore[arg-type]
        version_id=None,  # type: ignore[arg-type]
        version_number=1,
        content_type="application/pdf",  # type: ignore[arg-type]
        blob_path="",
        page_count=1,
        document_deleted=False,
        index_status="ready",
    )
    assert rerender_pages.still_due(base, 1) is None
    assert rerender_pages.still_due(dataclasses.replace(base, index_status="deleted"), 1) == (
        "document_deleted"
    )
