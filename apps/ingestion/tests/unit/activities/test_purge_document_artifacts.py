"""Unit tests of `purge_document_artifacts` (`14-ingestion.md` section 6.12)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from uuid import uuid4

from azure.core.exceptions import ResourceNotFoundError, ServiceRequestError

from docchat_core.ingestion.names import source_path
from docchat_ingestion.activities import purge_document_artifacts


def sets(kit: SimpleNamespace, ids: Any, count: int) -> Any:
    return kit.Result(
        rows=[
            kit.Row(id=uuid4(), document_id=ids.document, chunk_count=2, name="index-a")
            for _ in range(count)
        ]
    )


def versions(kit: SimpleNamespace, ids: Any, numbers: list[int]) -> Any:
    return kit.Result(
        rows=[
            kit.Row(
                id=uuid4(),
                search_index_id=ids.index,
                document_id=ids.document,
                version_number=number,
                blob_path=source_path(ids.index, ids.document, number),
            )
            for number in numbers
        ]
    )


async def test_the_document_is_purged(kit: SimpleNamespace, world: Any) -> None:
    ids = kit.Ids()
    for number in (1, 2):
        world.documents.blobs[source_path(ids.index, ids.document, number)] = b"source"
    world.sql.script(
        kit.fence_ok(),
        kit.Result(rows=[(ids.document,)]),
        sets(kit, ids, 2),
        versions(kit, ids, [1, 2]),
        *(kit.Result(rowcount=1) for _ in range(4)),
    )
    output = await purge_document_artifacts.run(kit.envelope(ids), world.provider)
    assert output == {
        **output,
        "ok": True,
        "done": True,
        "deletedChunks": 4,
        "purgedVersions": 2,
    }
    assert world.documents.blobs == {}
    assert len(world.search.deletes) == 2
    world.sql.connection.finished()


async def test_the_budget_ends_a_call_early(kit: SimpleNamespace, world: Any) -> None:
    ids = kit.Ids()
    world.clock.step = 700.0
    world.sql.script(
        kit.fence_ok(),
        kit.Result(rows=[(ids.document,)]),
        sets(kit, ids, 3),
        versions(kit, ids, [1]),
        *(kit.Result(rowcount=1) for _ in range(2)),
    )
    output = await purge_document_artifacts.run(kit.envelope(ids), world.provider)
    assert output["done"] is False
    assert output["deletedChunks"] < 6


async def test_a_gone_index_counts_as_deleted(kit: SimpleNamespace, world: Any) -> None:
    ids = kit.Ids()
    world.search.errors.append(ResourceNotFoundError("index gone"))
    world.sql.script(
        kit.fence_ok(),
        kit.Result(rows=[(ids.document,)]),
        sets(kit, ids, 1),
        versions(kit, ids, []),
        kit.Result(rowcount=1),
    )
    output = await purge_document_artifacts.run(kit.envelope(ids), world.provider)
    assert output["done"] is True
    assert output["deletedChunks"] == 0


async def test_a_search_failure_is_cleanup_failed(kit: SimpleNamespace, world: Any) -> None:
    ids = kit.Ids()
    world.search.errors.append(ServiceRequestError("unreachable"))
    world.sql.script(
        kit.fence_ok(),
        kit.Result(rows=[(ids.document,)]),
        sets(kit, ids, 1),
        versions(kit, ids, []),
    )
    output = await purge_document_artifacts.run(kit.envelope(ids), world.provider)
    assert output["errorCode"] == "cleanup_failed"


async def test_a_named_document_must_be_the_items(kit: SimpleNamespace, world: Any) -> None:
    ids = kit.Ids()
    world.sql.script(kit.fence_ok(), kit.Result(rows=[(ids.document,)]))
    payload = {**kit.envelope(ids), "documentId": str(uuid4())}
    output = await purge_document_artifacts.run(payload, world.provider)
    assert output["errorCode"] == "internal_error"
