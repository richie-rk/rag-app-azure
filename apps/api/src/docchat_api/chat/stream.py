"""The NDJSON stream of `createChatTurn`: the event models, the encoder, the event-order state
machine and the stream writer (`12-chat-pipeline.md` sections 1.2 and 14; `11-api.md` sections
3.3.2 and 3.3.3; ADR 0055).

`ChatStreamEvent` is the union of the eight event models, discriminated by `type`; every field is
always present and a nullable one is sent as `null`. Each event is one compact JSON object from
`model_dump_json(by_alias=True)` (UTF-8, non-ASCII unescaped) followed by exactly one `\\n`: JSON
string escaping keeps every raw newline out of an event, so a client that splits on byte `0x0A`
rebuilds every event whatever the network chunking.

The turn task never writes to the connection: it puts events on an unbounded queue, and the
writer, the response's body iterator, takes them off, checks each against the order of section
14.3 and writes it. After 15 seconds without a line it writes a heartbeat, the count restarting
after every line. An event out of order is a programming error: it is logged and, while no
terminal event has been written, replaced by `error` `internal_error`, which ends the stream. When
the iterator ends without a terminal event (the client went away, and Starlette cancelled it), its
`finally` block tells the turn.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Awaitable, Callable
from enum import Enum, auto
from typing import Annotated, Final, Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from pydantic.alias_generators import to_camel

from docchat_api.chat import telemetry
from docchat_api.chat.models import Citation, TurnUsageSummary
from docchat_core.cosmos.conversations.turn import ALLOWED_LINKS_MAX
from docchat_core.errors import CODES, DETAIL_MAX_LENGTH
from docchat_core.models import CanonicalUuid, CorrelationId
from docchat_core.models.clock import Clock

HEARTBEAT_SECONDS: Final = 15.0
"""A protocol constant: well within the browser's 45-second stall timer and the front end's idle
limit."""
DELTA_MAX_CODE_POINTS: Final = 4_000
FOLLOWUP_MAX: Final = 200
FOLLOWUPS_MAX: Final = 3
ALLOWED_LINK_MAX_LENGTH: Final = 2_048
STREAM_RETRY_AFTER_MAX: Final = 60
NDJSON_MEDIA_TYPE: Final = "application/x-ndjson"

StreamErrorCode = Literal[
    "conversation_unavailable",
    "retrieval_unavailable",
    "source_screening_unavailable",
    "prompt_too_large",
    "question_filtered",
    "answer_filtered",
    "model_busy",
    "model_unavailable",
    "model_request_rejected",
    "answer_incomplete",
    "turn_timeout",
    "turn_save_failed",
    "service_restarting",
    "internal_error",
]
"""The 14 codes an `error` event can carry (`11-api.md` section 2.3)."""

STREAM_ERROR_CODES: Final[frozenset[str]] = frozenset(get_args(StreamErrorCode))


class StreamEvent(BaseModel):
    """The base of the event models: camelCase names, every field present (the schema marks the
    `type` constants required too, because every event carries them)."""

    model_config = ConfigDict(
        json_schema_serialization_defaults_required=True,
        alias_generator=to_camel,
        extra="forbid",
        frozen=True,
        validate_by_alias=True,
        validate_by_name=True,
        serialize_by_alias=True,
    )


class StreamMetaEvent(StreamEvent):
    type: Literal["meta"] = "meta"
    session_id: CanonicalUuid
    turn_id: CanonicalUuid
    session_created: bool
    replayed: bool
    correlation_id: CorrelationId


class StreamStatusEvent(StreamEvent):
    type: Literal["status"] = "status"
    status: Literal["search_broadened"] = "search_broadened"


class StreamDeltaEvent(StreamEvent):
    type: Literal["delta"] = "delta"
    text: Annotated[str, Field(min_length=1, max_length=DELTA_MAX_CODE_POINTS)]


HttpsLink = Annotated[
    str, StringConstraints(min_length=9, max_length=ALLOWED_LINK_MAX_LENGTH, pattern=r"^https://")
]


class StreamCitationsEvent(StreamEvent):
    type: Literal["citations"] = "citations"
    citations: Annotated[list[Citation], Field(max_length=16)]
    allowed_links: Annotated[list[HttpsLink], Field(max_length=ALLOWED_LINKS_MAX)]


class StreamFollowupsEvent(StreamEvent):
    type: Literal["followups"] = "followups"
    questions: Annotated[
        list[Annotated[str, Field(min_length=1, max_length=FOLLOWUP_MAX)]],
        Field(min_length=1, max_length=FOLLOWUPS_MAX),
    ]


class StreamHeartbeatEvent(StreamEvent):
    type: Literal["heartbeat"] = "heartbeat"


class StreamErrorEvent(StreamEvent):
    type: Literal["error"] = "error"
    code: StreamErrorCode
    message: Annotated[str, Field(min_length=1, max_length=DETAIL_MAX_LENGTH)]
    retryable: bool
    retry_after_seconds: Annotated[int, Field(ge=1, le=STREAM_RETRY_AFTER_MAX)] | None
    correlation_id: CorrelationId

    @classmethod
    def of(
        cls, code: StreamErrorCode, correlation_id: str, retry_after_seconds: int | None = None
    ) -> StreamErrorEvent:
        """The event of `code`: its catalogue text and retryable flag, never exception text. A
        code whose catalogue entry fixes the wait (`service_restarting`) carries it."""
        entry = CODES[code]
        if retry_after_seconds is None and entry.retry_after is not None:
            low, high = entry.retry_after
            retry_after_seconds = low if low == high else None
        return cls(
            code=code,
            message=entry.detail,
            retryable=entry.retryable,
            retry_after_seconds=retry_after_seconds,
            correlation_id=correlation_id,
        )


class StreamDoneEvent(StreamEvent):
    type: Literal["done"] = "done"
    turn_id: CanonicalUuid
    truncated: bool
    usage: TurnUsageSummary


ChatStreamEvent = Annotated[
    StreamMetaEvent
    | StreamStatusEvent
    | StreamDeltaEvent
    | StreamCitationsEvent
    | StreamFollowupsEvent
    | StreamHeartbeatEvent
    | StreamErrorEvent
    | StreamDoneEvent,
    Field(discriminator="type"),
]
"""The stream's event union, published in the OpenAPI document under this name."""

type AnyEvent = (
    StreamMetaEvent
    | StreamStatusEvent
    | StreamDeltaEvent
    | StreamCitationsEvent
    | StreamFollowupsEvent
    | StreamHeartbeatEvent
    | StreamErrorEvent
    | StreamDoneEvent
)

TERMINAL_TYPES: Final = frozenset({"done", "error"})


def encode(event: AnyEvent) -> bytes:
    """One event as one line: compact UTF-8 JSON and exactly one `\\n`."""
    return event.model_dump_json(by_alias=True).encode("utf-8") + b"\n"


# --- The event-order state machine ------------------------------------------------------------


class StreamState(Enum):
    START = auto()
    OPENED = auto()
    BROADENED = auto()
    ANSWERING = auto()
    CITED = auto()
    SUGGESTED = auto()
    ENDED = auto()


_TRANSITIONS: Final[dict[tuple[StreamState, str], StreamState]] = {
    (StreamState.START, "meta"): StreamState.OPENED,
    (StreamState.OPENED, "status"): StreamState.BROADENED,
    (StreamState.OPENED, "delta"): StreamState.ANSWERING,
    (StreamState.BROADENED, "delta"): StreamState.ANSWERING,
    (StreamState.ANSWERING, "delta"): StreamState.ANSWERING,
    (StreamState.OPENED, "citations"): StreamState.CITED,
    (StreamState.BROADENED, "citations"): StreamState.CITED,
    (StreamState.ANSWERING, "citations"): StreamState.CITED,
    (StreamState.CITED, "followups"): StreamState.SUGGESTED,
    (StreamState.CITED, "done"): StreamState.ENDED,
    (StreamState.SUGGESTED, "done"): StreamState.ENDED,
    **{
        (state, "error"): StreamState.ENDED
        for state in (
            StreamState.OPENED,
            StreamState.BROADENED,
            StreamState.ANSWERING,
            StreamState.CITED,
            StreamState.SUGGESTED,
        )
    },
}
_OPEN_STATES: Final = frozenset(
    {
        StreamState.OPENED,
        StreamState.BROADENED,
        StreamState.ANSWERING,
        StreamState.CITED,
        StreamState.SUGGESTED,
    }
)


class StreamOrderError(Exception):
    """An event written out of the order of section 14.3 (a programming error)."""


class EventOrder:
    """The state machine of section 14.3: `meta` first; at most one `status`, before any
    `delta`; `delta`s; one `citations`; at most one `followups`; `done`. `error` may follow `meta`
    anywhere; a heartbeat is allowed between `meta` and the terminal event and changes nothing."""

    def __init__(self) -> None:
        self.state = StreamState.START

    @property
    def opened(self) -> bool:
        return self.state in _OPEN_STATES

    @property
    def ended(self) -> bool:
        return self.state is StreamState.ENDED

    def accept(self, event_type: str) -> None:
        """Move on with `event_type`, or raise `StreamOrderError` and stay."""
        if event_type == "heartbeat":
            if not self.opened:
                raise StreamOrderError("a heartbeat outside the stream's open states")
            return
        following = _TRANSITIONS.get((self.state, event_type))
        if following is None:
            raise StreamOrderError(f"{event_type} after {self.state.name}")
        self.state = following


# --- The writer ---------------------------------------------------------------------------------

type Receive = Callable[[asyncio.Queue[AnyEvent], float], Awaitable[AnyEvent | None]]
"""Takes the next event off the queue within `timeout` seconds, or `None` when none came."""


async def receive_within(queue: asyncio.Queue[AnyEvent], seconds: float) -> AnyEvent | None:
    """The next event, or `None` after `seconds`; a cancelled `get` loses no item."""
    if not queue.empty():
        return queue.get_nowait()
    try:
        return await asyncio.wait_for(queue.get(), seconds)
    except TimeoutError:
        return None


class StreamWriter:
    """The body iterator of one stream.

    `written` is told of every event written, so the turn can time its first `delta` and know
    when `citations` went out; `disconnected` is called when the iterator ends without a
    terminal event.
    """

    def __init__(
        self,
        queue: asyncio.Queue[AnyEvent],
        *,
        correlation_id: str,
        clock: Clock,
        written: Callable[[AnyEvent], None],
        disconnected: Callable[[], None],
        receive: Receive = receive_within,
    ) -> None:
        self._queue = queue
        self._correlation_id = correlation_id
        self._clock = clock
        self._written = written
        self._disconnected = disconnected
        self._receive = receive
        self.order = EventOrder()

    async def lines(self) -> AsyncGenerator[bytes]:
        """Every line of the stream, the heartbeats included, until the terminal event."""
        last_write = self._clock.monotonic()
        try:
            while not self.order.ended:
                wait = max(HEARTBEAT_SECONDS - (self._clock.monotonic() - last_write), 0.0)
                event = await self._receive(self._queue, wait)
                if event is None:
                    if self.order.opened:
                        self.order.accept("heartbeat")
                        telemetry.record_heartbeat()
                        yield encode(StreamHeartbeatEvent())
                        last_write = self._clock.monotonic()
                    continue
                try:
                    self.order.accept(event.type)
                except StreamOrderError as failure:
                    telemetry.report_unexpected(failure)
                    if not self.order.opened:
                        # Nothing may be written before `meta`, and nothing after the end.
                        continue
                    event = StreamErrorEvent.of("internal_error", self._correlation_id)
                    self.order.accept(event.type)
                line = encode(event)
                yield line
                last_write = self._clock.monotonic()
                self._written(event)
        finally:
            if not self.order.ended:
                self._disconnected()
