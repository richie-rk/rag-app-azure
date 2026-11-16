from __future__ import annotations

import pytest

from docchat_core.clients.openai import (
    OPENAI_SCOPE,
    ClientProfile,
    api_openai_client,
    ingestion_openai_client,
    openai_base_url,
)

_ENDPOINT = "https://oai-docchat-dev-x7.openai.azure.com"


async def _token() -> str:
    return "token"


@pytest.mark.parametrize("endpoint", [_ENDPOINT, _ENDPOINT + "/"])
def test_the_v1_base_url(endpoint: str) -> None:
    assert openai_base_url(endpoint) == _ENDPOINT + "/openai/v1/"


def test_the_token_audience_and_profiles() -> None:
    assert OPENAI_SCOPE == "https://ai.azure.com/.default"
    assert [profile.value for profile in ClientProfile] == ["api", "ingestion"]


async def test_the_api_profile() -> None:
    client = api_openai_client(
        _ENDPOINT, _token, connect_timeout_seconds=10.0, read_timeout_seconds=60.0
    )
    try:
        assert str(client.base_url) == _ENDPOINT + "/openai/v1/"
        assert client.max_retries == 0
        timeout = client.timeout
        assert (timeout.connect, timeout.read, timeout.write, timeout.pool) == (  # type: ignore[union-attr]
            10.0,
            60.0,
            10.0,
            10.0,
        )
    finally:
        await client.close()


async def test_the_api_profile_takes_its_connect_and_read_timeouts_from_the_settings() -> None:
    client = api_openai_client(
        _ENDPOINT, _token, connect_timeout_seconds=3.0, read_timeout_seconds=90.0
    )
    try:
        assert (client.timeout.connect, client.timeout.read) == (3.0, 90.0)  # type: ignore[union-attr]
    finally:
        await client.close()


async def test_t195_the_ingestion_profile() -> None:
    client = ingestion_openai_client(_ENDPOINT, _token)
    try:
        assert str(client.base_url) == _ENDPOINT + "/openai/v1/"
        assert client.max_retries == 3
        timeout = client.timeout
        assert (timeout.connect, timeout.read, timeout.write, timeout.pool) == (  # type: ignore[union-attr]
            5.0,
            60.0,
            60.0,
            60.0,
        )
    finally:
        await client.close()


async def test_a_constant_token_is_sent_as_given() -> None:
    client = ingestion_openai_client("http://127.0.0.1:8091", "local-emulator")
    try:
        assert client.api_key == "local-emulator"
    finally:
        await client.close()
