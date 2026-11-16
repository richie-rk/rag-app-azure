from __future__ import annotations

import asyncio
import json
import re
from collections.abc import MutableMapping
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from docchat_model_stub.app import HangResponse, StubSettings, create_app

CHAT = "/openai/v1/chat/completions"
EMBEDDINGS = "/openai/v1/embeddings"
SHIELD = "/contentsafety/text:shieldPrompt"
AUTH = {"Authorization": "Bearer any-token"}
FAST: dict[str, Any] = {
    "first_token_delay_ms": 0,
    "token_delay_ms": 0,
    "json_delay_ms": 0,
    "embedding_delay_ms": 0,
}
SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "standalone_query",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
            "additionalProperties": False,
        },
    },
}


def _client(**settings: Any) -> httpx.AsyncClient:
    app = create_app(StubSettings(**{**FAST, **settings}))
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://stub", headers=AUTH
    )


def _chat_body(question: str = "What is it?", **fields: Any) -> dict[str, Any]:
    return {
        "model": "chat",
        "messages": [
            {"role": "system", "content": "rules"},
            {"role": "user", "content": question},
        ],
        "max_completion_tokens": 100,
        "reasoning_effort": "low",
        **fields,
    }


def _stream_events(response: httpx.Response) -> list[Any]:
    events: list[Any] = []
    for block in response.text.split("\n\n"):
        if block:
            payload = block.removeprefix("data: ")
            events.append(payload if payload == "[DONE]" else json.loads(payload))
    return events


def _stream_text(events: list[Any]) -> str:
    return "".join(
        event["choices"][0]["delta"].get("content", "")
        for event in events
        if isinstance(event, dict) and event["choices"]
    )


async def test_healthz() -> None:
    async with _client() as client:
        response = await client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_no_documentation_routes() -> None:
    async with _client() as client:
        assert (await client.get("/openapi.json")).status_code == 404
        assert (await client.get("/docs")).status_code == 404


@pytest.mark.parametrize("path", [CHAT, EMBEDDINGS, SHIELD])
@pytest.mark.parametrize("authorization", [None, "Basic abc", "Bearer ", "Bearer"])
async def test_a_bearer_header_is_required(path: str, authorization: str | None) -> None:
    headers = {} if authorization is None else {"Authorization": authorization}
    async with _client() as client:
        client.headers.pop("Authorization")
        response = await client.post(path, json={}, headers=headers)
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "401"
        assert (await client.get("/_stub/requests")).json() == {"requests": []}


async def test_any_bearer_token_and_api_version_are_accepted() -> None:
    async with _client() as client:
        response = await client.post(
            CHAT,
            params={"api-version": "anything"},
            json=_chat_body(),
            headers={"Authorization": "bearer x"},
        )
    assert response.status_code == 200


@pytest.mark.parametrize("path", [CHAT, EMBEDDINGS, SHIELD])
@pytest.mark.parametrize("content", [b"not json", b"[1, 2]", b"\xff\xfe\x00"])
async def test_bodies_that_are_not_json_objects(path: str, content: bytes) -> None:
    async with _client() as client:
        response = await client.post(path, content=content)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "InvalidRequestBody"


async def test_a_body_that_does_not_validate_names_no_input() -> None:
    async with _client() as client:
        response = await client.post(CHAT, json={"model": "chat", "messages": "cnry-question"})
    assert response.status_code == 400
    assert response.json()["error"] == {
        "code": "InvalidRequestBody",
        "message": "The request body is invalid.",
    }


async def test_plain_text_answer() -> None:
    async with _client(text_tokens=5) as client:
        response = await client.post(CHAT, json=_chat_body())
        stats = (await client.get("/_stub/stats")).json()
    body = response.json()
    assert response.status_code == 200
    assert body["object"] == "chat.completion"
    assert len(body["choices"][0]["message"]["content"].split()) == 5
    assert body["usage"]["completion_tokens"] == 5
    assert stats == {
        "chat": {"started": 1, "completed": 1, "throttled": 0, "clientDisconnected": 0}
    }


async def test_structured_answer_from_the_schema() -> None:
    async with _client() as client:
        response = await client.post(CHAT, json=_chat_body(response_format=SCHEMA))
    content = response.json()["choices"][0]["message"]["content"]
    assert json.loads(content) == {"query": "stub-query"}


async def test_scripted_responses_come_before_the_schema_fill() -> None:
    async with _client() as client:
        put = await client.put(
            "/_stub/scripts/standalone_query",
            json={"responses": [{"query": "first"}, "not json"]},
        )
        contents = [
            (await client.post(CHAT, json=_chat_body(response_format=SCHEMA))).json()["choices"][0][
                "message"
            ]["content"]
            for _ in range(3)
        ]
    assert put.status_code == 204
    assert contents == ['{"query": "first"}', "not json", '{"query": "stub-query"}']


async def test_delete_scripts() -> None:
    async with _client() as client:
        await client.put("/_stub/scripts/standalone_query", json={"responses": [{"query": "a"}]})
        assert (await client.delete("/_stub/scripts")).status_code == 204
        response = await client.post(CHAT, json=_chat_body(response_format=SCHEMA))
    assert json.loads(response.json()["choices"][0]["message"]["content"]) == {
        "query": "stub-query"
    }


@pytest.mark.parametrize(
    ("name", "body"),
    [
        ("standalone_query", {"responses": "x"}),
        ("standalone_query", {"other": []}),
        ("shield_prompt", {"responses": [3]}),
        ("shield_prompt", {"responses": [{"status": 503}]}),
    ],
)
async def test_invalid_scripts_are_refused(name: str, body: dict[str, Any]) -> None:
    async with _client() as client:
        response = await client.put(f"/_stub/scripts/{name}", json=body)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "InvalidRequestBody"


async def test_a_broken_schema_reference_is_an_invalid_body() -> None:
    schema = {
        "type": "json_schema",
        "json_schema": {"name": "broken", "schema": {"$ref": "#/$defs/Missing"}},
    }
    async with _client() as client:
        response = await client.post(CHAT, json=_chat_body(response_format=schema))
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "InvalidRequestBody"


async def test_streamed_answer_with_usage_and_citations() -> None:
    sources = "The sources below.\n\n[[BEGIN SOURCE S1 ab]]\nx\n[[END SOURCE S1 ab]]"
    body = _chat_body(stream=True, stream_options={"include_usage": True})
    body["messages"].insert(1, {"role": "user", "content": sources})
    async with _client(answer_tokens=12, tokens_per_chunk=4) as client:
        response = await client.post(CHAT, json=body)
        stats = (await client.get("/_stub/stats")).json()
    assert response.headers["content-type"].startswith("text/event-stream")
    events = _stream_events(response)
    assert events[-1] == "[DONE]"
    assert events[-2]["usage"]["completion_tokens"] == 12
    assert events[-3]["choices"][0]["finish_reason"] == "stop"
    assert "given. [S1] It" in _stream_text(events)
    assert len(events) == 1 + 3 + 3
    assert stats["chat"]["completed"] == 1


async def test_stream_directives() -> None:
    question = "Q [[stub:tokens=3]] [[stub:no_usage]] [[stub:filter=completion]]"
    body = _chat_body(question, stream=True, stream_options={"include_usage": True})
    async with _client() as client:
        events = _stream_events(await client.post(CHAT, json=body))
    assert len(_stream_text(events).split()) == 3
    assert events[-2]["choices"][0]["finish_reason"] == "content_filter"
    assert all("usage" not in event for event in events if isinstance(event, dict))


async def test_cut_stream() -> None:
    body = _chat_body("Q [[stub:cut=3]]", stream=True, stream_options={"include_usage": True})
    async with _client() as client:
        response = await client.post(CHAT, json=body)
        stats = (await client.get("/_stub/stats")).json()
    events = _stream_events(response)
    assert response.headers["connection"] == "close"
    assert len(events) == 4
    assert "[DONE]" not in events
    assert stats["chat"]["completed"] == 0


async def test_first_and_delay_directives_are_accepted() -> None:
    body = _chat_body("Q [[stub:first_ms=0]] [[stub:delay_ms=0]] [[stub:tokens=2]]", stream=True)
    async with _client(first_token_delay_ms=60_000, token_delay_ms=5_000) as client:
        events = _stream_events(await client.post(CHAT, json=body))
    assert len(_stream_text(events).split()) == 2


async def test_first_ms_replaces_the_json_delay() -> None:
    async with _client(json_delay_ms=60_000) as client:
        response = await client.post(CHAT, json=_chat_body("Q [[stub:first_ms=0]]"))
    assert response.status_code == 200


async def test_status_directive() -> None:
    async with _client(retry_after_ms=1500) as client:
        busy = await client.post(CHAT, json=_chat_body("Q [[stub:status=429]]"))
        failed = await client.post(CHAT, json=_chat_body("Q [[stub:status=500]]"))
        stats = (await client.get("/_stub/stats")).json()
    assert busy.status_code == 429
    assert busy.headers["retry-after-ms"] == "1500"
    assert busy.headers["retry-after"] == "2"
    assert busy.json() == {"error": {"code": "429", "message": "stub directive"}}
    assert failed.status_code == 500
    assert "retry-after-ms" not in failed.headers
    assert failed.json() == {"error": {"code": "500", "message": "stub directive"}}
    assert stats["chat"] == {
        "started": 2,
        "completed": 0,
        "throttled": 1,
        "clientDisconnected": 0,
    }


async def test_a_qualified_directive_spares_other_kinds() -> None:
    question = "Q [[stub:status=500@stream]]"
    async with _client() as client:
        structured = await client.post(CHAT, json=_chat_body(question, response_format=SCHEMA))
        text = await client.post(CHAT, json=_chat_body(question))
        streamed = await client.post(CHAT, json=_chat_body(question, stream=True))
    assert structured.status_code == 200
    assert text.status_code == 200
    assert streamed.status_code == 500


async def test_directives_only_count_in_the_last_user_message() -> None:
    body = _chat_body("plain question")
    body["messages"].insert(1, {"role": "user", "content": "[[stub:status=500]]"})
    async with _client() as client:
        response = await client.post(CHAT, json=body)
    assert response.status_code == 200


async def test_filter_prompt() -> None:
    async with _client() as client:
        response = await client.post(CHAT, json=_chat_body("Q [[stub:filter=prompt]]"))
    assert response.status_code == 400
    assert response.json() == {
        "error": {
            "code": "content_filter",
            "message": "stub filter",
            "innererror": {"code": "ResponsibleAIPolicyViolation"},
        }
    }


async def test_unknown_directive() -> None:
    async with _client() as client:
        response = await client.post(CHAT, json=_chat_body("Q [[stub:bogus]]"))
        recorded = (await client.get("/_stub/requests")).json()
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "unknown_stub_directive"
    assert recorded == {"requests": []}


async def test_parameter_checks() -> None:
    async with _client() as client:
        sampling = await client.post(CHAT, json=_chat_body(temperature=0.2))
        effort = await client.post(CHAT, json=_chat_body(reasoning_effort="minimal"))
    assert sampling.status_code == 400
    assert sampling.json()["error"]["code"] == "unsupported_parameter"
    assert sampling.json()["error"]["param"] == "temperature"
    assert effort.status_code == 400
    assert effort.json()["error"]["code"] == "unsupported_value"


async def test_a_status_directive_comes_before_the_parameter_checks() -> None:
    async with _client() as client:
        response = await client.post(
            CHAT, json=_chat_body("Q [[stub:status=503]]", temperature=0.2)
        )
    assert response.status_code == 503


async def test_throttling_comes_before_the_status_directive() -> None:
    async with _client(throttle_every_n=1) as client:
        response = await client.post(CHAT, json=_chat_body("Q [[stub:status=500]]"))
    assert response.status_code == 429
    assert response.json() == {"error": {"code": "429", "message": "stub throttle"}}
    assert response.headers["retry-after-ms"] == "200"


async def test_throttling_every_nth_request_of_listed_deployments() -> None:
    async with _client(throttle_every_n=2, throttle_deployments=("chat-small",)) as client:
        small = [
            (await client.post(CHAT, json=_chat_body(model="chat-small"))).status_code
            for _ in range(4)
        ]
        chat = [(await client.post(CHAT, json=_chat_body())).status_code for _ in range(4)]
        stats = (await client.get("/_stub/stats")).json()
    assert small == [200, 429, 200, 429]
    assert chat == [200, 200, 200, 200]
    assert stats["chat-small"] == {
        "started": 4,
        "completed": 2,
        "throttled": 2,
        "clientDisconnected": 0,
    }


async def test_the_recorder() -> None:
    async with _client() as client:
        await client.post(CHAT, json=_chat_body(safety_identifier="abc"))
        await client.post(EMBEDDINGS, json={"model": "embed-query", "input": "q"})
        await client.post(SHIELD, json={"userPrompt": "", "documents": ["d"]})
        everything = (await client.get("/_stub/requests")).json()["requests"]
        later = (await client.get("/_stub/requests", params={"after": 2})).json()["requests"]
        assert (await client.delete("/_stub/requests")).status_code == 204
        cleared = (await client.get("/_stub/requests")).json()["requests"]
        refused = await client.get("/_stub/requests", params={"after": -1})
    assert [(entry["seq"], entry["path"], entry["deployment"]) for entry in everything] == [
        (1, CHAT, "chat"),
        (2, EMBEDDINGS, "embed-query"),
        (3, SHIELD, None),
    ]
    assert everything[0]["body"]["safety_identifier"] == "abc"
    assert "Authorization" not in json.dumps(everything)
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z", everything[0]["time"])
    assert [entry["seq"] for entry in later] == [3]
    assert cleared == []
    assert refused.status_code == 422


async def test_recording_off() -> None:
    async with _client(record=False) as client:
        await client.post(CHAT, json=_chat_body())
        assert (await client.get("/_stub/requests")).json() == {"requests": []}


async def test_embeddings() -> None:
    body = {"model": "embed-query", "input": ["a", "a", "bb"], "dimensions": 16}
    async with _client() as client:
        response = await client.post(EMBEDDINGS, json=body)
        stats = (await client.get("/_stub/stats")).json()
    data = response.json()["data"]
    assert [len(item["embedding"]) for item in data] == [16, 16, 16]
    assert data[0]["embedding"] == data[1]["embedding"] != data[2]["embedding"]
    assert response.json()["usage"] == {"prompt_tokens": 1, "total_tokens": 1}
    assert stats["embed-query"]["completed"] == 1


async def test_embedding_limits() -> None:
    async with _client() as client:
        wide = await client.post(EMBEDDINGS, json={"model": "e", "input": "a", "dimensions": 4000})
        empty = await client.post(EMBEDDINGS, json={"model": "e", "input": []})
    assert wide.status_code == 400
    assert empty.status_code == 400
    assert wide.json()["error"]["code"] == "InvalidRequestBody"


async def test_embedding_directives() -> None:
    async with _client() as client:
        failed = await client.post(
            EMBEDDINGS, json={"model": "e", "input": ["a", "b [[stub:status=503@embed]]"]}
        )
        spared = await client.post(
            EMBEDDINGS, json={"model": "e", "input": "b [[stub:status=503@stream]]"}
        )
        unknown = await client.post(EMBEDDINGS, json={"model": "e", "input": "[[stub:nope]]"})
        unfiltered = await client.post(
            EMBEDDINGS, json={"model": "e", "input": "[[stub:filter=prompt]]"}
        )
    assert failed.status_code == 503
    assert spared.status_code == 200
    assert unknown.status_code == 400
    assert unfiltered.status_code == 200


async def test_screening() -> None:
    body = {"userPrompt": "", "documents": ["fine", "bad [[stub:shield=attack]]"]}
    async with _client() as client:
        response = await client.post(SHIELD, params={"api-version": "2024-09-01"}, json=body)
        stats = (await client.get("/_stub/stats")).json()
    assert response.json() == {
        "userPromptAnalysis": {"attackDetected": False},
        "documentsAnalysis": [{"attackDetected": False}, {"attackDetected": True}],
    }
    assert stats == {}


async def test_screening_limits() -> None:
    async with _client() as client:
        many = await client.post(SHIELD, json={"userPrompt": "", "documents": ["d"] * 6})
        long = await client.post(SHIELD, json={"userPrompt": "x" * 10_001, "documents": []})
        typed = await client.post(SHIELD, json={"userPrompt": "", "documents": [1]})
        recorded = (await client.get("/_stub/requests")).json()["requests"]
    for response in (many, long, typed):
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "InvalidRequestBody"
    assert recorded == []


async def test_scripted_screening_responses_come_first() -> None:
    attack = {"userPromptAnalysis": {"attackDetected": True}, "documentsAnalysis": []}
    failure = {"status": 503, "error": {"code": "ServiceUnavailable", "message": "busy"}}
    body = {"userPrompt": "fact [[stub:status=500]]", "documents": []}
    async with _client() as client:
        await client.put("/_stub/scripts/shield_prompt", json={"responses": [attack, failure]})
        first = await client.post(SHIELD, json=body)
        second = await client.post(SHIELD, json=body)
        third = await client.post(SHIELD, json=body)
    assert (first.status_code, first.json()) == (200, attack)
    assert (second.status_code, second.json()) == (503, {"error": failure["error"]})
    assert third.status_code == 500


async def test_screening_directives() -> None:
    async with _client(retry_after_ms=100) as client:
        busy = await client.post(SHIELD, json={"userPrompt": "[[stub:status=429@shield]]"})
        unknown = await client.post(SHIELD, json={"documents": ["[[stub:what]]"]})
    assert busy.status_code == 429
    assert busy.headers["retry-after-ms"] == "100"
    assert unknown.status_code == 400
    assert unknown.json()["error"]["code"] == "unknown_stub_directive"


def test_settings_defaults() -> None:
    settings = StubSettings()
    assert settings.workers == 1
    assert settings.record is True
    assert settings.first_token_delay_ms == 300
    assert settings.token_delay_ms == 10
    assert settings.tokens_per_chunk == 1
    assert settings.answer_tokens == 60
    assert settings.text_tokens == 40
    assert settings.json_delay_ms == 20
    assert settings.embedding_delay_ms == 5
    assert settings.throttle_every_n == 0
    assert settings.throttle_deployments == ()
    assert settings.retry_after_ms == 200
    assert settings.source_id_pattern.pattern == r"\[\[BEGIN SOURCE (S\d{1,2}) "


def test_settings_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DOCCHAT_STUB_WORKERS", "2")
    monkeypatch.setenv("DOCCHAT_STUB_RECORD", "false")
    monkeypatch.setenv(
        "DOCCHAT_STUB_THROTTLE_DEPLOYMENTS", "embed-ingest-te3-large-v1, ingest-summary"
    )
    monkeypatch.setenv("DOCCHAT_STUB_SOURCE_ID_PATTERN", r"<(S\d+)>")
    settings = StubSettings()
    assert settings.workers == 2
    assert settings.record is False
    assert settings.throttle_deployments == ("embed-ingest-te3-large-v1", "ingest-summary")
    assert settings.source_id_pattern.findall("<S3>") == ["S3"]


def test_an_empty_throttle_list_means_every_deployment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DOCCHAT_STUB_THROTTLE_DEPLOYMENTS", " ")
    assert StubSettings().throttle_deployments == ()


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("DOCCHAT_STUB_WORKERS", "0"),
        ("DOCCHAT_STUB_WORKERS", "9"),
        ("DOCCHAT_STUB_TOKENS_PER_CHUNK", "17"),
        ("DOCCHAT_STUB_ANSWER_TOKENS", "0"),
        ("DOCCHAT_STUB_THROTTLE_EVERY_N", "1001"),
        ("DOCCHAT_STUB_RETRY_AFTER_MS", "-1"),
        ("DOCCHAT_STUB_THROTTLE_DEPLOYMENTS", "chat,,embed"),
        ("DOCCHAT_STUB_THROTTLE_DEPLOYMENTS", "bad name"),
        ("DOCCHAT_STUB_SOURCE_ID_PATTERN", "no group"),
        ("DOCCHAT_STUB_SOURCE_ID_PATTERN", "(a)(b)"),
        ("DOCCHAT_STUB_SOURCE_ID_PATTERN", "(unclosed"),
        ("DOCCHAT_STUB_RECORD", "maybe"),
    ],
)
def test_invalid_settings_stop_the_stub(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    monkeypatch.setenv(name, value)
    with pytest.raises(ValidationError):
        StubSettings()


def test_settings_are_frozen() -> None:
    settings = StubSettings()
    with pytest.raises(ValidationError):
        settings.workers = 2  # type: ignore[misc]


async def test_hang_response_ignores_messages_before_the_disconnect() -> None:
    disconnects: list[bool] = []
    messages: list[dict[str, Any]] = [
        {"type": "http.request", "body": b"", "more_body": False},
        {"type": "http.disconnect"},
    ]
    sent: list[MutableMapping[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return messages.pop(0)

    async def send(message: MutableMapping[str, Any]) -> None:
        sent.append(message)

    response = HangResponse("application/json", lambda: disconnects.append(True))
    await response({"type": "http"}, receive, send)
    assert [message["type"] for message in sent] == ["http.response.start"]
    assert disconnects == [True]
    assert messages == []


async def _run_until_disconnect(
    path: str, body: dict[str, Any], **settings: Any
) -> tuple[list[MutableMapping[str, Any]], dict[str, Any]]:
    """Drive the ASGI app directly: send the body, let it answer, then disconnect."""
    app = create_app(StubSettings(**{**FAST, **settings}))
    started = asyncio.Event()
    sent: list[MutableMapping[str, Any]] = []
    messages: list[dict[str, Any]] = [
        {"type": "http.request", "body": json.dumps(body).encode(), "more_body": False}
    ]

    async def receive() -> dict[str, Any]:
        if messages:
            return messages.pop(0)
        await started.wait()
        return {"type": "http.disconnect"}

    async def send(message: MutableMapping[str, Any]) -> None:
        sent.append(message)
        if message["type"] == "http.response.start":
            started.set()

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "root_path": "",
        "query_string": b"",
        "headers": [(b"authorization", b"Bearer t"), (b"content-type", b"application/json")],
        "client": ("127.0.0.1", 1),
        "server": ("stub", 80),
    }
    await asyncio.wait_for(app(scope, receive, send), timeout=5)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://stub"
    ) as client:
        stats = (await client.get("/_stub/stats")).json()
    return sent, stats


@pytest.mark.parametrize(
    ("path", "body", "media_type"),
    [
        (CHAT, _chat_body("Q [[stub:hang]]", stream=True), b"text/event-stream"),
        (CHAT, _chat_body("Q [[stub:hang]]"), b"application/json"),
        (EMBEDDINGS, {"model": "chat", "input": "[[stub:hang@embed]]"}, b"application/json"),
    ],
)
async def test_hang_holds_until_the_client_disconnects(
    path: str, body: dict[str, Any], media_type: bytes
) -> None:
    sent, stats = await _run_until_disconnect(path, body)
    start, *rest = sent
    assert start["status"] == 200
    assert dict(start["headers"])[b"content-type"].startswith(media_type)
    assert b"content-length" not in dict(start["headers"])
    assert rest == []
    assert stats["chat"]["clientDisconnected"] == 1
    assert stats["chat"]["completed"] == 0


async def test_screening_hang_counts_nothing() -> None:
    sent, stats = await _run_until_disconnect(SHIELD, {"documents": ["[[stub:hang]]"]})
    assert [message["type"] for message in sent] == ["http.response.start"]
    assert stats == {}


async def test_a_stream_stops_at_the_disconnect() -> None:
    body = _chat_body("Q [[stub:first_ms=60000]]", stream=True)
    sent, stats = await _run_until_disconnect(CHAT, body)
    assert sent[0]["status"] == 200
    assert stats["chat"]["clientDisconnected"] == 1
    assert stats["chat"]["completed"] == 0
