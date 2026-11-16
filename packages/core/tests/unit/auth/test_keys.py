"""Unit tests of the signing-key providers (T-008; `11-api.md` section 1.2 "Signing keys")."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable, Iterator
from typing import Any
from uuid import UUID

import httpx
import jwt
import pytest
import respx

from docchat_core.auth import keys
from docchat_core.auth.exceptions import AuthenticationUnavailable, InvalidToken
from docchat_core.auth.keys import (
    EntraSigningKeyProvider,
    NotAKeySetError,
    NoUsableKeyError,
    StaticSigningKeyProvider,
    jwks_uri,
    parse_key_set,
)
from docchat_testing.clock import FakeClock
from docchat_testing.tokens import SigningKeys

TENANT = UUID("5b0c2c8e-7a51-4c3e-9d6f-1f2a3b4c5d6e")
URL = f"https://login.microsoftonline.com/{TENANT}/discovery/v2.0/keys"


@pytest.fixture(scope="module")
def rolled() -> SigningKeys:
    """A second key pair, the key an emergency roll publishes; generated outside the loop."""
    return SigningKeys()


def _key_set(*pairs: SigningKeys) -> dict[str, Any]:
    return {"keys": [pair.jwks()["keys"][0] for pair in pairs]}


def _ok(*pairs: SigningKeys) -> httpx.Response:
    return httpx.Response(200, json=_key_set(*pairs))


@pytest.fixture
def router() -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_called=False) as mock:
        yield mock


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
async def entra(clock: FakeClock) -> AsyncIterator[EntraSigningKeyProvider]:
    provider = EntraSigningKeyProvider(TENANT, clock=clock)
    try:
        yield provider
    finally:
        await provider.aclose()


async def _until(predicate: Callable[[], bool]) -> None:
    for _ in range(200):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("the condition never held")


# --- The URL and the client ------------------------------------------------------------------


def test_the_url_is_built_from_the_tenant_alone() -> None:
    assert jwks_uri(TENANT) == URL


async def test_the_client_has_its_timeouts_and_follows_no_redirect(
    entra: EntraSigningKeyProvider,
) -> None:
    assert entra.url == URL
    assert entra.client.timeout == httpx.Timeout(8.0, connect=4.0)
    assert entra.client.follow_redirects is False
    assert "valid=False" in repr(entra)


# --- Parsing ----------------------------------------------------------------------------------


def test_a_key_set_becomes_rs256_keys(signing_keys: SigningKeys, rolled: SigningKeys) -> None:
    parsed = parse_key_set(json.dumps(_key_set(signing_keys, rolled)).encode())

    assert set(parsed) == {signing_keys.kid, rolled.kid}
    assert all(isinstance(key, jwt.PyJWK) for key in parsed.values())
    assert parsed[signing_keys.kid].algorithm_name == "RS256"


def test_unusable_keys_are_skipped(signing_keys: SigningKeys, rolled: SigningKeys) -> None:
    good = signing_keys.jwks()["keys"][0]
    entries = [
        good | {"kid": "ec", "kty": "EC"},
        good | {"kid": "enc", "use": "enc"},
        {key: value for key, value in good.items() if key != "kid"},
        good | {"kid": ""},
        good | {"kid": "broken", "n": "!!"},
        "not a key",
        rolled.jwks()["keys"][0] | {"use": None},
        good,
        good | {"n": rolled.jwks()["keys"][0]["n"]},
    ]
    parsed = parse_key_set(json.dumps({"keys": entries}).encode())

    # The duplicate kid keeps its first key; `use` absent (here null) is accepted.
    assert set(parsed) == {rolled.kid, signing_keys.kid}


def test_the_first_hundred_usable_keys_are_kept(signing_keys: SigningKeys) -> None:
    good = signing_keys.jwks()["keys"][0]
    entries = [good | {"kid": f"k{n:03d}"} for n in range(120)]

    parsed = parse_key_set(json.dumps({"keys": entries}).encode())

    assert len(parsed) == 100
    assert "k099" in parsed
    assert "k100" not in parsed


@pytest.mark.parametrize(
    "body", [b"not json", b"[1, 2]", b'{"value": []}', b'{"keys": {}}', b"\xff\xfe"]
)
def test_a_document_without_a_keys_array_is_no_key_set(body: bytes) -> None:
    with pytest.raises(NotAKeySetError):
        parse_key_set(body)


@pytest.mark.parametrize("entries", [[], [{"kty": "oct", "kid": "a", "k": "c2VjcmV0"}]])
def test_a_key_set_without_a_usable_key_is_refused(entries: list[dict[str, str]]) -> None:
    with pytest.raises(NoUsableKeyError):
        parse_key_set(json.dumps({"keys": entries}).encode())


# --- The static provider ------------------------------------------------------------------------


async def test_the_static_provider_serves_its_keys(signing_keys: SigningKeys) -> None:
    provider = StaticSigningKeyProvider(signing_keys.public_keys())
    provider.start()

    assert (await provider.get_key(signing_keys.kid)).key_id == signing_keys.kid
    with pytest.raises(InvalidToken) as caught:
        await provider.get_key("unknown")
    assert caught.value.reason == "unknown_kid"
    await provider.aclose()


# --- The Entra provider -------------------------------------------------------------------------


async def test_t008_the_start_up_fetch_brings_the_key_set(
    router: respx.MockRouter, entra: EntraSigningKeyProvider, signing_keys: SigningKeys
) -> None:
    route = router.get(URL).mock(return_value=_ok(signing_keys))

    await entra.refresh("startup")

    assert route.call_count == 1
    assert entra.valid()
    assert (await entra.get_key(signing_keys.kid)).key_id == signing_keys.kid
    assert route.call_count == 1


async def test_t008_an_unknown_kid_fetches_once_per_300_seconds(
    router: respx.MockRouter,
    entra: EntraSigningKeyProvider,
    clock: FakeClock,
    signing_keys: SigningKeys,
) -> None:
    route = router.get(URL).mock(return_value=_ok(signing_keys))
    await entra.refresh("startup")

    with pytest.raises(InvalidToken) as first:
        await entra.get_key("unknown")
    assert first.value.reason == "unknown_kid"
    assert route.call_count == 2

    clock.advance(299)
    with pytest.raises(InvalidToken):
        await entra.get_key("unknown")
    assert route.call_count == 2

    clock.advance(1)
    with pytest.raises(InvalidToken):
        await entra.get_key("unknown")
    assert route.call_count == 3


async def test_t008_a_rolled_key_is_found_by_the_on_demand_fetch(
    router: respx.MockRouter,
    entra: EntraSigningKeyProvider,
    signing_keys: SigningKeys,
    rolled: SigningKeys,
) -> None:
    router.get(URL).mock(side_effect=[_ok(signing_keys), _ok(signing_keys, rolled)])
    await entra.refresh("startup")

    assert (await entra.get_key(rolled.kid)).key_id == rolled.kid


async def test_t008_fifty_concurrent_requests_share_one_fetch(
    router: respx.MockRouter,
    entra: EntraSigningKeyProvider,
    signing_keys: SigningKeys,
    rolled: SigningKeys,
) -> None:
    route = router.get(URL).mock(side_effect=[_ok(signing_keys), _ok(signing_keys, rolled)])
    await entra.refresh("startup")

    found = await asyncio.gather(*(entra.get_key(rolled.kid) for _ in range(50)))

    assert {key.key_id for key in found} == {rolled.kid}
    assert route.call_count == 2


async def test_t008_requests_during_the_start_up_fetch_wait_for_it(
    router: respx.MockRouter, entra: EntraSigningKeyProvider, signing_keys: SigningKeys
) -> None:
    release = asyncio.Event()
    started: list[httpx.Request] = []

    async def slow_response(request: httpx.Request) -> httpx.Response:
        started.append(request)
        await release.wait()
        return _ok(signing_keys)

    route = router.get(URL).mock(side_effect=slow_response)
    entra.start()
    await _until(lambda: len(started) == 1)
    waiting = [asyncio.create_task(entra.get_key(signing_keys.kid)) for _ in range(5)]
    await asyncio.sleep(0)
    release.set()

    found = await asyncio.gather(*waiting)

    assert {key.key_id for key in found} == {signing_keys.kid}
    assert route.call_count == 1


async def test_t008_the_background_refresh_runs_hourly_and_stops_at_shutdown(
    router: respx.MockRouter, clock: FakeClock, signing_keys: SigningKeys
) -> None:
    waits: list[float] = []
    gate: asyncio.Queue[None] = asyncio.Queue()

    async def sleep(seconds: float) -> None:
        waits.append(seconds)
        await gate.get()
        clock.advance(seconds)

    route = router.get(URL).mock(return_value=_ok(signing_keys))
    provider = EntraSigningKeyProvider(TENANT, clock=clock, sleep=sleep)
    provider.start()
    provider.start()
    await _until(lambda: route.call_count == 1 and len(waits) == 1)
    assert provider.age_seconds() == 0

    gate.put_nowait(None)
    await _until(lambda: route.call_count == 2 and len(waits) == 2)
    assert waits == [3600, 3600]
    assert provider.age_seconds() == 0

    await provider.aclose()
    gate.put_nowait(None)
    await asyncio.sleep(0)
    assert route.call_count == 2


async def test_t008_a_failed_hourly_refresh_keeps_serving_until_24_hours(
    router: respx.MockRouter,
    entra: EntraSigningKeyProvider,
    clock: FakeClock,
    signing_keys: SigningKeys,
) -> None:
    route = router.get(URL).mock(
        side_effect=[_ok(signing_keys), httpx.Response(500), httpx.Response(500)]
    )
    await entra.refresh("startup")

    clock.advance(23 * 3600)
    await entra.refresh("scheduled")
    assert (await entra.get_key(signing_keys.kid)).key_id == signing_keys.kid

    clock.advance(3600)
    assert not entra.valid()
    with pytest.raises(AuthenticationUnavailable) as caught:
        await entra.get_key(signing_keys.kid)
    assert caught.value.retry_after_seconds == 30
    assert caught.value.status == 503
    assert route.call_count == 3


async def test_t008_without_a_valid_set_an_on_demand_fetch_waits_30_seconds(
    router: respx.MockRouter,
    entra: EntraSigningKeyProvider,
    clock: FakeClock,
    signing_keys: SigningKeys,
) -> None:
    route = router.get(URL).mock(
        side_effect=[
            httpx.ConnectError("refused"),
            httpx.ConnectError("refused"),
            _ok(signing_keys),
        ]
    )
    await entra.refresh("startup")

    with pytest.raises(AuthenticationUnavailable):
        await entra.get_key(signing_keys.kid)
    assert route.call_count == 2

    clock.advance(29)
    with pytest.raises(AuthenticationUnavailable):
        await entra.get_key(signing_keys.kid)
    assert route.call_count == 2

    clock.advance(1)
    assert (await entra.get_key(signing_keys.kid)).key_id == signing_keys.kid
    assert route.call_count == 3


async def _oversized_stream() -> AsyncIterator[bytes]:
    chunk = b" " * 65_536
    for _ in range(17):
        yield chunk


@pytest.mark.parametrize(
    "refused",
    [
        pytest.param(
            lambda: httpx.Response(200, content=b" " * (keys.MAX_RESPONSE_BYTES + 1)),
            id="declared-oversize",
        ),
        pytest.param(lambda: httpx.Response(200, content=_oversized_stream()), id="streamed"),
        pytest.param(lambda: httpx.Response(200, json={"keys": []}), id="keyless"),
        pytest.param(lambda: httpx.Response(200, json={"value": "x"}), id="no-key-set"),
        pytest.param(lambda: httpx.Response(200, text="<html>"), id="not-json"),
        pytest.param(lambda: httpx.Response(302, headers={"location": URL}), id="redirect"),
    ],
)
async def test_t008_a_refused_response_keeps_the_last_set(
    router: respx.MockRouter,
    entra: EntraSigningKeyProvider,
    clock: FakeClock,
    signing_keys: SigningKeys,
    refused: Callable[[], httpx.Response],
) -> None:
    route = router.get(URL).mock(side_effect=[_ok(signing_keys), refused()])
    await entra.refresh("startup")
    clock.advance(600)

    await entra.refresh("scheduled")

    assert route.call_count == 2
    assert entra.age_seconds() == 600
    assert (await entra.get_key(signing_keys.kid)).key_id == signing_keys.kid


async def test_a_fetch_is_bounded_by_its_timeout(
    router: respx.MockRouter,
    entra: EntraSigningKeyProvider,
    monkeypatch: pytest.MonkeyPatch,
    captured: Any,
) -> None:
    async def never(request: httpx.Request) -> httpx.Response:
        del request
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    monkeypatch.setattr(keys, "FETCH_TIMEOUT_SECONDS", 0.01)
    router.get(URL).mock(side_effect=never)

    await entra.refresh("startup")

    assert not entra.valid()
    assert captured.events("signing_keys_refresh_failed") == [
        {"trigger": "startup", "statusCode": 0, "errorType": "builtins.TimeoutError"}
    ]


async def test_each_fetch_is_traced_and_counted(
    router: respx.MockRouter,
    entra: EntraSigningKeyProvider,
    captured: Any,
    signing_keys: SigningKeys,
    rolled: SigningKeys,
) -> None:
    router.get(URL).mock(side_effect=[_ok(signing_keys, rolled), httpx.Response(503)])

    await entra.refresh("startup")
    with pytest.raises(InvalidToken):
        await entra.get_key("unknown")

    assert captured.span_attributes("docchat.auth.fetch_signing_keys") == [
        {
            "docchat.auth.trigger": "startup",
            "docchat.auth.outcome": "ok",
            "docchat.auth.key_count": 2,
            "http.response.status_code": 200,
        },
        {
            "docchat.auth.trigger": "unknown_kid",
            "docchat.auth.outcome": "error",
            "http.response.status_code": 503,
        },
    ]
    assert captured.points("docchat.auth.signing_key_fetches") == {
        (("outcome", "ok"), ("trigger", "startup")): 1,
        (("outcome", "error"), ("trigger", "unknown_kid")): 1,
    }
    (record,) = captured.records("signing_keys_fetch_failed")
    assert record.__dict__["error.type"] == "docchat_core.auth.keys.UnexpectedStatusError"
    assert record.__dict__["http.response.status_code"] == 503


async def test_a_connection_failure_names_its_type(
    router: respx.MockRouter, entra: EntraSigningKeyProvider, captured: Any
) -> None:
    router.get(URL).mock(side_effect=httpx.ConnectError("refused"))

    await entra.refresh("scheduled")

    assert captured.events("signing_keys_refresh_failed") == [
        {"trigger": "scheduled", "statusCode": 0, "errorType": "httpx.ConnectError"}
    ]


async def test_the_key_age_gauge_follows_the_set(
    router: respx.MockRouter, clock: FakeClock, captured: Any, signing_keys: SigningKeys
) -> None:
    router.get(URL).mock(return_value=_ok(signing_keys))
    provider = EntraSigningKeyProvider(TENANT, clock=clock)
    try:
        assert captured.points("docchat.auth.signing_key_age") == {}
        await provider.refresh("startup")
        clock.advance(90)
        assert captured.points("docchat.auth.signing_key_age") == {(): 90.0}
    finally:
        await provider.aclose()
    assert provider.client.is_closed
