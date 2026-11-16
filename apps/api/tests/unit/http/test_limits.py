from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import uuid4

import pytest

from docchat_api.http.limits import RequestTooLarge, UnsupportedMediaType, media_type_allowed
from docchat_testing.fixtures.api import ApiUnderTest
from docchat_testing.fixtures.identities import SharedPrincipal

JSON = ("application/json",)


@pytest.mark.parametrize(
    "content_type",
    [
        "application/json",
        "Application/JSON",
        "application/json; charset=utf-8",
        "application/json;charset=UTF-8",
        'application/json; charset="utf-8"',
    ],
)
def test_json_with_no_charset_or_utf8_is_allowed(content_type: str) -> None:
    assert media_type_allowed(content_type, JSON)


@pytest.mark.parametrize(
    "content_type",
    [
        "application/json; charset=latin-1",
        "application/json; boundary=x",
        "application/json; charset",
        "text/plain",
        "application/x-ndjson",
        "application/octet-stream",
        "",
    ],
)
def test_other_json_forms_and_types_are_refused(content_type: str) -> None:
    assert not media_type_allowed(content_type, JSON)


def test_octet_stream_routes_take_octet_stream() -> None:
    allowed = ("application/octet-stream",)

    assert media_type_allowed("application/octet-stream", allowed)
    assert not media_type_allowed("application/json", allowed)


def test_the_problems() -> None:
    assert RequestTooLarge().status == 413
    assert UnsupportedMediaType().status == 415


def _item_url() -> str:
    return f"/v1/probe/items/{uuid4()}"


def _body(size: int) -> bytes:
    """A valid JSON body of exactly `size` bytes."""
    head = b'{"name": "n", "projectId": "' + str(uuid4()).encode() + b'", "pad": "'
    return head + b"x" * (size - len(head) - 2) + b'"}'


async def test_t052_a_70000_byte_body_is_too_large_on_a_default_route(
    started: ApiUnderTest, alice: SharedPrincipal
) -> None:
    response = await started.client.post(
        _item_url(),
        content=_body(70_000),
        headers=alice.authorization() | {"Content-Type": "application/json"},
    )

    assert (response.status_code, response.json()["code"]) == (413, "request_too_large")


async def test_t052_a_40000_byte_body_is_too_large_on_a_32_kib_route(
    started: ApiUnderTest, alice: SharedPrincipal
) -> None:
    response = await started.client.post(
        "/v1/probe/chat",
        content=_body(40_000),
        headers=alice.authorization() | {"Content-Type": "application/json"},
    )

    assert (response.status_code, response.json()["code"]) == (413, "request_too_large")


async def test_a_streamed_body_over_the_limit_is_too_large(
    started: ApiUnderTest, alice: SharedPrincipal
) -> None:
    async def chunks() -> AsyncIterator[bytes]:
        for _ in range(9):
            yield b"x" * 8_192

    response = await started.client.post(
        _item_url(),
        content=chunks(),
        headers=alice.authorization() | {"Content-Type": "application/json"},
    )

    assert (response.status_code, response.json()["code"]) == (413, "request_too_large")


async def test_t052_octet_stream_on_a_json_route_is_unsupported(
    started: ApiUnderTest, alice: SharedPrincipal
) -> None:
    response = await started.client.post(
        _item_url(),
        content=b"\x00\x01",
        headers=alice.authorization() | {"Content-Type": "application/octet-stream"},
    )

    assert (response.status_code, response.json()["code"]) == (415, "unsupported_media_type")


async def test_t052_the_octet_stream_route_has_no_json_limit_and_refuses_json(
    started: ApiUnderTest, alice: SharedPrincipal
) -> None:
    big = await started.client.put(
        "/v1/probe/raw",
        content=b"\x00" * 200_000,
        headers=alice.authorization() | {"Content-Type": "application/octet-stream"},
    )
    json = await started.client.put(
        "/v1/probe/raw",
        content=b"{}",
        headers=alice.authorization() | {"Content-Type": "application/json"},
    )

    assert big.status_code == 200
    assert big.json() == {"size": 200_000}
    assert (json.status_code, json.json()["code"]) == (415, "unsupported_media_type")
    assert json.json()["detail"] == UnsupportedMediaType().detail


async def test_a_body_without_a_content_type_is_unsupported(
    started: ApiUnderTest, alice: SharedPrincipal
) -> None:
    response = await started.client.post(
        _item_url(), content=b'{"name": "n"}', headers=alice.authorization()
    )

    assert (response.status_code, response.json()["code"]) == (415, "unsupported_media_type")


async def test_an_empty_body_is_the_validators_required(
    started: ApiUnderTest, alice: SharedPrincipal
) -> None:
    response = await started.client.post(_item_url(), headers=alice.authorization())

    assert response.status_code == 422
    assert response.json()["errors"] == [{"path": "body", "reason": "required"}]


async def test_invalid_json_and_invalid_utf8_are_invalid_json(
    started: ApiUnderTest, alice: SharedPrincipal
) -> None:
    headers = alice.authorization() | {"Content-Type": "application/json"}
    broken = await started.client.post(_item_url(), content=b'{"name": ', headers=headers)
    latin = await started.client.post(_item_url(), content=b'{"name": "\xff"}', headers=headers)

    for response in (broken, latin):
        assert response.status_code == 422
        assert response.json()["errors"] == [{"path": "body", "reason": "invalid_json"}]
