"""The model stub served by Uvicorn on a random loopback port, in the test's own event loop.

Each test starts a stub with the settings it needs (throttling, delays, recording), so the
stub's own suites don't depend on the settings the compose service runs with. A real server is
needed because the `openai` client brings its own transport, and because `hang`, `cut` and the
disconnect counter only show over a real connection.
"""

from __future__ import annotations

import asyncio
import contextlib
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

import httpx
import pytest
import uvicorn

from docchat_model_stub.app import StubSettings, create_app

# The `openai` client decodes base64 embeddings with NumPy when it can import it, and imports it
# on its first decode: inside a test's coroutine that import holds the event loop for about
# 150 ms (84-testing.md section 2.8). Imported here, at collection, it is already loaded then.
with contextlib.suppress(ImportError):
    import numpy  # noqa: F401

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable
    from contextlib import AbstractAsyncContextManager

FAST: dict[str, Any] = {
    "first_token_delay_ms": 0,
    "token_delay_ms": 0,
    "json_delay_ms": 0,
    "embedding_delay_ms": 0,
}
AUTH = {"Authorization": "Bearer stub-test-token"}

type StartStub = Callable[..., AbstractAsyncContextManager[str]]


@asynccontextmanager
async def _running_stub(**settings: Any) -> AsyncIterator[str]:
    app = create_app(StubSettings(**{**FAST, **settings}))
    config = uvicorn.Config(
        app, host="127.0.0.1", port=0, lifespan="off", log_config=None, access_log=False
    )
    server = uvicorn.Server(config)
    serving = asyncio.create_task(server.serve())
    while not server.started:
        if serving.done():
            await serving
            raise RuntimeError("the stub server stopped before it started")
        await asyncio.sleep(0.01)
    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        await serving


@pytest.fixture
def start_stub() -> StartStub:
    """`async with start_stub(**settings) as base_url:` serves a stub for the block."""
    return _running_stub


def stub_client(base_url: str, *, timeout: float = 10.0) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=base_url,
        headers=AUTH,
        timeout=httpx.Timeout(timeout),
        follow_redirects=False,
        trust_env=False,
    )


@pytest.fixture
def client_for() -> Callable[..., httpx.AsyncClient]:
    """`client_for(base_url)`: an `httpx.AsyncClient` that sends a bearer token to the stub."""
    return stub_client
