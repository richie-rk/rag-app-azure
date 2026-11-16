"""`document_item_orchestrator`'s `ingest` operation through the orchestration harness with the
real Extract, Enrich and Chunk activities and the real page-wise method (`14-ingestion.md`
sections 6.1, 6.3 to 6.5 and 10).

The orchestrator's own rules are tested with scripted activities in
`tests/unit/orchestrators/test_document_item_orchestrator.py`; this test lives beside the
activity fakes it needs and proves the steps hand each other their work files: Extract's parts,
part by part through the orchestrator's `nextPart`, Enrich's summaries and Chunk's chunks file,
with the usage the orchestrator sums from their outputs.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

from azure.durable_functions import OrchestrationRuntimeStatus

from docchat_core.ingestion.models import ItemOperation
from docchat_core.ingestion.names import chunks_path, extract_part_path, table_summaries_path
from docchat_ingestion.activities import chunk_document, enrich_document, extract_document
from docchat_ingestion.activities.validate_item import ValidateResult
from docchat_ingestion.orchestrators import document_item_orchestrator as orchestrator
from docchat_ingestion.workfiles import decode_chunks, decode_table_summaries
from docchat_testing.clock import FakeClock
from docchat_testing.durable import OrchestrationHarness

OK: dict[str, Any] = {"ok": True, "errorCode": None, "retryable": False, "retryAfterSeconds": None}
SCRIPTED: dict[str, dict[str, Any]] = {
    "embed_chunks": {**OK, "done": True, "embeddingTokens": 30},
    "index_chunks": {**OK, "indexed": 3},
    "render_pages": {**OK, "pages": 3, "skipped": False, "documentPdf": "built"},
    "flip_chunk_set": {
        **OK,
        "outcome": "flipped",
        "code": None,
        "retiredChunkSetIds": [],
        "supersededVersionId": None,
        "buildingItemQueued": False,
    },
    "clean_up_item": {**OK, "deletedChunks": 0, "purgedVersion": False},
    "record_item_outcome": OK,
}


async def test_extract_enrich_and_chunk_hand_on_their_work_files(
    kit: SimpleNamespace, world: Any
) -> None:
    ids = kit.Ids()
    plan = kit.make_plan(ids)
    world.documents.blobs[plan.source_blob_path] = kit.PDF_BYTES
    world.sql.script(
        # Extract, pages 1 and 2: the PDF's hash, the inspect's seconds, the page count and the
        # partition's seconds.
        kit.fence_ok(),
        kit.build_facts(page_count=None),
        *(kit.Result() for _ in range(4)),
        # Extract, page 3.
        kit.fence_ok(),
        kit.build_facts(),
        kit.Result(),
        kit.Result(),
        # Enrich.
        kit.fence_ok(),
        kit.build_facts(),
        # Chunk and its count.
        kit.fence_ok(),
        kit.build_facts(),
        kit.Result(rowcount=1),
    )
    calls: list[str] = []
    outcomes: list[dict[str, Any]] = []
    harness = OrchestrationHarness(FakeClock(datetime(2026, 3, 1, 9, tzinfo=UTC)))
    harness.register_orchestrator(orchestrator.NAME, orchestrator.document_item_orchestrator)

    async def validate(payload: dict[str, Any]) -> dict[str, Any]:
        calls.append("validate_item")
        return ValidateResult.build(plan).model_dump(mode="json")

    def real(name: str, module: Any) -> Any:
        async def activity(payload: dict[str, Any]) -> Any:
            calls.append(name)
            return await module.run(payload, world.provider)

        return activity

    def scripted(name: str) -> Any:
        def activity(payload: dict[str, Any]) -> dict[str, Any]:
            calls.append(name)
            if name == "record_item_outcome":
                outcomes.append(payload)
            return SCRIPTED[name]

        return activity

    harness.register_activity("validate_item", validate)
    harness.register_activity("extract_document", real("extract_document", extract_document))
    harness.register_activity("enrich_document", real("enrich_document", enrich_document))
    harness.register_activity("chunk_document", real("chunk_document", chunk_document))
    for name in SCRIPTED:
        harness.register_activity(name, scripted(name))
    item = {
        **kit.envelope(ids),
        "operation": ItemOperation.INGEST.value,
        "retry": {"maxAttempts": 1, "firstDelaySeconds": 15, "maxDelaySeconds": 480},
    }

    instance = await harness.start(
        orchestrator.NAME, item, instance_id=f"docchat-item-{ids.item}-1"
    )
    status = await harness.run_to_completion(instance)

    assert status is OrchestrationRuntimeStatus.Completed
    assert calls == [
        "validate_item",
        "extract_document",
        "extract_document",
        "enrich_document",
        "chunk_document",
        "embed_chunks",
        "index_chunks",
        "render_pages",
        "flip_chunk_set",
        "clean_up_item",
        "record_item_outcome",
    ]
    prefix = plan.work_prefix
    assert extract_part_path(prefix, 1, 2) in world.work.blobs
    assert extract_part_path(prefix, 3, 3) in world.work.blobs
    (summary,) = decode_table_summaries(world.work.blobs[table_summaries_path(prefix)])
    assert summary.page_number == 1
    chunks = decode_chunks(world.work.blobs[chunks_path(prefix, plan.chunk_set_id)])
    assert [chunk.page_number for chunk in chunks.chunks] == [1, 2, 3]
    assert chunks.chunks[0].chunk_text.endswith(f"{summary.summary}\n\n{kit.TABLE_HTML}")
    (outcome,) = outcomes
    assert (outcome["state"], outcome["code"]) == ("succeeded", None)
    assert outcome["usage"] == {
        "embeddingTokens": 30,
        "summaryInputTokens": 120,
        "summaryOutputTokens": 40,
        "docIntelPages": 0,
    }
    assert world.pool.taken == 2
    world.sql.connection.finished()
