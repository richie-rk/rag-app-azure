"""Unit tests of what the document activities share (`14-ingestion.md` sections 4.4 to 4.9, 6.2,
6.10 and 13.4): the plan's checks, the run wrapper, the blob and chunk helpers, the bounded
concurrency and the process's services."""

from __future__ import annotations

import asyncio
import io
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from pydantic import ValidationError

from docchat_core.clients import ClientRegistry
from docchat_core.ingestion.models import ActivityResult, ContentType, ItemEnvelope, ItemStage
from docchat_core.ingestion.settings import TokenEncoding
from docchat_core.telemetry import TestExporters
from docchat_ingestion.activities import documents, runtime
from docchat_ingestion.activities.documents import (
    BuildFacts,
    ConversionPlan,
    ItemFailure,
    LeaseLost,
    PartRange,
    PlanInput,
    check_work_prefix,
    delete_blob,
    delete_chunks,
    delete_names,
    delete_prefix,
    download_into,
    method_context,
    pdf_location,
    read_blob,
    run_bounded,
    run_document_activity,
    write_blob,
)


def test_the_plan_checks_its_paths_and_conversion(kit: SimpleNamespace) -> None:
    ids = kit.Ids()
    plan = kit.make_plan(ids)
    check_work_prefix(ids.run, plan)
    with pytest.raises(ValueError, match="another run"):
        check_work_prefix(uuid4(), plan)
    dumped = plan.model_dump(mode="json")
    with pytest.raises(ValidationError):
        documents.BuildPlan.model_validate({**dumped, "contentType": ContentType.DOCX.value})
    office = kit.make_plan(ids, content_type=ContentType.PPTX).model_dump(mode="json")
    office["conversion"]["convertedBlobPath"] = f"{ids.index}/{uuid4()}/1/converted.pdf"
    with pytest.raises(ValidationError, match="belongs to this version"):
        documents.BuildPlan.model_validate(office)
    with pytest.raises(ValidationError):
        ConversionPlan(
            convert=True,
            converted_blob_path=office["conversion"]["convertedBlobPath"],
            converted_sha256="a" * 64,
        )
    with pytest.raises(ValidationError):
        PartRange(first_page=3, last_page=2)
    assert plan.ids()["docchat.chunk_set_id"] == ids.chunk_set


def test_the_method_context_names_the_builds_pdf(kit: SimpleNamespace) -> None:
    ids = kit.Ids()
    office = kit.make_plan(ids, content_type=ContentType.DOCX)
    item = PlanInput.model_validate(kit.plan_payload(ids, office), by_alias=True, by_name=False)
    facts = BuildFacts(
        page_count=2,
        pdf_sha256="b" * 64,
        chunk_count=None,
        parse_seconds_used=0,
        chunk_set_state="building",
    )
    context = method_context(item, facts)
    assert context.pdf_container == "ingestion-work"
    assert context.pdf_sha256 == "b" * 64
    assert context.encoding is TokenEncoding.CL100K_BASE
    with pytest.raises(ValueError, match="conversion call"):
        pdf_location(office, BuildFacts(None, None, None, 0, "building"))
    with pytest.raises(ValueError, match="page count"):
        method_context(item, BuildFacts(None, "b" * 64, None, 0, "building"))


async def test_download_into_writes_every_sink_and_checks_the_hash(
    world: Any, kit: SimpleNamespace
) -> None:
    world.documents.blobs["a"] = kit.PDF_BYTES
    first, second = io.BytesIO(), io.BytesIO()
    downloaded = await download_into(
        world.documents, "a", (first, second), expected_sha256=kit.sha(kit.PDF_BYTES), keep=True
    )
    assert first.getvalue() == second.getvalue() == kit.PDF_BYTES
    assert downloaded.size == len(kit.PDF_BYTES)
    assert downloaded.data == kit.PDF_BYTES
    with pytest.raises(ItemFailure, match="source_changed"):
        await download_into(world.documents, "a", (), expected_sha256="0" * 64)
    with pytest.raises(ItemFailure, match="source_missing"):
        await download_into(world.documents, "b", (), expected_sha256="0" * 64)
    world.documents.fail.add("download")
    with pytest.raises(ItemFailure, match="storage_unavailable"):
        await download_into(world.documents, "a", (), expected_sha256="0" * 64)


async def test_blob_helpers(world: Any) -> None:
    container = world.renditions
    assert await read_blob(container, "missing") is None
    await write_blob(container, "x", b"1", content_settings=None)
    assert container.kwargs["x"]["overwrite"] is True
    assert await read_blob(container, "x") == b"1"
    await delete_blob(container, "x", code="cleanup_failed")
    await delete_blob(container, "x", code="cleanup_failed")
    for name in range(300):
        container.blobs[f"p/{name}"] = b""
    container.blobs["q/1"] = b""
    assert await delete_prefix(container, "p/", code="cleanup_failed") == 300
    assert list(container.blobs) == ["q/1"]
    assert await delete_names(container, ["absent"], code="cleanup_failed") == 1
    container.fail = {"upload", "download", "delete", "list", "delete_blobs"}
    with pytest.raises(ItemFailure, match="storage_unavailable"):
        await write_blob(container, "y", b"")
    with pytest.raises(ItemFailure, match="storage_unavailable"):
        await read_blob(container, "q/1")
    with pytest.raises(ItemFailure, match="cleanup_failed"):
        await delete_blob(container, "q/1", code="cleanup_failed")
    with pytest.raises(ItemFailure, match="cleanup_failed"):
        await delete_prefix(container, "q/", code="cleanup_failed")
    with pytest.raises(ItemFailure, match="cleanup_failed"):
        await delete_names(container, ["q/1"], code="cleanup_failed")


async def test_a_refused_batch_delete_is_the_callers_code(world: Any) -> None:
    class Refusing:
        async def delete_blobs(self, *blobs: str, **kwargs: Any) -> Any:
            async def responses() -> Any:
                yield SimpleNamespace(status_code=403)

            return responses()

    with pytest.raises(ItemFailure, match="cleanup_failed"):
        await delete_names(Refusing(), ["a"], code="cleanup_failed")  # type: ignore[arg-type]


async def test_chunks_are_deleted_by_key_in_batches_of_500(world: Any) -> None:
    document, chunk_set = uuid4(), uuid4()
    for ordinal in range(1200):
        world.search.documents[f"{document}_{chunk_set}_{ordinal}"] = {}
    assert await delete_chunks(world.search, document, chunk_set, 1200) == 1200
    assert [len(batch) for batch in world.search.deletes] == [500, 500, 200]
    assert world.search.documents == {}


async def test_run_bounded_limits_and_raises_the_first_failure() -> None:
    running = peak = 0

    async def work(value: int) -> None:
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0)
        running -= 1
        if value == 5:
            raise ItemFailure("embedding_failed")

    with pytest.raises(ItemFailure, match="embedding_failed"):
        await run_bounded(range(8), 2, work)
    assert peak <= 2

    async def unexpected(value: int) -> None:
        raise KeyError(value)

    with pytest.raises(KeyError):
        await run_bounded([1], 1, unexpected)


def test_the_first_failure_of_nested_groups() -> None:
    group = BaseExceptionGroup(
        "outer", [ValueError("a"), BaseExceptionGroup("inner", [LeaseLost()])]
    )
    assert isinstance(documents._first_failure(group), LeaseLost)


async def test_the_run_wrapper_records_the_span_and_the_stage(
    kit: SimpleNamespace, world: Any, telemetry: TestExporters
) -> None:
    ids = kit.Ids()

    async def body(services: Any, item: ItemEnvelope, span: Any) -> ActivityResult:
        return ActivityResult.success()

    output = await run_document_activity(
        "chunk_document",
        kit.envelope(ids),
        ItemEnvelope,
        body,
        stage=ItemStage.CHUNK,
        provider=world.provider,
    )
    assert output["ok"] is True
    (span,) = [
        span
        for span in telemetry.spans.get_finished_spans()
        if span.name == "docchat.ingestion.activity"
    ]
    attributes = dict(span.attributes or {})
    assert attributes["docchat.activity_name"] == "chunk_document"
    assert attributes["docchat.outcome"] == "ok"
    assert format(span.context.trace_id, "032x") == kit.TRACEPARENT.split("-")[1]


async def test_the_deadline_is_activity_timeout(
    kit: SimpleNamespace, world: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(documents.DEADLINES, "chunk_document", 0)

    async def body(services: Any, item: ItemEnvelope, span: Any) -> ActivityResult:
        await asyncio.sleep(1)
        return ActivityResult.success()

    output = await run_document_activity(
        "chunk_document",
        kit.envelope(kit.Ids()),
        ItemEnvelope,
        body,
        stage=ItemStage.CHUNK,
        provider=world.provider,
    )
    assert output["errorCode"] == "activity_timeout"
    assert output["retryable"] is True


async def test_an_unexpected_exception_is_internal_error(kit: SimpleNamespace, world: Any) -> None:
    async def body(services: Any, item: ItemEnvelope, span: Any) -> ActivityResult:
        raise RuntimeError("library text that might quote a document")

    output = await run_document_activity(
        "chunk_document",
        kit.envelope(kit.Ids()),
        ItemEnvelope,
        body,
        stage=ItemStage.CHUNK,
        provider=world.provider,
    )
    assert output["errorCode"] == "internal_error"


async def test_the_process_services_are_built_once(
    monkeypatch: pytest.MonkeyPatch, worker_env: pytest.MonkeyPatch
) -> None:

    for name, value in {
        "DOCCHAT_SEARCH_ENDPOINT": "https://srch-docchat-dev.search.windows.net",
        "DOCCHAT_OPENAI_ENDPOINT": "https://oai-docchat-dev.openai.azure.com",
        "DOCCHAT_DOCINTEL_ENDPOINT": "https://di-docchat-dev.cognitiveservices.azure.com",
    }.items():
        worker_env.setenv(name, value)
    process = SimpleNamespace(
        sql=object(), clock=object(), rendition_rules_version=1, workload_principal_id=uuid4()
    )

    async def fake_runtime() -> Any:
        return process

    registry = SimpleNamespace(
        blob_container=lambda name: f"container {name}",
        search_client=lambda name: None,
        openai=object(),
    )
    monkeypatch.setattr(runtime, "runtime", fake_runtime)
    monkeypatch.setattr(runtime, "method_registry", lambda: "registry")
    monkeypatch.setattr(ClientRegistry, "current", classmethod(lambda cls: registry))
    monkeypatch.setattr(documents, "_SLOT", documents._ServicesSlot())
    services = await documents.document_services()
    assert services is await documents.document_services()
    assert str(services.documents) == "container documents"
    assert str(services.work) == "container ingestion-work"
    assert services.parse_pool.memory_limit_mb == 2048
