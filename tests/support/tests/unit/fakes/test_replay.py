from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from azure.core.credentials import AzureKeyCredential
from azure.core.exceptions import ResourceNotFoundError
from azure.search.documents.aio import SearchClient

from docchat_testing.fakes.replay import (
    FIXTURES_HTTP_ROOT,
    Exchange,
    ReplayMismatchError,
    ReplayTransport,
    load_exchanges,
)

ENDPOINT = "https://srch-docchat-dev.search.windows.net"
SEARCH_PATH = "/indexes('docchat-0123456789abcdef0123456789abcdef-g1')/docs/search.post.search"


def search_exchange(body: dict[str, Any], hits: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "request": {
            "method": "post",
            "path": SEARCH_PATH,
            "query": {"api-version": "2026-04-01"},
            "json": body,
        },
        "response": {"status": 200, "json": {"value": hits}},
    }


def client(transport: ReplayTransport) -> SearchClient:
    return SearchClient(
        ENDPOINT,
        "docchat-0123456789abcdef0123456789abcdef-g1",
        AzureKeyCredential("unused"),
        transport=transport,
        api_version="2026-04-01",
    )


async def test_an_sdk_client_gets_the_recorded_answer() -> None:
    hit = {"@search.score": 1.5, "chunkKey": "k1"}
    transport = ReplayTransport(
        [
            Exchange.from_json(search_exchange({"search": "other", "top": 3}, [])),
            Exchange.from_json(search_exchange({"top": 3, "search": "hello"}, [hit])),
        ]
    )
    async with client(transport) as search:
        results = [item async for item in await search.search(search_text="hello", top=3)]
    assert [item["chunkKey"] for item in results] == ["k1"]
    (sent,) = transport.requests
    assert sent.path == SEARCH_PATH
    assert sent.json() == {"search": "hello", "top": 3}


async def test_an_unrecorded_request_fails_the_call() -> None:
    transport = ReplayTransport([Exchange.from_json(search_exchange({"search": "a"}, []))])
    async with client(transport) as search:
        results = await search.search(search_text="b")
        with pytest.raises(ReplayMismatchError, match="POST /indexes"):
            _ = [item async for item in results]
    assert len(transport.requests) == 1


async def test_error_answers_reach_the_sdk_as_its_errors() -> None:
    exchange = {
        "request": {"method": "GET", "path": "/indexes('missing')/docs/$count"},
        "response": {
            "status": 404,
            "json": {"error": {"code": "IndexNotFound", "message": "no index"}},
        },
    }
    transport = ReplayTransport([Exchange.from_json(exchange)])
    async with SearchClient(
        ENDPOINT, "missing", AzureKeyCredential("unused"), transport=transport
    ) as search:
        with pytest.raises(ResourceNotFoundError):
            await search.get_document_count()


def test_exchange_parsing() -> None:
    plain = Exchange.from_json(
        {"request": {"method": "delete", "path": "/x"}, "response": {"status": 204}}
    )
    assert plain.request.method == "DELETE"
    assert plain.response.content == b""
    assert not plain.request.has_json
    text = Exchange.from_json(
        {
            "request": {"method": "GET", "path": "/y", "query": {"n": 1}},
            "response": {"status": 200, "body": "hi", "headers": {"Content-Type": "text/plain"}},
        }
    )
    assert text.request.query == {"n": "1"}
    assert text.response.content == b"hi"
    assert text.response.headers == {"Content-Type": "text/plain"}


def test_files_load_in_name_order(tmp_path: Path) -> None:
    service = tmp_path / "search"
    service.mkdir()
    (service / "b.json").write_text(json.dumps(search_exchange({"search": "b"}, [])))
    (service / "a.json").write_text(
        json.dumps([search_exchange({"search": "a1"}, []), search_exchange({"search": "a2"}, [])])
    )
    exchanges = load_exchanges(service)
    assert [exchange.source for exchange in exchanges] == ["a.json", "a.json", "b.json"]
    assert [exchange.request.json for exchange in exchanges] == [
        {"search": "a1"},
        {"search": "a2"},
        {"search": "b"},
    ]
    assert len(ReplayTransport.for_service("search", root=tmp_path).requests) == 0
    with pytest.raises(FileNotFoundError):
        load_exchanges(tmp_path / "absent")


def test_fixture_root_is_the_repository_directory() -> None:
    assert FIXTURES_HTTP_ROOT.parts[-3:] == ("tests", "fixtures", "http")
    assert (FIXTURES_HTTP_ROOT.parents[2] / "pyproject.toml").is_file()


async def test_transport_lifecycle_is_a_no_op() -> None:
    transport = ReplayTransport([])
    async with transport as entered:
        assert entered is transport
    await transport.open()
    await transport.close()
