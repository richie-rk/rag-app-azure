"""Unit tests of `render_pages` (`14-ingestion.md` sections 6.8 and 14.5; T-621's marker rule,
T-615 and T-616's Office path at unit level)."""

from __future__ import annotations

import dataclasses
import json
from types import SimpleNamespace
from typing import Any

import pytest
from azure.storage.blob import StandardBlobTier

from docchat_core.ingestion.models import ContentType
from docchat_core.ingestion.names import rendition_page_path
from docchat_ingestion.activities import render_pages
from docchat_ingestion.isolation.runner import RenditionOutcome
from docchat_ingestion.workfiles import RenditionMarker


def facts(kit: SimpleNamespace, data: bytes) -> Any:
    return kit.build_facts(page_count=3, pdf_sha256=bytes.fromhex(kit.sha(data)))


def marker(kit: SimpleNamespace, data: bytes, rules: int) -> bytes:
    return RenditionMarker(
        page_count=3, pdf_sha256=kit.sha(data), rendition_rules_version=rules, document_pdf="built"
    ).encode()


async def test_t192_render_writes_every_page_then_the_marker_last(
    kit: SimpleNamespace, world: Any
) -> None:
    ids = kit.Ids()
    plan = kit.make_plan(ids)
    world.documents.blobs[plan.source_blob_path] = kit.PDF_BYTES
    stale = rendition_page_path(ids.index, ids.document, 1, 7)
    world.renditions.blobs[stale] = b"left by an earlier conversion"
    world.renditions.blobs[plan.marker_path()] = marker(kit, b"an earlier pdf", 1)
    world.sql.script(kit.fence_ok(), facts(kit, kit.PDF_BYTES), kit.Result(), kit.Result())
    output = await render_pages.run(kit.plan_payload(ids, plan), world.provider)
    assert output == {**output, "ok": True, "pages": 3, "skipped": False, "documentPdf": "built"}
    renditions = [
        entry for entry in world.log if entry[0] == "renditions" and entry[1] != "download"
    ]
    assert renditions[0] == ("renditions", "delete", plan.marker_path())
    assert renditions[-1] == ("renditions", "upload", plan.marker_path())
    for number in (1, 2, 3):
        path = rendition_page_path(ids.index, ids.document, 1, number)
        assert world.renditions.kwargs[path]["content_settings"].content_type == "application/pdf"
    assert stale not in world.renditions.blobs
    stored = json.loads(world.renditions.blobs[plan.marker_path()])
    assert stored == {
        "pageCount": 3,
        "pdfSha256": kit.sha(kit.PDF_BYTES),
        "renditionRulesVersion": kit.RULES_VERSION,
        "documentPdf": "built",
    }
    status, rules = world.sql.connection.executed[2:]
    assert status.sql.startswith("UPDATE app.document_versions SET document_pdf_status=N'built'")
    assert "converted_sha256" not in status.sql
    assert rules.values["rendition_rules_version"] == kit.RULES_VERSION
    assert "state != N'retired'" in rules.sql
    assert world.pool.calls[0][2] == {"source.bin": kit.PDF_BYTES}


async def test_t621_a_current_marker_only_records_its_values(
    kit: SimpleNamespace, world: Any
) -> None:
    ids = kit.Ids()
    plan = kit.make_plan(ids)
    world.renditions.blobs[plan.marker_path()] = marker(kit, kit.PDF_BYTES, kit.RULES_VERSION)
    world.sql.script(kit.fence_ok(), facts(kit, kit.PDF_BYTES), kit.Result(), kit.Result())
    output = await render_pages.run(kit.plan_payload(ids, plan), world.provider)
    assert output["skipped"] is True
    assert world.pool.calls == []


@pytest.mark.parametrize("stored", ["lower rules", "no rules", "other pdf"])
async def test_t621_any_other_marker_renders_every_page(
    kit: SimpleNamespace, world: Any, stored: str
) -> None:
    ids = kit.Ids()
    plan = kit.make_plan(ids)
    world.documents.blobs[plan.source_blob_path] = kit.PDF_BYTES
    if stored == "lower rules":
        world.services = dataclasses.replace(world.services, rendition_rules_version=2)
        data = marker(kit, kit.PDF_BYTES, 1)
    elif stored == "no rules":
        data = json.dumps(
            {"pageCount": 3, "pdfSha256": kit.sha(kit.PDF_BYTES), "documentPdf": "built"}
        ).encode()
    else:
        data = marker(kit, b"another pdf", kit.RULES_VERSION)
    world.renditions.blobs[plan.marker_path()] = data
    world.sql.script(kit.fence_ok(), facts(kit, kit.PDF_BYTES), kit.Result(), kit.Result())
    output = await render_pages.run(kit.plan_payload(ids, plan), world.provider)
    assert output["skipped"] is False
    assert world.pool.commands() == ["render"]


async def test_a_storage_error_on_the_marker_writes_no_page(
    kit: SimpleNamespace, world: Any
) -> None:
    ids = kit.Ids()
    plan = kit.make_plan(ids)
    world.renditions.blobs[plan.marker_path()] = b"{}"
    world.renditions.fail.add("delete")
    world.sql.script(kit.fence_ok(), facts(kit, kit.PDF_BYTES))
    output = await render_pages.run(kit.plan_payload(ids, plan), world.provider)
    assert output["errorCode"] == "storage_unavailable"
    assert world.pool.calls == []


async def test_render_too_large_deletes_the_uploaded_pages(
    kit: SimpleNamespace, world: Any
) -> None:
    ids = kit.Ids()
    plan = kit.make_plan(ids)
    world.documents.blobs[plan.source_blob_path] = kit.PDF_BYTES
    world.pool.rendition = RenditionOutcome("render_too_large", uploaded_pages=(1, 2))
    world.pool.render_uploads = [1, 2]
    world.sql.script(kit.fence_ok(), facts(kit, kit.PDF_BYTES))
    output = await render_pages.run(kit.plan_payload(ids, plan), world.provider)
    assert output["errorCode"] == "render_too_large"
    assert not [name for name in world.renditions.blobs if "/pages/" in name]
    assert plan.marker_path() not in world.renditions.blobs


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("parse_timeout", "parse_timeout"),
        ("parse_memory_exceeded", "parse_memory_exceeded"),
        ("render_failed", "render_failed"),
        ("corrupt_document", "render_failed"),
    ],
)
async def test_render_failures(kit: SimpleNamespace, world: Any, code: str, expected: str) -> None:
    ids = kit.Ids()
    plan = kit.make_plan(ids)
    world.documents.blobs[plan.source_blob_path] = kit.PDF_BYTES
    world.pool.rendition = RenditionOutcome(code)
    world.sql.script(kit.fence_ok(), facts(kit, kit.PDF_BYTES))
    output = await render_pages.run(kit.plan_payload(ids, plan), world.provider)
    assert output["errorCode"] == expected


async def test_a_whole_document_too_large_is_recorded_not_failed(
    kit: SimpleNamespace, world: Any
) -> None:
    ids = kit.Ids()
    plan = kit.make_plan(ids)
    world.documents.blobs[plan.source_blob_path] = kit.PDF_BYTES
    world.renditions.blobs[plan.document_pdf_path()] = b"an earlier whole document"
    world.pool.rendition = RenditionOutcome(None, 3, "too_large", (1, 2, 3))
    world.pool.render_uploads = [1, 2, 3]
    world.sql.script(kit.fence_ok(), facts(kit, kit.PDF_BYTES), kit.Result(), kit.Result())
    output = await render_pages.run(kit.plan_payload(ids, plan), world.provider)
    assert output["documentPdf"] == "too_large"
    assert plan.document_pdf_path() not in world.renditions.blobs
    assert json.loads(world.renditions.blobs[plan.marker_path()])["documentPdf"] == "too_large"


async def test_t615_a_converted_pdf_is_stored_after_its_renditions(
    kit: SimpleNamespace, world: Any
) -> None:
    ids = kit.Ids()
    plan = kit.make_plan(ids, content_type=ContentType.DOCX)
    world.work.blobs[plan.work_pdf_path()] = kit.CONVERTED_BYTES
    world.sql.script(
        kit.fence_ok(), facts(kit, kit.CONVERTED_BYTES), kit.Result(), kit.Result(), kit.Result()
    )
    output = await render_pages.run(kit.plan_payload(ids, plan), world.provider)
    assert output["ok"] is True, output
    converted = plan.conversion.converted_blob_path
    assert world.documents.blobs[converted] == kit.CONVERTED_BYTES
    assert world.documents.kwargs[converted]["standard_blob_tier"] is StandardBlobTier.COOL
    uploads = [entry for entry in world.log if entry[1] == "upload"]
    assert uploads.index(("documents", "upload", converted)) > uploads.index(
        ("renditions", "upload", rendition_page_path(ids.index, ids.document, 1, 3))
    )
    assert ("documents", "download", plan.source_blob_path) not in world.log
    clear, status, rules = world.sql.connection.executed[2:]
    assert clear.sql.startswith("UPDATE app.document_versions SET converted_sha256=")
    assert "converted_sha256 IS NOT NULL" in clear.sql
    assert status.values["converted_sha256"] == bytes.fromhex(kit.sha(kit.CONVERTED_BYTES))
    assert kit.CONVERTER in status.values.values()
    assert rules.values["rendition_rules_version"] == kit.RULES_VERSION


async def test_a_reused_converted_pdf_keeps_its_converter_version(
    kit: SimpleNamespace, world: Any
) -> None:
    ids = kit.Ids()
    plan = kit.make_plan(ids, content_type=ContentType.DOCX, convert=False)
    world.work.blobs[plan.work_pdf_path()] = kit.CONVERTED_BYTES
    world.sql.script(
        kit.fence_ok(), facts(kit, kit.CONVERTED_BYTES), kit.Result(), kit.Result(), kit.Result()
    )
    output = await render_pages.run(kit.plan_payload(ids, plan), world.provider)
    assert output["ok"] is True
    assert plan.conversion.converted_blob_path not in world.documents.blobs
    status = world.sql.connection.executed[3]
    assert kit.CONVERTER not in status.values.values()
    assert "coalesce(" in status.sql


async def test_render_needs_the_pdf_extract_recorded(kit: SimpleNamespace, world: Any) -> None:
    ids = kit.Ids()
    world.sql.script(kit.fence_ok(), kit.build_facts(pdf_sha256=None))
    output = await render_pages.run(kit.plan_payload(ids, kit.make_plan(ids)), world.provider)
    assert output["errorCode"] == "internal_error"


def test_failure_code_defaults_to_render_failed() -> None:
    assert render_pages.failure_code(RenditionOutcome(None)) == "render_failed"
