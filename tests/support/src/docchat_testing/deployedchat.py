"""Chat turns of the deployed suites (`84-testing.md` sections 6.4 and 6.5; `12-chat-pipeline.md`
sections 2, 3.2 and 14).

`send_chat_turn` sends one `createChatTurn` through a `DeployedApiClient` and reads its stream to
the end: the events (framing and order checked by `docchat_testing.streaming`), the response
headers, and how long after sending the first complete line arrived (the `meta` line, which the
deployed chat test needs within 3 s). A refusal before `200` comes back with its problem body.

The per-user request rate of chat turns is low (`ratelimit.chat_requests_per_minute`, 3 by
default) and every turn of a suite run comes from the same few users, so a `429 rate_limited`
waits its `retryAfterSeconds` and sends the question again with a new `clientTurnId` (section
2.1: a retry sends a new one), for at most `rate_wait` seconds in total. The client itself leaves
`rate_limited` to the caller (section 6.4 item 7); waiting here is the caller's choice for tests
whose subject isn't the limit.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any, Final

import httpx

from docchat_api.chat.models import Citation
from docchat_api.chat.stream import (
    AnyEvent,
    StreamCitationsEvent,
    StreamDeltaEvent,
    StreamDoneEvent,
    StreamErrorEvent,
    StreamMetaEvent,
)
from docchat_testing.apiclient import DeployedApiClient
from docchat_testing.streaming import parse_events, split_lines

CHAT_PATH: Final = "/v1/chat/turns"
RATE_LIMITED: Final = "rate_limited"
RATE_WAIT_SECONDS: Final = 180.0
"""Longer than two windows of the default rate (`retry_after` never exceeds 120 s)."""
RATE_RETRY_MARGIN_SECONDS: Final = 1.0
HTTP_OK: Final = 200
HTTP_TOO_MANY_REQUESTS: Final = 429
NEWLINE: Final = b"\n"

type Sleep = Callable[[float], Awaitable[None]]


class ChatTurnError(AssertionError):
    """A chat turn didn't end the way the test needs; the message names how it ended."""


@dataclass(frozen=True, slots=True)
class ChatTurn:
    """One `createChatTurn`: its status, headers, events or problem, and timing."""

    status_code: int
    headers: httpx.Headers
    events: list[AnyEvent] = field(default_factory=list)
    problem: dict[str, Any] | None = None
    first_line_seconds: float | None = None
    """Seconds from sending the request to the first complete line; `None` without a stream."""
    rate_waited_seconds: float = 0.0
    """Seconds `send_chat_turn` waited out `429 rate_limited` answers before this one."""

    @property
    def problem_code(self) -> str | None:
        code = (self.problem or {}).get("code")
        return code if isinstance(code, str) else None

    @property
    def meta(self) -> StreamMetaEvent:
        first = self.events[0] if self.events else None
        if not isinstance(first, StreamMetaEvent):
            raise ChatTurnError(f"no meta line: {self.describe()}")
        return first

    @property
    def done(self) -> StreamDoneEvent | None:
        last = self.events[-1] if self.events else None
        return last if isinstance(last, StreamDoneEvent) else None

    @property
    def error(self) -> StreamErrorEvent | None:
        last = self.events[-1] if self.events else None
        return last if isinstance(last, StreamErrorEvent) else None

    @property
    def citations(self) -> list[Citation]:
        for event in self.events:
            if isinstance(event, StreamCitationsEvent):
                return list(event.citations)
        return []

    @property
    def answer(self) -> str:
        return "".join(event.text for event in self.events if isinstance(event, StreamDeltaEvent))

    def describe(self) -> str:
        """How the turn ended, for an assertion message: never the answer or a question."""
        if self.status_code != HTTP_OK:
            return f"{self.status_code} {self.problem_code or ''}".rstrip()
        if self.error is not None:
            return f"error {self.error.code}"
        if self.done is not None:
            return "done"
        return "no terminal event"

    def require_done(self) -> StreamDoneEvent:
        """The `done` event; anything else fails the test with how the turn ended."""
        if self.done is None:
            raise ChatTurnError(f"the chat turn ended with {self.describe()}")
        return self.done


def turn_body(project_id: str, question: str, session_id: str | None) -> dict[str, Any]:
    return {
        "projectId": project_id,
        "sessionId": session_id,
        "clientTurnId": str(uuid.uuid4()),
        "question": question,
    }


async def read_turn(
    client: DeployedApiClient,
    body: Mapping[str, Any],
    *,
    headers: Mapping[str, str] | None = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> ChatTurn:
    """One `createChatTurn` read to its end, with the first line's arrival time."""
    started = monotonic()
    async with client.stream("POST", CHAT_PATH, json=dict(body), headers=headers) as response:
        if response.status_code != HTTP_OK:
            await response.aread()
            try:
                problem = response.json()
            except ValueError:
                problem = None
            return ChatTurn(
                status_code=response.status_code,
                headers=response.headers,
                problem=problem if isinstance(problem, dict) else None,
            )
        chunks: list[bytes] = []
        first_line: float | None = None
        async for chunk in response.aiter_bytes():
            chunks.append(chunk)
            if first_line is None and NEWLINE in chunk:
                first_line = monotonic() - started
    return ChatTurn(
        status_code=response.status_code,
        headers=response.headers,
        events=parse_events(split_lines(chunks)),
        first_line_seconds=first_line,
    )


async def send_chat_turn(
    client: DeployedApiClient,
    *,
    project_id: str,
    question: str,
    session_id: str | None = None,
    headers: Mapping[str, str] | None = None,
    rate_wait: float = RATE_WAIT_SECONDS,
    sleep: Sleep = asyncio.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> ChatTurn:
    """Send `question` to the project and read the turn; a `429 rate_limited` waits its
    `retryAfterSeconds` and sends again, for at most `rate_wait` seconds of waiting."""
    waited = 0.0
    while True:
        turn = await read_turn(
            client, turn_body(project_id, question, session_id), headers=headers,
            monotonic=monotonic,
        )  # fmt: skip
        turn = replace(turn, rate_waited_seconds=waited)
        if turn.status_code != HTTP_TOO_MANY_REQUESTS or turn.problem_code != RATE_LIMITED:
            return turn
        seconds = (turn.problem or {}).get("retryAfterSeconds")
        if isinstance(seconds, bool) or not isinstance(seconds, int) or seconds < 1:
            return turn
        wait = seconds + RATE_RETRY_MARGIN_SECONDS
        if waited + wait > rate_wait:
            return turn
        await sleep(wait)
        waited += wait
