"""Unit tests of `enrich_document` (`14-ingestion.md` sections 6.4, 9.1 and 12): the activity calls
the real page-wise method's `enrich`, whose table summaries go through the real
`OpenAISummaryModel` to a fake `AsyncOpenAI` client."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import openai

from docchat_core.ingestion.names import extract_part_path, table_summaries_path
from docchat_core.telemetry import TestExporters
from docchat_ingestion.activities import enrich_document
from docchat_ingestion.workfiles import decode_table_summaries

TABLE = "<table><tr><th>Item</th></tr><tr><td>Total 12 EUR</td></tr></table>"


def part(first: int, last: int, tables: int) -> bytes:
    pages = [
        {
            "pageNumber": page,
            "elements": [
                {"kind": "title", "text": f"Page {page}", "tableHtml": None, "headingLevel": 1},
                *(
                    {
                        "kind": "table",
                        "text": "table",
                        "tableHtml": TABLE.replace("12", f"{page}{index}"),
                        "headingLevel": None,
                    }
                    for index in range(tables)
                ),
            ],
        }
        for page in range(first, last + 1)
    ]
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


def store_parts(world: Any, plan: Any) -> None:
    world.work.blobs[extract_part_path(plan.work_prefix, 1, 2)] = part(1, 2, 1)
    world.work.blobs[extract_part_path(plan.work_prefix, 3, 3)] = part(3, 3, 1)


def response(status: int, headers: dict[str, str] | None = None) -> Any:
    return SimpleNamespace(status_code=status, headers=headers or {}, request=None)


async def test_the_recursive_method_is_done_at_once(kit: SimpleNamespace, world: Any) -> None:
    ids = kit.Ids()
    world.sql.script(kit.fence_ok(), kit.build_facts())
    output = await enrich_document.run(
        kit.plan_payload(ids, kit.make_plan(ids, method="recursive")), world.provider
    )
    assert output == {**output, "ok": True, "done": True, "tables": 0}
    assert world.openai.chat.completions.calls == []


async def test_summaries_off_is_done_at_once(kit: SimpleNamespace, world: Any) -> None:
    ids = kit.Ids()
    world.sql.script(kit.fence_ok(), kit.build_facts())
    output = await enrich_document.run(
        kit.plan_payload(ids, kit.make_plan(ids, summarize=False)), world.provider
    )
    assert output["done"] is True
    assert world.openai.chat.completions.calls == []


async def test_every_table_of_every_part_is_summarised(
    kit: SimpleNamespace, world: Any, telemetry: TestExporters
) -> None:
    ids = kit.Ids()
    plan = kit.make_plan(ids)
    store_parts(world, plan)
    world.sql.script(kit.fence_ok(), kit.build_facts())
    output = await enrich_document.run(kit.plan_payload(ids, plan), world.provider)
    assert output == {
        **output,
        "ok": True,
        "done": True,
        "tables": 3,
        "summarised": 3,
        "filtered": 0,
        "discarded": 0,
        "inputTokens": 360,
        "outputTokens": 120,
        "warnings": [],
    }
    lines = decode_table_summaries(world.work.blobs[table_summaries_path(plan.work_prefix)])
    assert sorted(line.page_number for line in lines) == [1, 2, 3]
    calls = world.openai.chat.completions.calls
    titles = {
        call["messages"][1]["content"].split("nearby title: ")[1].split(".")[0] for call in calls
    }
    assert titles == {"Page 1", "Page 2", "Page 3"}
    assert {(call["model"], call["reasoning_effort"]) for call in calls} == {
        ("ingest-summary", "low")
    }
    # Each call's usage is counted once, where the method makes the call.
    tokens = kit.metric_points(telemetry, "docchat.ingestion.tokens")
    assert {point.attributes["kind"]: point.value for point in tokens} == {
        "summary_input": 360,
        "summary_output": 120,
    }


async def test_a_retry_resumes_after_the_saved_tables(kit: SimpleNamespace, world: Any) -> None:
    ids = kit.Ids()
    plan = kit.make_plan(ids)
    store_parts(world, plan)
    world.sql.script(kit.fence_ok(), kit.build_facts())
    await enrich_document.run(kit.plan_payload(ids, plan), world.provider)
    world.openai.chat.completions.calls.clear()
    world.sql.script(kit.fence_ok(), kit.build_facts())
    output = await enrich_document.run(kit.plan_payload(ids, plan), world.provider)
    assert output["done"] is True
    assert output["tables"] == 0
    assert world.openai.chat.completions.calls == []


async def test_outcomes_carry_their_warnings(kit: SimpleNamespace, world: Any) -> None:
    ids = kit.Ids()
    plan = kit.make_plan(ids)
    store_parts(world, plan)
    world.openai.chat.completions.responses.extend(
        [
            openai.BadRequestError(
                "blocked", response=response(400), body={"code": "content_filter"}
            ),
            kit.completion("See https://example.net for the figures."),
        ]
    )
    world.sql.script(kit.fence_ok(), kit.build_facts())
    payload = kit.plan_payload(ids, plan)
    payload["settings"]["tableSummaryConcurrency"] = 1
    output = await enrich_document.run(payload, world.provider)
    assert (output["done"], output["filtered"], output["discarded"], output["summarised"]) == (
        True,
        1,
        1,
        1,
    )
    assert output["warnings"] == [
        {"code": "table_summary_filtered", "page": 1},
        {"code": "table_summary_discarded", "page": 2},
    ]


async def test_an_unavailable_deployment_saves_progress_and_is_retryable_with_its_wait(
    kit: SimpleNamespace, world: Any, telemetry: TestExporters
) -> None:
    ids = kit.Ids()
    plan = kit.make_plan(ids)
    store_parts(world, plan)
    world.openai.chat.completions.responses.extend(
        [
            kit.completion("A table of totals in euros."),
            openai.RateLimitError(
                "throttled", response=response(429, {"retry-after": "12"}), body=None
            ),
        ]
    )
    world.sql.script(kit.fence_ok(), kit.build_facts())
    payload = kit.plan_payload(ids, plan)
    payload["settings"]["tableSummaryConcurrency"] = 1
    output = await enrich_document.run(payload, world.provider)
    assert output["errorCode"] == "table_summary_unavailable"
    assert output["retryable"] is True
    assert output["retryAfterSeconds"] == 12
    saved = decode_table_summaries(world.work.blobs[table_summaries_path(plan.work_prefix)])
    assert [line.page_number for line in saved] == [1]
    throttled = kit.metric_points(telemetry, "docchat.ingestion.throttled")
    assert [dict(point.attributes) for point in throttled] == [
        {"service": "openai", "deployment": "ingest-summary"}
    ]


async def test_another_bad_request_reaches_the_failure_boundary(
    kit: SimpleNamespace, world: Any
) -> None:
    """A `400` other than `content_filter` is no outcome of section 12.7: retryable
    `internal_error` (section 12.9 item 3), never a silent `discarded`."""
    ids = kit.Ids()
    plan = kit.make_plan(ids)
    store_parts(world, plan)
    world.openai.chat.completions.responses.append(
        openai.BadRequestError(
            "unsupported", response=response(400), body={"code": "unsupported_parameter"}
        )
    )
    world.sql.script(kit.fence_ok(), kit.build_facts())
    payload = kit.plan_payload(ids, plan)
    payload["settings"]["tableSummaryConcurrency"] = 1
    output = await enrich_document.run(payload, world.provider)
    assert (output["errorCode"], output["retryable"]) == ("internal_error", True)


async def test_missing_parts_are_internal_error(kit: SimpleNamespace, world: Any) -> None:
    ids = kit.Ids()
    world.sql.script(kit.fence_ok(), kit.build_facts())
    output = await enrich_document.run(kit.plan_payload(ids, kit.make_plan(ids)), world.provider)
    assert (output["errorCode"], output["retryable"]) == ("internal_error", True)


async def test_a_damaged_summaries_file_is_internal_error(kit: SimpleNamespace, world: Any) -> None:
    ids = kit.Ids()
    plan = kit.make_plan(ids)
    store_parts(world, plan)
    world.work.blobs[table_summaries_path(plan.work_prefix)] = b'{"tableSha256": "x"}\n'
    world.sql.script(kit.fence_ok(), kit.build_facts())
    output = await enrich_document.run(kit.plan_payload(ids, plan), world.provider)
    assert (output["errorCode"], output["retryable"]) == ("internal_error", True)
    assert world.openai.chat.completions.calls == []
