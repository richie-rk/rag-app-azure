"""The streaming harness: `collect_stream(response)` (`84-testing.md` section 2.9;
`12-chat-pipeline.md` section 14).

It reads an `httpx` streaming response as bytes, splits on byte `0x0A` before decoding (so a
multi-byte character split across network chunks is never decoded in halves), validates every
line against `ChatStreamEvent`, and checks the order of section 14.3 with the stream writer's own
state machine: `meta` first, at most one `status` before any `delta`, the `delta`s, one
`citations`, at most one `followups`, then exactly one terminal event (`done` or `error`), with
`heartbeat` allowed anywhere between `meta` and the end, and nothing after the terminal event.
"""

from __future__ import annotations

from collections.abc import AsyncIterable, Iterable
from typing import Final

import httpx
from pydantic import TypeAdapter

from docchat_api.chat.stream import AnyEvent, ChatStreamEvent, EventOrder, StreamOrderError

NEWLINE: Final = b"\n"
_ADAPTER: Final[TypeAdapter[AnyEvent]] = TypeAdapter(ChatStreamEvent)


class StreamProtocolError(AssertionError):
    """A stream that breaks the protocol: an invalid line, an order the state machine refuses, or
    no terminal event."""


def split_lines(chunks: Iterable[bytes]) -> list[bytes]:
    """The lines of a byte stream, split on `0x0A` before any decoding; a trailing partial line
    is kept for the caller to refuse."""
    buffer = b"".join(chunks)
    lines = buffer.split(NEWLINE)
    if lines and lines[-1] == b"":
        lines.pop()
    elif lines:
        raise StreamProtocolError("the stream ends inside a line")
    return lines


def parse_events(lines: Iterable[bytes]) -> list[AnyEvent]:
    """Every line validated as one event, in order, with the order checked."""
    order = EventOrder()
    events: list[AnyEvent] = []
    for number, line in enumerate(lines, start=1):
        if not line:
            raise StreamProtocolError(f"line {number} is empty")
        if order.ended:
            raise StreamProtocolError(f"line {number} follows the terminal event")
        event = _ADAPTER.validate_json(line)
        try:
            order.accept(event.type)
        except StreamOrderError as failure:
            raise StreamProtocolError(f"line {number}: {failure}") from failure
        events.append(event)
    if not order.ended:
        raise StreamProtocolError("the stream has no terminal event")
    return events


async def read_bytes(chunks: AsyncIterable[bytes]) -> list[bytes]:
    return [chunk async for chunk in chunks]


async def collect_stream(response: httpx.Response) -> list[AnyEvent]:
    """Every event of a `createChatTurn` stream, its framing and order checked."""
    return parse_events(split_lines(await read_bytes(response.aiter_bytes())))
