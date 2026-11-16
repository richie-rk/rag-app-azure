"""Unit tests of `clean_up_item` (`14-ingestion.md` sections 6.10 and 13.4)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from uuid import uuid4

from docchat_core.ingestion.names import converted_path, rendition_page_path, source_path
from docchat_ingestion.activities import clean_up_item


def flip_output(retired: list[Any], superseded: Any = None) -> dict[str, Any]:
    return {
        "ok": True,
        "errorCode": None,
        "retryable": False,
        "retryAfterSeconds": None,
        "outcome": "flipped",
        "code": None,
        "retiredChunkSetIds": [str(chunk_set) for chunk_set in retired],
        "supersededVersionId": None if superseded is None else str(superseded),
        "buildingItemQueued": False,
    }


async def test_after_a_flip(kit: SimpleNamespace, world: Any) -> None:
    ids = kit.Ids()
    plan = kit.make_plan(ids)
    old_set, superseded = uuid4(), uuid4()
    for ordinal in range(3):
        world.search.documents[f"{ids.document}_{old_set}_{ordinal}"] = {}
    world.search.documents[f"{ids.document}_{ids.chunk_set}_0"] = {}
    old_source = source_path(ids.index, ids.document, 1)
    world.documents.blobs[old_source] = b"old source"
    world.renditions.blobs[rendition_page_path(ids.index, ids.document, 1, 1)] = b"page"
    world.renditions.blobs[f"{ids.index}/{ids.document}/1/complete.json"] = b"{}"
    world.renditions.blobs[rendition_page_path(ids.index, ids.document, 2, 1)] = b"current"
    world.work.blobs[plan.work_pdf_path()] = b"work"
    world.sql.script(
        kit.fence_ok(),
        kit.Result(
            rows=[
                kit.Row(
                    id=superseded,
                    search_index_id=ids.index,
                    document_id=ids.document,
                    version_number=1,
                    blob_path=old_source,
                )
            ]
        ),
        kit.Result(
            rows=[
                kit.Row(
                    id=old_set,
                    document_id=ids.document,
                    chunk_count=3,
                    name="docchat-0123456789abcdef0123456789abcdef-g1",
                )
            ]
        ),
        kit.Result(rowcount=1),  # chunks_deleted_at
        kit.Result(rowcount=1),  # storage_purged_at
    )
    payload = kit.plan_payload(ids, plan, flip=flip_output([old_set], superseded), mode="flipped")
    output = await clean_up_item.run(payload, world.provider)
    assert output == {**output, "ok": True, "deletedChunks": 3, "purgedVersion": True}
    assert list(world.search.documents) == [f"{ids.document}_{ids.chunk_set}_0"]
    assert old_source not in world.documents.blobs
    assert ("documents", "delete", converted_path(ids.index, ids.document, 1)) in world.log
    assert list(world.renditions.blobs) == [rendition_page_path(ids.index, ids.document, 2, 1)]
    assert world.work.blobs == {}
    retired_query = world.sql.connection.executed[2]
    assert "state = N'retired'" in retired_query.sql
    assert "N'active', N'building'" in retired_query.sql


async def test_discard_mode_cleans_the_build(kit: SimpleNamespace, world: Any) -> None:
    ids = kit.Ids()
    plan = kit.make_plan(ids)
    world.sql.script(
        kit.fence_ok(),
        kit.Result(
            rows=[kit.Row(id=ids.chunk_set, document_id=ids.document, chunk_count=None, name="x")]
        ),
        kit.Result(rowcount=1),
    )
    output = await clean_up_item.run(
        kit.plan_payload(ids, plan, flip=None, mode="discard"), world.provider
    )
    assert output["ok"] is True
    assert output["deletedChunks"] == 0
    assert world.search.deletes == []


async def test_a_storage_failure_is_cleanup_failed(kit: SimpleNamespace, world: Any) -> None:
    ids = kit.Ids()
    plan = kit.make_plan(ids)
    world.work.fail.add("list")
    world.sql.script(kit.fence_ok(), kit.Result(rows=[]))
    output = await clean_up_item.run(
        kit.plan_payload(ids, plan, flip=None, mode="discard"), world.provider
    )
    assert output["errorCode"] == "cleanup_failed"
    assert output["retryable"] is True


async def test_the_mode_and_the_flip_go_together(kit: SimpleNamespace, world: Any) -> None:
    ids = kit.Ids()
    output = await clean_up_item.run(
        kit.plan_payload(ids, kit.make_plan(ids), flip=None, mode="flipped"), world.provider
    )
    assert output["errorCode"] == "internal_error"
