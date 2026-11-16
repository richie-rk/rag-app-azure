"""The two `openai.AsyncOpenAI` profiles against the Azure OpenAI v1 API (`01-repository.md`
sections 2.3 and 6.3; `12-chat-pipeline.md` section 10.6; `14-ingestion.md` section 12; ADR 0130).

`httpx2` is the `openai` package's own transport, and this is the only module that imports it,
only to set the profiles' timeouts and connection limits. Both profiles call
`{endpoint}/openai/v1/` with an Entra bearer token from an async provider over the process's one
credential. The API profile retries nothing itself, because the chat pipeline's own rules decide
every retry; the ingestion profile retries three times inside an activity before the
orchestrator's backoff takes over.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from enum import StrEnum
from typing import Final

import httpx2
from openai import AsyncOpenAI

from docchat_core.clients.tls import TLS_CONTEXT

OPENAI_SCOPE: Final = "https://ai.azure.com/.default"
"""The token audience of every Azure OpenAI call."""

API_WRITE_TIMEOUT_SECONDS: Final = 10.0
API_POOL_TIMEOUT_SECONDS: Final = 10.0
API_MAX_CONNECTIONS: Final = 200
API_MAX_KEEPALIVE_CONNECTIONS: Final = 50
INGESTION_TIMEOUT_SECONDS: Final = 60.0
INGESTION_CONNECT_TIMEOUT_SECONDS: Final = 5.0
INGESTION_MAX_RETRIES: Final = 3

type ApiKey = str | Callable[[], Awaitable[str]]
"""A bearer-token provider, or a constant token for the local model stub."""


class ClientProfile(StrEnum):
    """Which deployable a process's clients serve; the options differ per caller."""

    API = "api"
    INGESTION = "ingestion"


def openai_base_url(endpoint: str) -> str:
    """`{endpoint}/openai/v1/` of an account (or model stub) endpoint."""
    return f"{endpoint.rstrip('/')}/openai/v1/"


def api_openai_client(
    endpoint: str,
    api_key: ApiKey,
    *,
    connect_timeout_seconds: float,
    read_timeout_seconds: float,
) -> AsyncOpenAI:
    """The API profile: connect and read from `DOCCHAT_OPENAI_CONNECT_TIMEOUT_SECONDS` and
    `DOCCHAT_OPENAI_READ_TIMEOUT_SECONDS`, write and pool 10 s, no retries, and a pool sized for
    the worker's concurrent streams."""
    return AsyncOpenAI(
        base_url=openai_base_url(endpoint),
        api_key=api_key,
        max_retries=0,
        timeout=httpx2.Timeout(
            connect=connect_timeout_seconds,
            read=read_timeout_seconds,
            write=API_WRITE_TIMEOUT_SECONDS,
            pool=API_POOL_TIMEOUT_SECONDS,
        ),
        http_client=httpx2.AsyncClient(
            limits=httpx2.Limits(
                max_connections=API_MAX_CONNECTIONS,
                max_keepalive_connections=API_MAX_KEEPALIVE_CONNECTIONS,
            ),
            verify=TLS_CONTEXT,
        ),
    )


def ingestion_openai_client(endpoint: str, api_key: ApiKey) -> AsyncOpenAI:
    """The ingestion profile: 60 s for read, write and pool, 5 s to connect, three retries,
    each honouring `retry-after-ms` or `retry-after`."""
    return AsyncOpenAI(
        base_url=openai_base_url(endpoint),
        api_key=api_key,
        max_retries=INGESTION_MAX_RETRIES,
        timeout=httpx2.Timeout(
            INGESTION_TIMEOUT_SECONDS, connect=INGESTION_CONNECT_TIMEOUT_SECONDS
        ),
    )
