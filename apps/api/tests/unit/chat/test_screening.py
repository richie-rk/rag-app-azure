"""Prompt Shields: segments, packing, source screening and `shield_user_prompt` (T-236, T-239;
`12-chat-pipeline.md` section 8)."""

from __future__ import annotations

import itertools
import json
from collections.abc import Callable, Iterator
from typing import Any

import httpx
import pytest

from docchat_api.chat.screening import (
    API_VERSION,
    SHIELD_PATH,
    PromptShields,
    ScreeningStats,
    ScreeningUnavailable,
    pack,
    segments,
    shield_user_prompt,
    utf16_units,
)
from docchat_core.clients.tls import TLS_CONTEXT
from docchat_testing.fakes.chat import SHIELD_ATTACK, FakePromptShields

type Handler = Callable[[httpx.Request], httpx.Response]

GRINNING = "\U0001f600"


class Sleeps:
    def __init__(self) -> None:
        self.waits: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.waits.append(seconds)


def client_of(handler: Handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url="https://cs.example.invalid",
        transport=httpx.MockTransport(handler),
        timeout=httpx.Timeout(5.0),
        follow_redirects=False,
        trust_env=False,
        verify=TLS_CONTEXT,
    )


# --- T-236: segments and packing ------------------------------------------------------------------


def test_utf16_units_count_surrogate_pairs_twice() -> None:
    assert utf16_units("ab" + GRINNING) == 4


def test_a_short_body_is_one_segment() -> None:
    assert segments("x" * 10_000) == ["x" * 10_000]


def test_t236_a_long_body_is_cut_at_whitespace_with_a_200_unit_overlap() -> None:
    body = ("word " * 5_000).strip()

    parts = segments(body)

    assert len(parts) > 1
    assert all(utf16_units(part) <= 10_000 for part in parts)
    assert all(part.endswith(" ") for part in parts[:-1])
    for previous, following in itertools.pairwise(parts):
        assert previous[-200:] == following[:200]
    covered = parts[0] + "".join(part[200:] for part in parts[1:])
    assert covered == body


def test_t236_a_window_without_whitespace_is_cut_at_its_end_never_inside_a_pair() -> None:
    body = "x" * 9_999 + GRINNING + "y" * 6_000

    parts = segments(body)

    assert parts[0] == "x" * 9_999
    assert all(utf16_units(part) <= 10_000 for part in parts)
    assert parts[1].startswith("x" * 199)


def test_t236_whitespace_before_unit_5000_doesnt_count() -> None:
    body = "a " + "b" * 12_000

    assert len(segments(body)[0]) == 10_000


def test_t236_packing_is_first_fit_decreasing_within_5_documents_and_10000_units() -> None:
    bodies = ["a" * 6_000, "b" * 3_000, "c" * 5_000, "d" * 100, "e" * 100, "f" * 100, "g" * 100]

    calls = pack(bodies)

    assert all(len(call.documents) <= 5 and call.units <= 10_000 for call in calls)
    assert [[segment.source for segment in call.documents] for call in calls] == [
        [0, 1, 3, 4, 5],
        [2, 6],
    ]


def test_t236_a_long_body_splits_across_calls() -> None:
    calls = pack(["z " * 12_000])

    assert len(calls) == 3
    assert {segment.source for call in calls for segment in call.documents} == {0}


# --- T-236: source screening ----------------------------------------------------------------------


@pytest.fixture
def fake() -> FakePromptShields:
    return FakePromptShields()


@pytest.fixture
def shields(fake: FakePromptShields) -> PromptShields:
    return PromptShields(fake.client(), timeout_seconds=5, sleep=Sleeps())


async def test_t236_a_source_flagged_in_any_segment_is_dropped_by_position(
    fake: FakePromptShields, shields: PromptShields
) -> None:
    flagged_late = "q " * 6_000 + SHIELD_ATTACK
    bodies = ["clean one", flagged_late, "clean two", SHIELD_ATTACK]
    stats = ScreeningStats()

    result = await shields.screen(bodies, stats)

    assert result.flagged == frozenset({1, 3})
    assert stats.calls == len(fake.requests)
    assert stats.segments == sum(len(request["documents"]) for request in fake.requests)
    assert stats.characters == sum(
        utf16_units(text) for request in fake.requests for text in request["documents"]
    )
    assert all(request["userPrompt"] == "" for request in fake.requests)


async def test_no_body_makes_no_call(fake: FakePromptShields, shields: PromptShields) -> None:
    result = await shields.screen([], ScreeningStats())

    assert result.flagged == frozenset()
    assert fake.requests == []


async def test_a_second_failure_is_unavailable_and_counts_both_requests(
    fake: FakePromptShields,
) -> None:
    sleeps = Sleeps()
    shields = PromptShields(fake.client(), timeout_seconds=5, sleep=sleeps)
    fake.queued.extend([httpx.Response(503), httpx.Response(503)])
    stats = ScreeningStats()

    with pytest.raises(ScreeningUnavailable) as failure:
        await shields.screen(["a body"], stats)

    assert failure.value.reason == "error"
    assert stats.calls == 2
    assert sleeps.waits == [0.5]


async def test_a_body_without_one_flag_per_document_is_unavailable_at_once(
    fake: FakePromptShields, shields: PromptShields
) -> None:
    fake.queued.append(httpx.Response(200, json={"documentsAnalysis": []}))

    with pytest.raises(ScreeningUnavailable):
        await shields.screen(["a body"], ScreeningStats())
    assert len(fake.requests) == 1


# --- T-239: shield_user_prompt --------------------------------------------------------------------


class Recorded:
    """A handler answering from a script, recording each request."""

    def __init__(self, *answers: httpx.Response | Exception) -> None:
        self.answers = list(answers)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


def verdict(attack: Any) -> httpx.Response:
    return httpx.Response(200, json={"userPromptAnalysis": {"attackDetected": attack}})


@pytest.fixture
def installed() -> Iterator[None]:
    yield
    current = PromptShields._current
    if current is not None:
        current.uninstall()


@pytest.mark.parametrize("attack", [True, False])
async def test_t239_the_verdict_and_the_request(attack: bool, installed: None) -> None:
    handler = Recorded(verdict(attack))
    PromptShields(client_of(handler), timeout_seconds=5).install()

    assert await shield_user_prompt("Works on supplier renewals") is attack
    (request,) = handler.requests
    assert request.url.path == SHIELD_PATH
    assert request.url.params["api-version"] == API_VERSION
    assert json.loads(request.content) == {
        "userPrompt": "Works on supplier renewals",
        "documents": [],
    }
    assert request.extensions["timeout"]["read"] == 5


@pytest.mark.parametrize(
    "first",
    [
        httpx.ReadTimeout("slow"),
        httpx.ConnectError("refused"),
        httpx.Response(429),
        httpx.Response(503),
    ],
    ids=["timeout", "connection", "429", "503"],
)
async def test_t239_one_retry_after_half_a_second(first: httpx.Response | Exception) -> None:
    handler, sleeps = Recorded(first, verdict(False)), Sleeps()
    shields = PromptShields(client_of(handler), timeout_seconds=5, sleep=sleeps)

    assert await shields.shield_user_prompt("text") is False
    assert sleeps.waits == [0.5]
    assert len(handler.requests) == 2


async def test_t239_retry_after_2_is_honoured_and_3_is_not() -> None:
    handler, sleeps = (
        Recorded(httpx.Response(429, headers={"Retry-After": "2"}), verdict(True)),
        Sleeps(),
    )
    shields = PromptShields(client_of(handler), timeout_seconds=5, sleep=sleeps)
    assert await shields.shield_user_prompt("text") is True
    later = Recorded(httpx.Response(503, headers={"Retry-After": "3"}), verdict(True))
    slow_sleeps = Sleeps()
    await PromptShields(client_of(later), timeout_seconds=5, sleep=slow_sleeps).shield_user_prompt(
        "text"
    )

    assert sleeps.waits == [2.0]
    assert slow_sleeps.waits == [0.5]


async def test_t239_a_second_failure_raises() -> None:
    handler = Recorded(httpx.Response(500), httpx.ReadTimeout("slow"))
    shields = PromptShields(client_of(handler), timeout_seconds=5, sleep=Sleeps())

    with pytest.raises(ScreeningUnavailable) as failure:
        await shields.shield_user_prompt("text")
    assert failure.value.reason == "timeout"


@pytest.mark.parametrize(
    "answer",
    [
        httpx.Response(400, json={"error": {"code": "InvalidRequestBody"}}),
        httpx.Response(200, json={"userPromptAnalysis": {}}),
        httpx.Response(200, json={"userPromptAnalysis": {"attackDetected": "yes"}}),
        httpx.Response(200, content=b"not json"),
    ],
    ids=["400", "missing", "not-boolean", "not-json"],
)
async def test_t239_unusable_answers_raise_at_once(answer: httpx.Response) -> None:
    handler, sleeps = Recorded(answer), Sleeps()
    shields = PromptShields(client_of(handler), timeout_seconds=5, sleep=sleeps)

    with pytest.raises(ScreeningUnavailable):
        await shields.shield_user_prompt("text")
    assert len(handler.requests) == 1
    assert sleeps.waits == []


@pytest.mark.parametrize("text", ["", "x" * 10_001, GRINNING * 5_001])
async def test_t239_a_text_outside_1_to_10000_units_makes_no_call(text: str) -> None:
    handler = Recorded()
    shields = PromptShields(client_of(handler), timeout_seconds=5)

    with pytest.raises(ValueError, match="UTF-16"):
        await shields.shield_user_prompt(text)
    assert handler.requests == []


def test_installing_twice_is_refused(installed: None) -> None:
    first = PromptShields(client_of(Recorded()), timeout_seconds=5).install()

    with pytest.raises(RuntimeError, match="already installed"):
        PromptShields(client_of(Recorded()), timeout_seconds=5).install()
    first.uninstall()
    with pytest.raises(RuntimeError, match="no Prompt Shields"):
        PromptShields.current()
