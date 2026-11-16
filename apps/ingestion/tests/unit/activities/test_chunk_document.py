"""Unit tests of `chunk_document` (`14-ingestion.md` sections 6.5, 10.6 and 11.6): the activity
calls the real method's `chunk`, which reads the plan's extract files and the summaries file and
writes the chunks file; `FakeMethod` stands in only for the activity's own limits."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from docchat_core.ingestion.names import (
    chunks_path,
    extract_document_path,
    extract_part_path,
    table_summaries_path,
)
from docchat_ingestion.activities import chunk_document
from docchat_ingestion.methods.tables import table_sha256
from docchat_ingestion.workfiles import (
    ExtractDocument,
    PageOffset,
    SummaryOutcome,
    TableSummaryLine,
    decode_chunks,
    encode_table_summaries,
)


def part(first: int, last: int, *, empty: bool = False, table: str | None = None) -> bytes:
    pages = []
    for page in range(first, last + 1):
        elements: list[dict[str, Any]] = []
        if not empty:
            elements = [
                {"kind": "title", "text": f"Section {page}", "tableHtml": None, "headingLevel": 1},
                {"kind": "text", "text": f"Text of page {page}.", "tableHtml": None,
                 "headingLevel": None},
            ]  # fmt: skip
            if table is not None and page == first:
                elements.append(
                    {"kind": "table", "text": "t", "tableHtml": table, "headingLevel": None}
                )
        pages.append({"pageNumber": page, "elements": elements})
    return json.dumps(
        {
            "formatVersion": 1,
            "method": "pagewise",
            "pageCount": 3,
            "firstPage": first,
            "lastPage": last,
            "pages": pages,
        }
    ).encode()


def chunks_of(world: Any, plan: Any) -> Any:
    return decode_chunks(world.work.blobs[chunks_path(plan.work_prefix, plan.chunk_set_id)])


async def test_the_page_wise_chunks_and_their_count(kit: SimpleNamespace, world: Any) -> None:
    ids = kit.Ids()
    plan = kit.make_plan(ids)
    world.work.blobs[extract_part_path(plan.work_prefix, 1, 2)] = part(1, 2, table=kit.TABLE_HTML)
    world.work.blobs[extract_part_path(plan.work_prefix, 3, 3)] = part(3, 3)
    summary = TableSummaryLine(
        table_sha256=table_sha256(kit.TABLE_HTML),
        page_number=1,
        outcome=SummaryOutcome.SUMMARISED,
        summary="Tea costs 12 euros.",
        input_tokens=5,
        output_tokens=4,
    )
    world.work.blobs[table_summaries_path(plan.work_prefix)] = encode_table_summaries([summary])
    world.sql.script(kit.fence_ok(), kit.build_facts(), kit.Result(rowcount=1))
    output = await chunk_document.run(kit.plan_payload(ids, plan), world.provider)
    assert output["ok"] is True, output
    assert output["chunkCount"] == 3
    chunks = chunks_of(world, plan)
    assert chunks.header.chunk_set_id == ids.chunk_set
    assert chunks.header.title == "Annual report"
    assert [(chunk.page_number, chunk.heading_path) for chunk in chunks.chunks] == [
        (1, ["Section 1"]),
        (2, ["Section 2"]),
        (3, ["Section 3"]),
    ]
    assert chunks.chunks[0].chunk_text.endswith(f"Tea costs 12 euros.\n\n{kit.TABLE_HTML}")
    (recorded,) = world.sql.connection.statements("UPDATE app.chunk_sets SET chunk_count")
    assert recorded.values["chunk_count"] == 3
    assert "state = N'building'" in recorded.sql
    world.sql.connection.finished()


async def test_the_recursive_chunks(kit: SimpleNamespace, world: Any) -> None:
    ids = kit.Ids()
    plan = kit.make_plan(ids, method="recursive")
    markdown = "# Intro\n\nFirst page words.\n\n# Next\n\nSecond page words."
    document = ExtractDocument(
        page_count=3,
        markdown=markdown,
        page_offsets=[
            PageOffset(page_number=1, offset=0, length=28),
            PageOffset(page_number=2, offset=28, length=len(markdown) - 28),
        ],
        doc_intel_pages=3,
    )
    world.work.blobs[extract_document_path(plan.work_prefix)] = document.encode()
    world.sql.script(kit.fence_ok(), kit.build_facts(), kit.Result(rowcount=1))
    output = await chunk_document.run(kit.plan_payload(ids, plan), world.provider)
    assert output["chunkCount"] == 2
    chunks = chunks_of(world, plan)
    assert [(chunk.page_number, chunk.heading_path) for chunk in chunks.chunks] == [
        (1, ["Intro"]),
        (2, ["Next"]),
    ]


async def test_no_text_is_no_extractable_text(kit: SimpleNamespace, world: Any) -> None:
    ids = kit.Ids()
    plan = kit.make_plan(ids)
    world.work.blobs[extract_part_path(plan.work_prefix, 1, 2)] = part(1, 2, empty=True)
    world.work.blobs[extract_part_path(plan.work_prefix, 3, 3)] = part(3, 3, empty=True)
    world.sql.script(kit.fence_ok(), kit.build_facts())
    output = await chunk_document.run(kit.plan_payload(ids, plan), world.provider)
    assert (output["errorCode"], output["retryable"]) == ("no_extractable_text", False)
    assert chunks_path(plan.work_prefix, plan.chunk_set_id) not in world.work.blobs
    assert not world.sql.connection.statements("UPDATE app.chunk_sets SET chunk_count")


@pytest.mark.parametrize(
    ("count", "code"), [(0, "no_extractable_text"), (100_001, "chunk_limit_exceeded")]
)
async def test_the_activity_holds_the_chunk_limits_whatever_the_method(
    kit: SimpleNamespace, world: Any, count: int, code: str
) -> None:
    pagewise, _ = world.fake_methods()
    ids = kit.Ids()
    world.sql.script(kit.fence_ok(), kit.build_facts())
    pagewise.chunk_count = count
    output = await chunk_document.run(kit.plan_payload(ids, kit.make_plan(ids)), world.provider)
    assert output["errorCode"] == code
    assert output["retryable"] is False
    assert not world.sql.connection.statements("UPDATE app.chunk_sets SET chunk_count")


async def test_missing_extract_files_are_internal_error(kit: SimpleNamespace, world: Any) -> None:
    ids = kit.Ids()
    plan = kit.make_plan(ids)
    # A part an earlier claim wrote with other ranges isn't one of the plan's.
    world.work.blobs[extract_part_path(plan.work_prefix, 1, 3)] = part(1, 3)
    world.sql.script(kit.fence_ok(), kit.build_facts())
    output = await chunk_document.run(kit.plan_payload(ids, plan), world.provider)
    assert (output["errorCode"], output["retryable"]) == ("internal_error", True)


async def test_the_page_count_is_needed(kit: SimpleNamespace, world: Any) -> None:
    ids = kit.Ids()
    world.sql.script(kit.fence_ok(), kit.build_facts(page_count=None))
    output = await chunk_document.run(kit.plan_payload(ids, kit.make_plan(ids)), world.provider)
    assert output["errorCode"] == "internal_error"
