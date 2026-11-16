from __future__ import annotations

import pytest
from pydantic import BaseModel, ConfigDict, ValidationError

from docchat_core.settings.endpoints import (
    ENDPOINT_SETTINGS,
    EndpointUrl,
    is_insecure_endpoint,
    is_loopback_host,
)


class _Target(BaseModel):
    model_config = ConfigDict(extra="forbid")

    endpoint: EndpointUrl


def test_endpoint_settings_are_the_eight_of_check_3() -> None:
    assert set(ENDPOINT_SETTINGS) == {
        "DOCCHAT_SQL_SERVER",
        "DOCCHAT_COSMOS_ENDPOINT",
        "DOCCHAT_STORAGE_BLOB_ENDPOINT",
        "DOCCHAT_STORAGE_QUEUE_ENDPOINT",
        "DOCCHAT_SEARCH_ENDPOINT",
        "DOCCHAT_OPENAI_ENDPOINT",
        "DOCCHAT_CONTENTSAFETY_ENDPOINT",
        "DOCCHAT_DOCINTEL_ENDPOINT",
    }
    assert len(ENDPOINT_SETTINGS) == 8


@pytest.mark.parametrize(
    "url",
    [
        "https://srch-docchat-dev-abc.search.windows.net",
        "https://stdocchatdatadevabc.blob.core.windows.net/",
        "https://cosmos-docchat-dev-abc.documents.azure.com:443/",
        "https://aoai.openai.azure.com",
        "http://127.0.0.1:8091",
        "HTTPS://Example.com/base/",
    ],
)
def test_endpoint_url_keeps_a_base_url_as_configured(url: str) -> None:
    assert _Target(endpoint=url).endpoint == url


@pytest.mark.parametrize(
    "url",
    [
        "srch.search.windows.net",
        "ftp://example.com",
        "https://",
        "https://user:secret@example.com",
        "https://example.com/?sig=x",
        "https://example.com/?",
        "https://example.com/#part",
        "https://example.com:99999",
        "https://example.com:port",
        " https://example.com",
        "https://exa mple.com",
        "https://example.com\n",
        "",
    ],
)
def test_endpoint_url_refuses_anything_else(url: str) -> None:
    with pytest.raises(ValidationError) as caught:
        _Target(endpoint=url)

    assert caught.value.errors()[0]["type"] == "url_parsing"


def test_endpoint_url_refuses_a_non_string() -> None:
    with pytest.raises(ValidationError):
        _Target(endpoint=443)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "host",
    [
        "localhost",
        "LOCALHOST",
        "localhost.",
        "api.localhost",
        "127.0.0.1",
        "127.10.20.30",
        "127.1",
        "0x7f000001",
        "2130706433",
        "::1",
        "[::1]",
        "0:0:0:0:0:0:0:1",
        "::ffff:127.0.0.1",
        "0.0.0.0",  # noqa: S104 - the refused bind-all address
        "::",
    ],
)
def test_loopback_hosts(host: str) -> None:
    assert is_loopback_host(host) is True


@pytest.mark.parametrize(
    "host",
    [
        "example.com",
        "localhost.example.com",
        "mylocalhost",
        "10.0.0.1",
        "128.0.0.1",
        "::2",
        "2001:db8::1",
        "sql-docchat-dev-abc.database.windows.net",
    ],
)
def test_remote_hosts(host: str) -> None:
    assert is_loopback_host(host) is False


@pytest.mark.parametrize(
    ("value", "insecure"),
    [
        ("https://srch-docchat-dev-abc.search.windows.net", False),
        ("https://cosmos-docchat-dev-abc.documents.azure.com:443/", False),
        ("http://srch-docchat-dev-abc.search.windows.net", True),
        ("http://127.0.0.1:9", True),
        ("https://127.0.0.1:9", True),
        ("https://localhost/", True),
        ("https://[::1]:443/", True),
        ("ftp://example.com", True),
        ("https://", True),
        ("https://[::1", True),
        ("sql-docchat-dev-abc.database.windows.net", False),
        ("tcp:sql-docchat-dev-abc.database.windows.net,1433", False),
        ("127.0.0.1", True),
        ("tcp:127.0.0.1,14330", True),
        ("localhost:1433", True),
        ("[::1],1433", True),
        ("::1", True),
        ("", True),
    ],
)
def test_is_insecure_endpoint(value: str, insecure: bool) -> None:
    assert is_insecure_endpoint(value) is insecure
