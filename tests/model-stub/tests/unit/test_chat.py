from __future__ import annotations

import asyncio
import json
import re
from typing import Any

import pytest
from pydantic import ValidationError

from docchat_model_stub.chat import (
    WORDS,
    ChatRequest,
    StreamPlan,
    cited_sources,
    completion_body,
    json_content,
    parameter_problem,
    stream_events,
    text_content,
    words,
)
from docchat_model_stub.recorder import DeploymentStats

SOURCE_PATTERN = re.compile(r"\[\[BEGIN SOURCE (S\d{1,2}) ")


def _request(**fields: Any) -> ChatRequest:
    body: dict[str, Any] = {"model": "chat", "messages": [{"role": "user", "content": "hi"}]}
    body.update(fields)
    return ChatRequest.model_validate(body)


def _plan(**fields: Any) -> StreamPlan:
    values: dict[str, Any] = {
        "deployment": "chat",
        "prompt_tokens": 5,
        "words": 4,
        "words_per_chunk": 1,
        "first_delay_ms": 0,
        "word_delay_ms": 0,
        "citations": (),
        "include_usage": True,
        "finish_reason": "stop",
        "cut_after": None,
    }
    values.update(fields)
    return StreamPlan(**values)


async def _connected() -> bool:
    return False


async def _events(plan: StreamPlan, stats: DeploymentStats) -> list[Any]:
    events: list[Any] = []
    async for raw in stream_events(plan, _connected, stats):
        text = raw.decode()
        assert text.startswith("data: ")
        assert text.endswith("\n\n")
        payload = text.removeprefix("data: ").removesuffix("\n\n")
        events.append(payload if payload == "[DONE]" else json.loads(payload))
    return events


def _content(events: list[Any]) -> str:
    return "".join(
        event["choices"][0]["delta"].get("content", "")
        for event in events
        if isinstance(event, dict) and event["choices"]
    )


def test_request_kinds() -> None:
    schema = {"type": "json_schema", "json_schema": {"name": "s", "schema": {}}}
    assert _request(stream=True).kind() == "stream"
    assert _request(response_format=schema).kind() == "json"
    assert _request(response_format={"type": "text"}).kind() == "text"
    assert _request().kind() == "text"
    assert _request(response_format=schema).json_schema() is not None
    assert _request().json_schema() is None


@pytest.mark.parametrize(
    "fields",
    [
        {"messages": []},
        {"messages": "hi"},
        {"model": 3},
        {"stream": "true"},
        {"response_format": {"type": "json_schema"}},
        {"response_format": {"type": "json_schema", "json_schema": {"schema": {}}}},
        {"stream_options": {"include_usage": "yes"}},
        {"messages": [{"role": "user", "content": 7}]},
    ],
)
def test_malformed_requests_are_refused(fields: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        _request(**fields)


def test_unknown_parameters_are_accepted() -> None:
    request = _request(safety_identifier="abc", prompt_cache_options={"mode": "explicit"})
    assert request.model == "chat"


def test_last_user_text_reads_the_last_user_message() -> None:
    request = _request(
        messages=[
            {"role": "system", "content": "rules"},
            {"role": "user", "content": "sources"},
            {"role": "assistant", "content": None},
            {"role": "user", "content": [{"type": "text", "text": "the "}, {"type": "text"}]},
        ]
    )
    assert request.last_user_text() == "the "
    assert _request(messages=[{"role": "system", "content": "x"}]).last_user_text() == ""


def test_prompt_tokens_count_every_message() -> None:
    request = _request(
        messages=[
            {"role": "system", "content": "abcd"},
            {"role": "assistant", "content": None},
            {"role": "user", "content": "e"},
        ]
    )
    assert request.prompt_tokens() == 2


def test_include_usage() -> None:
    assert _request(stream=True, stream_options={"include_usage": True}).include_usage()
    assert not _request(stream=True).include_usage()


@pytest.mark.parametrize(
    "name", ["temperature", "top_p", "max_tokens", "presence_penalty", "frequency_penalty"]
)
def test_sampling_parameters_are_unsupported(name: str) -> None:
    problem = parameter_problem({"model": "chat", name: 0})
    assert problem is not None
    assert (problem.code, problem.param) == ("unsupported_parameter", name)


@pytest.mark.parametrize("effort", ["none", "low", "medium", "high"])
def test_supported_efforts(effort: str) -> None:
    assert parameter_problem({"reasoning_effort": effort, "max_completion_tokens": 10}) is None


@pytest.mark.parametrize("effort", ["minimal", "xhigh", "max", "", None, 1])
def test_other_efforts_are_unsupported_values(effort: object) -> None:
    problem = parameter_problem({"reasoning_effort": effort})
    assert problem is not None
    assert (problem.code, problem.param) == ("unsupported_value", "reasoning_effort")


def test_words_cycle_the_constant_list() -> None:
    assert words(3) == list(WORDS[:3])
    assert words(len(WORDS) + 2) == [*WORDS, *WORDS[:2]]


def test_cited_sources_finds_two_distinct_ids() -> None:
    sources = (
        "[[BEGIN SOURCE S2 abc]]\nx\n[[END SOURCE S2 abc]]\n"
        "[[BEGIN SOURCE S2 abc]]\n[[BEGIN SOURCE S7 abc]]\n[[BEGIN SOURCE S9 abc]]"
    )
    request = _request(messages=[{"role": "user", "content": sources}])
    assert cited_sources(request, SOURCE_PATTERN) == ["S2", "S7"]
    assert cited_sources(_request(), SOURCE_PATTERN) == []


def test_completion_body() -> None:
    request = _request(messages=[{"role": "user", "content": "abcdefgh"}])
    body = completion_body(request, "two words", 2)
    assert body["object"] == "chat.completion"
    assert body["model"] == "chat"
    assert body["id"].startswith("chatcmpl-stub-")
    assert body["choices"] == [
        {
            "index": 0,
            "message": {"role": "assistant", "content": "two words", "refusal": None},
            "finish_reason": "stop",
            "logprobs": None,
        }
    ]
    assert body["usage"] == {
        "prompt_tokens": 2,
        "completion_tokens": 2,
        "total_tokens": 4,
        "completion_tokens_details": {"reasoning_tokens": 0},
        "prompt_tokens_details": {"cached_tokens": 0},
    }


def test_text_content() -> None:
    assert text_content(3) == (" ".join(WORDS[:3]), 3)


def test_json_content() -> None:
    assert json_content({"query": "é"}) == ('{"query": "é"}', 4)
    assert json_content("not json") == ("not json", 2)


async def test_a_full_stream() -> None:
    stats = DeploymentStats()
    events = await _events(_plan(words=12, words_per_chunk=5), stats)
    role, *content, final, usage, done = events
    assert role["choices"][0]["delta"] == {"role": "assistant", "content": ""}
    assert len(content) == 3
    assert _content(events) == " ".join(WORDS[:12])
    assert final["choices"][0] == {
        "index": 0,
        "delta": {},
        "finish_reason": "stop",
        "logprobs": None,
    }
    assert usage["choices"] == []
    assert usage["usage"]["prompt_tokens"] == 5
    assert usage["usage"]["completion_tokens"] == 12
    assert usage["usage"]["completion_tokens_details"] == {"reasoning_tokens": 0}
    assert usage["usage"]["prompt_tokens_details"] == {"cached_tokens": 0}
    assert done == "[DONE]"
    assert {event["id"] for event in events if isinstance(event, dict)} == {role["id"]}
    assert stats.completed == 1


async def test_citation_markers_follow_the_first_sentence() -> None:
    events = await _events(_plan(words=12, citations=("S1", "S3")), DeploymentStats())
    first_sentence = " ".join(WORDS[:10])
    assert first_sentence.endswith(".")
    assert _content(events) == f"{first_sentence} [S1][S3] {' '.join(WORDS[10:12])}"


async def test_no_usage_and_content_filter() -> None:
    events = await _events(
        _plan(include_usage=False, finish_reason="content_filter"), DeploymentStats()
    )
    *_, final, done = events
    assert final["choices"][0]["finish_reason"] == "content_filter"
    assert done == "[DONE]"


@pytest.mark.parametrize(("cut", "chunks"), [(0, 0), (2, 2), (4, 4), (9, 4)])
async def test_a_cut_stream_has_no_final_chunk(cut: int, chunks: int) -> None:
    stats = DeploymentStats()
    events = await _events(_plan(words=4, cut_after=cut), stats)
    assert len(events) == 1 + chunks
    assert all(event["choices"][0]["finish_reason"] is None for event in events)
    assert stats.completed == 0
    assert stats.client_disconnected == 0


async def test_a_disconnect_stops_the_stream() -> None:
    stats = DeploymentStats()
    answers = iter([False, False, True])

    async def disconnects_later() -> bool:
        return next(answers)

    events = [raw async for raw in stream_events(_plan(), disconnects_later, stats)]
    assert len(events) == 2
    assert stats.client_disconnected == 1
    assert stats.completed == 0


async def test_a_disconnect_before_the_first_chunk() -> None:
    stats = DeploymentStats()

    async def gone() -> bool:
        return True

    assert [raw async for raw in stream_events(_plan(), gone, stats)] == []
    assert stats.client_disconnected == 1


async def test_cancellation_counts_as_a_disconnect() -> None:
    stats = DeploymentStats()
    started = asyncio.Event()

    async def consume() -> None:
        async for _ in stream_events(_plan(first_delay_ms=60_000), _connected, stats):
            started.set()

    task = asyncio.create_task(consume())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert stats.client_disconnected == 1
