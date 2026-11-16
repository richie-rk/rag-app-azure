"""Prompt Shields: source screening and `shield_user_prompt`, the only calls the API makes to the
Content Safety service (`12-chat-pipeline.md` section 8; ADR 0062; `82-security.md` SEC-36).

Retrieved text is written by other people, so every kept source is screened before prompt
assembly, a flagged source is dropped on its own, and screening fails closed. The text screened
for a source is exactly the defanged body of its `SOURCE` block. Lengths are UTF-16 code units,
never fewer than code points, so the service's 10,000-character limit holds however it counts:

- a body of at most 10,000 units is one segment; a longer one is cut into segments of at most
  10,000 units, each ending just after the last whitespace character at or beyond unit 5,000 of
  its window (at the window's end when that half holds none, never inside a surrogate pair), each
  later segment starting 200 units before the previous one ended, so an instruction of up to 200
  units that straddles a cut appears whole in one segment;
- segments are packed first-fit decreasing (longest first, ties by source rank, then segment
  order) into calls of fewer than 6 documents and at most 10,000 units, at most 4 calls at once;
- each call is `POST /contentsafety/text:shieldPrompt?api-version=2024-09-01` with
  `{"userPrompt": "", "documents": [...]}`, and `documentsAnalysis[i]` belongs to its `i`-th
  segment. A source is flagged when any of its segments is.

A timeout, a connection failure, `429` or `5xx` is retried once, after the response's `Retry-After`
when it is at most 2 s, otherwise after 0.5 s; a second failure, any other non-`2xx` status, or a
body without one boolean `attackDetected` per document raises `ScreeningUnavailable`, and the turn's
other calls are cancelled. No setting bypasses screening.

`shield_user_prompt(text)` screens one memory fact for the conversation store on the same client,
endpoint, token, timeout and retry rule, with `{"userPrompt": text, "documents": []}`.
"""

from __future__ import annotations

import asyncio
from bisect import bisect_right
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from http import HTTPStatus
from typing import Any, ClassVar, Final, Literal, Self

import httpx

from docchat_api.chat import telemetry
from docchat_core.clients import CONTENTSAFETY_CONNECT_TIMEOUT_SECONDS

type Sleep = Callable[[float], Awaitable[None]]
type FailureReason = Literal["timeout", "throttled", "error"]

SHIELD_PATH: Final = "/contentsafety/text:shieldPrompt"
API_VERSION: Final = "2024-09-01"
SOURCE_USER_PROMPT: Final = ""
"""The `userPrompt` of source screening: empty (SP-10; ADR 0062 records the probe's outcome)."""
SEGMENT_MAX_UNITS: Final = 10_000
SEGMENT_FLOOR_UNITS: Final = 5_000
SEGMENT_OVERLAP_UNITS: Final = 200
CALL_MAX_DOCUMENTS: Final = 5
CALL_MAX_UNITS: Final = 10_000
CALLS_AT_ONCE: Final = 4
BMP_MAX: Final = 0xFFFF
TEXT_MAX_UNITS: Final = 10_000
RETRY_WAIT_SECONDS: Final = 0.5
RETRY_AFTER_MAX_SECONDS: Final = 2.0
ATTEMPTS: Final = 2


class ScreeningUnavailable(Exception):  # noqa: N818 - the spec's name
    """Prompt Shields couldn't screen: two failures, or a status or body that can't be used."""

    def __init__(self, reason: FailureReason) -> None:
        super().__init__(reason)
        self.reason: FailureReason = reason


def utf16_units(text: str) -> int:
    """The UTF-16 code units of `text`."""
    return len(text.encode("utf-16-le")) // 2


def _offsets(text: str) -> list[int]:
    """The unit offset of each code point, and the total at the end."""
    offsets = [0]
    for character in text:
        offsets.append(offsets[-1] + (2 if ord(character) > BMP_MAX else 1))
    return offsets


def segments(body: str) -> list[str]:
    """`body` cut into segments of at most 10,000 UTF-16 units with the 200-unit overlap."""
    offsets = _offsets(body)
    if offsets[-1] <= SEGMENT_MAX_UNITS:
        return [body]
    cut_segments: list[str] = []
    start = 0
    while True:
        start_unit = offsets[start]
        if offsets[-1] - start_unit <= SEGMENT_MAX_UNITS:
            cut_segments.append(body[start:])
            return cut_segments
        # The last code point boundary inside the window; inside a surrogate pair this moves
        # back one unit, since code points are never split.
        end = bisect_right(offsets, start_unit + SEGMENT_MAX_UNITS) - 1
        cut = end
        floor = start_unit + SEGMENT_FLOOR_UNITS
        for index in range(end - 1, start - 1, -1):
            if offsets[index] < floor:
                break
            if body[index].isspace():
                cut = index + 1
                break
        cut_segments.append(body[start:cut])
        start = bisect_right(offsets, offsets[cut] - SEGMENT_OVERLAP_UNITS) - 1


@dataclass(frozen=True, slots=True)
class Segment:
    source: int
    """The source's position in the screened list (its rank order)."""
    order: int
    text: str
    units: int


@dataclass(slots=True)
class ShieldCall:
    """One call's documents, in the order they are sent."""

    documents: list[Segment] = field(default_factory=list)

    @property
    def units(self) -> int:
        return sum(segment.units for segment in self.documents)


def pack(bodies: Sequence[str]) -> list[ShieldCall]:
    """Every body's segments, first-fit decreasing into calls of at most 5 documents and 10,000
    units."""
    pieces = [
        Segment(source, order, text, utf16_units(text))
        for source, body in enumerate(bodies)
        for order, text in enumerate(segments(body))
    ]
    pieces.sort(key=lambda piece: (-piece.units, piece.source, piece.order))
    calls: list[ShieldCall] = []
    for piece in pieces:
        for call in calls:
            if (
                len(call.documents) < CALL_MAX_DOCUMENTS
                and call.units + piece.units <= CALL_MAX_UNITS
            ):
                call.documents.append(piece)
                break
        else:
            calls.append(ShieldCall([piece]))
    return calls


@dataclass(slots=True)
class ScreeningStats:
    """Requests sent (retries included), segments, and the UTF-16 units of the documents sent."""

    calls: int = 0
    segments: int = 0
    characters: int = 0


@dataclass(frozen=True, slots=True)
class ScreeningResult:
    """The positions of the flagged sources among the bodies screened."""

    flagged: frozenset[int]
    stats: ScreeningStats


def _retry_after(response: httpx.Response | None) -> float:
    if response is not None:
        value = response.headers.get("retry-after")
        try:
            seconds = float(value) if value is not None else None
        except ValueError:
            seconds = None
        if seconds is not None and 0 <= seconds <= RETRY_AFTER_MAX_SECONDS:
            return seconds
    return RETRY_WAIT_SECONDS


def _retryable_status(status: int) -> bool:
    return status == HTTPStatus.TOO_MANY_REQUESTS or status >= HTTPStatus.INTERNAL_SERVER_ERROR


def _attack_flags(body: object, count: int) -> list[bool] | None:
    if not isinstance(body, Mapping):
        return None
    analysis = body.get("documentsAnalysis")
    if not isinstance(analysis, list) or len(analysis) != count:
        return None
    flags: list[bool] = []
    for entry in analysis:
        flag = entry.get("attackDetected") if isinstance(entry, Mapping) else None
        if not isinstance(flag, bool):
            return None
        flags.append(flag)
    return flags


def _prompt_flag(body: object) -> bool | None:
    analysis = body.get("userPromptAnalysis") if isinstance(body, Mapping) else None
    flag = analysis.get("attackDetected") if isinstance(analysis, Mapping) else None
    return flag if isinstance(flag, bool) else None


class PromptShields:
    """The process's Prompt Shields caller over the one Content Safety client."""

    _current: ClassVar[PromptShields | None] = None

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        timeout_seconds: float,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._client = client
        self._timeout = httpx.Timeout(
            timeout_seconds, connect=CONTENTSAFETY_CONNECT_TIMEOUT_SECONDS
        )
        self._sleep = sleep

    def install(self) -> Self:
        """Make this the process's caller, which `shield_user_prompt` uses."""
        if PromptShields._current is not None:
            raise RuntimeError("the process's Prompt Shields caller is already installed")
        PromptShields._current = self
        return self

    def uninstall(self) -> None:
        if PromptShields._current is self:
            PromptShields._current = None

    @classmethod
    def current(cls) -> PromptShields:
        shields = cls._current
        if shields is None:
            raise RuntimeError("no Prompt Shields caller is installed")
        return shields

    async def _post(
        self, payload: Mapping[str, Any], stats: ScreeningStats | None = None
    ) -> object:
        """One screening request with its single retry; the decoded `2xx` body."""
        failure: FailureReason = "error"
        for attempt in range(ATTEMPTS):
            if stats is not None:
                stats.calls += 1
                stats.characters += sum(utf16_units(text) for text in payload["documents"])
            response: httpx.Response | None = None
            try:
                response = await self._client.post(
                    SHIELD_PATH,
                    params={"api-version": API_VERSION},
                    json=payload,
                    timeout=self._timeout,
                )
            except httpx.TimeoutException:
                failure = "timeout"
            except httpx.TransportError:
                failure = "error"
            else:
                if response.is_success:
                    try:
                        return response.json()
                    except ValueError:
                        raise ScreeningUnavailable("error") from None
                if not _retryable_status(response.status_code):
                    raise ScreeningUnavailable("error")
                throttled = response.status_code == HTTPStatus.TOO_MANY_REQUESTS
                failure = "throttled" if throttled else "error"
            if attempt + 1 < ATTEMPTS:
                await self._sleep(_retry_after(response))
        raise ScreeningUnavailable(failure)

    async def screen(self, bodies: Sequence[str], stats: ScreeningStats) -> ScreeningResult:
        """Screen each body; `stats` counts what was sent even when this raises
        `ScreeningUnavailable`. With no body, no call is made."""
        calls = pack(bodies)
        stats.segments = sum(len(call.documents) for call in calls)
        flagged: set[int] = set()
        limit = asyncio.Semaphore(CALLS_AT_ONCE)

        async def run(call: ShieldCall) -> None:
            async with limit:
                documents = [segment.text for segment in call.documents]
                body = await self._post(
                    {"userPrompt": SOURCE_USER_PROMPT, "documents": documents}, stats
                )
            flags = _attack_flags(body, len(documents))
            if flags is None:
                raise ScreeningUnavailable("error")
            flagged.update(
                segment.source for segment, flag in zip(call.documents, flags, strict=True) if flag
            )

        try:
            async with asyncio.TaskGroup() as group:
                for call in calls:
                    group.create_task(run(call))
        except* ScreeningUnavailable as failures:
            first = failures.exceptions[0]
            raise first if isinstance(first, ScreeningUnavailable) else failures from None
        return ScreeningResult(frozenset(flagged), stats)

    async def shield_user_prompt(self, text: str) -> bool:
        units = utf16_units(text)
        if not 1 <= units <= TEXT_MAX_UNITS:
            raise ValueError("a screened text holds 1 to 10,000 UTF-16 units")
        flag = _prompt_flag(await self._post({"userPrompt": text, "documents": []}))
        if flag is None:
            raise ScreeningUnavailable("error")
        return flag


async def shield_user_prompt(text: str) -> bool:
    """Screen one memory fact: `userPromptAnalysis.attackDetected`. `ValueError` for a text
    outside 1 to 10,000 UTF-16 units, with no call; `ScreeningUnavailable` after the second
    failure, and at once on another non-`2xx` status or a body without the flag."""
    return await PromptShields.current().shield_user_prompt(text)


def record_failure(failure: ScreeningUnavailable) -> None:
    """The source-screening failure that ends a turn, by reason."""
    telemetry.record_screening_failure(failure.reason)
