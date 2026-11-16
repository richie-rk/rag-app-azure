"""The streaming harness (`84-testing.md` section 2.9)."""

from __future__ import annotations

import json
import random
from typing import Any

import httpx
import pytest

from docchat_testing.streaming import (
    StreamProtocolError,
    collect_stream,
    parse_events,
    split_lines,
)

META = {
    "type": "meta",
    "sessionId": "5f0c2c8e-7a51-4c3e-9d6f-1f2a3b4c5d6e",
    "turnId": "9a1e2c8e-7a51-4c3e-9d6f-1f2a3b4c5d6e",
    "sessionCreated": True,
    "replayed": False,
    "correlationId": "0af7651916cd43dd8448eb211c80319c",
}
DONE = {
    "type": "done",
    "turnId": META["turnId"],
    "truncated": False,
    "usage": {
        "promptTokens": 1,
        "completionTokens": 1,
        "reasoningTokens": 0,
        "cachedTokens": 0,
        "totalTokens": 2,
        "estimated": False,
    },
}
CITATIONS = {"type": "citations", "citations": [], "allowedLinks": []}


def ndjson(*events: dict[str, Any]) -> bytes:
    return b"".join(
        json.dumps(event, ensure_ascii=False, separators=(",", ":")).encode() + b"\n"
        for event in events
    )


STREAM = ndjson(
    META,
    {"type": "heartbeat"},
    {"type": "delta", "text": "caf\u00e9 \U0001f600 \u27e61\u27e7"},
    CITATIONS,
    {"type": "followups", "questions": ["Next?"]},
    DONE,
)


def chunks_of(data: bytes, sizes: list[int]) -> list[bytes]:
    pieces, start = [], 0
    for size in sizes:
        pieces.append(data[start : start + size])
        start += size
    pieces.append(data[start:])
    return [piece for piece in pieces if piece]


@pytest.mark.parametrize("size", [1, 7])
def test_any_chunking_rebuilds_every_event(size: int) -> None:
    events = parse_events(split_lines(chunks_of(STREAM, [size] * (len(STREAM) // size))))

    assert [event.type for event in events] == [
        "meta",
        "heartbeat",
        "delta",
        "citations",
        "followups",
        "done",
    ]


def test_random_chunking_rebuilds_multi_byte_characters() -> None:
    generator = random.Random(70)  # noqa: S311 - repeatable test data
    sizes = [generator.randint(1, 9) for _ in range(len(STREAM))]

    events = parse_events(split_lines(chunks_of(STREAM, sizes)))

    assert events[2].model_dump(by_alias=True)["text"] == "caf\u00e9 \U0001f600 \u27e61\u27e7"


@pytest.mark.parametrize(
    ("data", "problem"),
    [
        (ndjson(META, CITATIONS), "no terminal event"),
        (ndjson(META, DONE), "done after OPENED"),
        (ndjson(META, CITATIONS, DONE, DONE), "follows the terminal event"),
        (ndjson(META)[:-1], "inside a line"),
        (ndjson(META) + b"\n" + ndjson(CITATIONS, DONE), "is empty"),
    ],
)
def test_protocol_breaches_are_refused(data: bytes, problem: str) -> None:
    with pytest.raises(StreamProtocolError, match=problem):
        parse_events(split_lines([data]))


async def test_collect_stream_reads_a_response() -> None:
    response = httpx.Response(200, content=STREAM)

    events = await collect_stream(response)

    assert events[-1].type == "done"
