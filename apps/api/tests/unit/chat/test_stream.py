"""The NDJSON encoder, the event-order state machine and the stream writer (T-263, T-265, T-266;
`12-chat-pipeline.md` sections 14.1 to 14.5)."""

from __future__ import annotations

import asyncio
import json
import random
from uuid import uuid4

import pytest
from pydantic import TypeAdapter

from docchat_api.chat.models import Citation, TurnUsageSummary
from docchat_api.chat.stream import (
    HEARTBEAT_SECONDS,
    STREAM_ERROR_CODES,
    AnyEvent,
    ChatStreamEvent,
    EventOrder,
    StreamCitationsEvent,
    StreamDeltaEvent,
    StreamDoneEvent,
    StreamErrorEvent,
    StreamFollowupsEvent,
    StreamMetaEvent,
    StreamOrderError,
    StreamStatusEvent,
    StreamWriter,
    encode,
    receive_within,
)
from docchat_core.cosmos.conversations import CitationContentType
from docchat_core.errors import CODES
from docchat_testing.clock import FakeClock

CORRELATION = "0af7651916cd43dd8448eb211c80319c"
ADAPTER: TypeAdapter[AnyEvent] = TypeAdapter(ChatStreamEvent)
USAGE = TurnUsageSummary(
    prompt_tokens=1,
    completion_tokens=2,
    reasoning_tokens=0,
    cached_tokens=0,
    total_tokens=3,
    estimated=False,
)


def meta() -> StreamMetaEvent:
    return StreamMetaEvent(
        session_id=uuid4(),
        turn_id=uuid4(),
        session_created=True,
        replayed=False,
        correlation_id=CORRELATION,
    )


def citations() -> StreamCitationsEvent:
    citation = Citation(
        number=1,
        title="Guide",
        page_number=3,
        heading_path=["Claims"],
        content_type=CitationContentType.PDF,
        open_as="pdf_page",
    )
    return StreamCitationsEvent(citations=[citation], allowed_links=["https://example.org/x"])


def done() -> StreamDoneEvent:
    return StreamDoneEvent(turn_id=uuid4(), truncated=False, usage=USAGE)


def error() -> StreamErrorEvent:
    return StreamErrorEvent.of("model_unavailable", CORRELATION)


# --- T-263: the encoder ---------------------------------------------------------------------------

TRICKY = [
    "line one\nline two",
    "carriage\rreturn\r\n",
    "separators \u2028 and \u2029",
    "emoji \U0001f600 and a 4-byte \U00010348 character",
    'quotes " and backslash \\',
    "\x00\x1f control",
]


def _random_texts(count: int) -> list[str]:
    generator = random.Random(1405)  # noqa: S311 - repeatable test data
    alphabet = ["a", " ", "\n", "\r", "\u2028", "\u2029", "\U0001f600", "\U00010348", "\u27e6"]
    return ["".join(generator.choices(alphabet, k=40)) for _ in range(count)]


@pytest.mark.parametrize("text", [*TRICKY, *_random_texts(50)])
def test_t263_one_compact_object_and_one_newline_per_event(text: str) -> None:
    line = encode(StreamDeltaEvent(text=text))

    assert line.endswith(b"\n")
    assert line.count(b"\n") == 1
    assert b"\n" not in line[:-1]
    decoded = line.decode("utf-8")
    assert json.loads(decoded) == {"type": "delta", "text": text}
    assert ", " not in decoded.split('"text"')[0]
    if "\U0001f600" in text:
        assert "\U0001f600" in decoded, "non-ASCII characters stay unescaped"


def test_every_field_is_present_and_nullable_ones_are_null() -> None:
    assert json.loads(encode(error())) == {
        "type": "error",
        "code": "model_unavailable",
        "message": CODES["model_unavailable"].detail,
        "retryable": True,
        "retryAfterSeconds": None,
        "correlationId": CORRELATION,
    }
    assert json.loads(encode(StreamStatusEvent())) == {
        "type": "status",
        "status": "search_broadened",
    }


def test_the_error_event_takes_the_catalogue_text_and_fixed_wait() -> None:
    restarting = StreamErrorEvent.of("service_restarting", CORRELATION)
    busy = StreamErrorEvent.of("model_busy", CORRELATION, retry_after_seconds=7)

    assert restarting.retry_after_seconds == 5
    assert restarting.message == CODES["service_restarting"].detail
    assert busy.retry_after_seconds == 7
    assert StreamErrorEvent.of("prompt_too_large", CORRELATION).retryable is False
    assert {code for code in STREAM_ERROR_CODES if CODES[code].status is not None} == {
        "internal_error"
    }
    assert len(STREAM_ERROR_CODES) == 14


@pytest.mark.parametrize(
    "event",
    [
        meta(),
        StreamStatusEvent(),
        StreamDeltaEvent(text="a \u27e61\u27e7"),
        citations(),
        StreamFollowupsEvent(questions=["Next?"]),
        error(),
        done(),
    ],
    ids=lambda event: event.type,
)
def test_t265_every_event_validates_against_the_union(event: AnyEvent) -> None:
    assert ADAPTER.validate_json(encode(event)) == event


# --- T-265: the state machine ---------------------------------------------------------------------


def _accept_all(types: list[str]) -> EventOrder:
    order = EventOrder()
    for event_type in types:
        order.accept(event_type)
    return order


@pytest.mark.parametrize(
    "types",
    [
        ["meta", "status", "delta", "delta", "citations", "followups", "done"],
        ["meta", "delta", "citations", "done"],
        ["meta", "citations", "done"],
        ["meta", "status", "citations", "followups", "done"],
        ["meta", "heartbeat", "delta", "heartbeat", "citations", "heartbeat", "done"],
    ],
)
def test_t265_allowed_orders(types: list[str]) -> None:
    assert _accept_all(types).ended


@pytest.mark.parametrize(
    "prefix",
    [
        ["meta"],
        ["meta", "status"],
        ["meta", "delta"],
        ["meta", "citations"],
        ["meta", "citations", "followups"],
    ],
)
def test_t265_error_is_accepted_at_every_point(prefix: list[str]) -> None:
    assert _accept_all([*prefix, "error"]).ended


@pytest.mark.parametrize(
    ("prefix", "event_type"),
    [
        (["meta", "citations", "done"], "delta"),
        (["meta", "error"], "delta"),
        (["meta", "error"], "heartbeat"),
        (["meta", "delta"], "status"),
        ([], "delta"),
        ([], "heartbeat"),
        ([], "error"),
        (["meta"], "meta"),
        (["meta"], "done"),
        (["meta", "status"], "status"),
        (["meta", "citations"], "citations"),
        (["meta", "citations", "followups"], "followups"),
    ],
)
def test_t265_refused_orders(prefix: list[str], event_type: str) -> None:
    order = _accept_all(prefix)
    state = order.state

    with pytest.raises(StreamOrderError):
        order.accept(event_type)
    assert order.state is state


# --- T-266 and the writer -------------------------------------------------------------------------


class Receiver:
    """A receive port over a fake clock: an empty queue waits the whole timeout at once; a
    queued event arrives after `arrival` seconds."""

    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.waits: list[float] = []
        self.arrival = 0.0

    async def __call__(self, queue: asyncio.Queue[AnyEvent], seconds: float) -> AnyEvent | None:
        self.waits.append(seconds)
        await asyncio.sleep(0)
        if queue.empty():
            self.clock.advance(seconds)
            return None
        self.clock.advance(self.arrival)
        return queue.get_nowait()


class Recorder:
    def __init__(self) -> None:
        self.written: list[AnyEvent] = []
        self.disconnects = 0

    def write(self, event: AnyEvent) -> None:
        self.written.append(event)

    def disconnected(self) -> None:
        self.disconnects += 1


def writer(
    queue: asyncio.Queue[AnyEvent], clock: FakeClock, recorder: Recorder, receiver: Receiver
) -> StreamWriter:
    return StreamWriter(
        queue,
        correlation_id=CORRELATION,
        clock=clock,
        written=recorder.write,
        disconnected=recorder.disconnected,
        receive=receiver,
    )


async def test_t266_a_heartbeat_after_15_s_and_the_count_restarts_after_each_line() -> None:
    clock = FakeClock()
    queue: asyncio.Queue[AnyEvent] = asyncio.Queue()
    recorder, receiver = Recorder(), Receiver(clock)
    lines = writer(queue, clock, recorder, receiver).lines()
    queue.put_nowait(meta())

    assert json.loads(await anext(lines))["type"] == "meta"
    assert json.loads(await anext(lines)) == {"type": "heartbeat"}
    assert receiver.waits == [HEARTBEAT_SECONDS, HEARTBEAT_SECONDS]
    receiver.arrival = 4.0
    queue.put_nowait(StreamDeltaEvent(text="x"))
    assert json.loads(await anext(lines))["type"] == "delta"
    receiver.arrival = 0.0
    assert json.loads(await anext(lines)) == {"type": "heartbeat"}
    assert receiver.waits[-2:] == [HEARTBEAT_SECONDS, HEARTBEAT_SECONDS]
    queue.put_nowait(citations())
    queue.put_nowait(done())
    rest = [json.loads(line)["type"] async for line in lines]

    assert rest == ["citations", "done"]
    assert [event.type for event in recorder.written] == ["meta", "delta", "citations", "done"]
    assert recorder.disconnects == 0


async def test_no_heartbeat_before_meta() -> None:
    clock = FakeClock()
    queue: asyncio.Queue[AnyEvent] = asyncio.Queue()
    recorder, receiver = Recorder(), Receiver(clock)
    lines = writer(queue, clock, recorder, receiver).lines()

    async def put_later() -> None:
        for _ in range(5):
            await asyncio.sleep(0)
        queue.put_nowait(meta())

    task = asyncio.create_task(put_later())
    first = json.loads(await anext(lines))
    await task

    assert first["type"] == "meta"
    assert len(receiver.waits) > 1, "the writer waited through silence before meta"
    await lines.aclose()


async def test_an_out_of_order_event_becomes_internal_error(captured: object) -> None:
    clock = FakeClock()
    queue: asyncio.Queue[AnyEvent] = asyncio.Queue()
    recorder, receiver = Recorder(), Receiver(clock)
    for event in (meta(), StreamDeltaEvent(text="a"), StreamStatusEvent(), done()):
        queue.put_nowait(event)

    lines = [json.loads(line) async for line in writer(queue, clock, recorder, receiver).lines()]

    assert [line["type"] for line in lines] == ["meta", "delta", "error"]
    assert lines[-1]["code"] == "internal_error"
    assert recorder.disconnects == 0


async def test_events_before_meta_are_never_written(captured: object) -> None:
    clock = FakeClock()
    queue: asyncio.Queue[AnyEvent] = asyncio.Queue()
    recorder, receiver = Recorder(), Receiver(clock)
    for event in (StreamDeltaEvent(text="a"), meta(), error()):
        queue.put_nowait(event)

    lines = [json.loads(line) async for line in writer(queue, clock, recorder, receiver).lines()]

    assert [line["type"] for line in lines] == ["meta", "error"]


async def test_closing_without_a_terminal_event_tells_the_turn() -> None:
    clock = FakeClock()
    queue: asyncio.Queue[AnyEvent] = asyncio.Queue()
    recorder, receiver = Recorder(), Receiver(clock)
    queue.put_nowait(meta())
    lines = writer(queue, clock, recorder, receiver).lines()
    await anext(lines)

    await lines.aclose()

    assert recorder.disconnects == 1


async def test_receive_within_returns_none_after_the_wait_and_loses_nothing() -> None:
    queue: asyncio.Queue[AnyEvent] = asyncio.Queue()

    assert await receive_within(queue, 0.01) is None
    assert await receive_within(queue, 0) is None
    queue.put_nowait(meta())
    received = await receive_within(queue, 0)

    assert received is not None
    assert received.type == "meta"
    assert queue.empty()
