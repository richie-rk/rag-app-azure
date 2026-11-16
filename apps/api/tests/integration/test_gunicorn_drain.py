"""T-066, the Gunicorn drain integration test (`11-api.md` section 1.15 and acceptance criterion
24; `12-chat-pipeline.md` section 14.8; ADR 0007).

Gunicorn runs in a subprocess with the API image's own configuration, `apps/api/gunicorn.conf.py`
(its settings load, its start-up checks and `DocChatUvicornWorker`), bound to a loopback port and
serving a probe application instead of the API, because the API's lifespan would need every
service. The probe streams NDJSON and wraps `docchat_api.chat.tasks.begin_drain`, the function the
worker schedules on its loop at the first SIGTERM, so the open stream reports the moment the drain
is delivered. SIGTERM goes to the master; the drain must reach the worker within 1 s while the
stream is still open, and once the stream ends the master must exit with code 0 within 10 s.

`gunicorn.conf.py` loads both encodings from `TIKTOKEN_CACHE_DIR` before it forks (start-up check
8); where they can't be loaded the master would exit 78, so the test skips with that reason.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import socket
import sys
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import httpx
import pytest

from docchat_api.settings import ApiSettings, check_encodings

ROOT: Final = next(path for path in Path(__file__).resolve().parents if (path / "apps").is_dir())
CONFIG: Final = ROOT / "apps" / "api" / "gunicorn.conf.py"
DRAIN_SECONDS: Final = 1.0
EXIT_SECONDS: Final = 10.0
START_SECONDS: Final = 30.0

ENVIRONMENT: Final = {
    "DOCCHAT_CORE_ENVIRONMENT": "dev",
    "DOCCHAT_CORE_IDENTITY_CLIENT_ID": "6f1d3c2a-7b8e-4c9d-a0b1-c2d3e4f5a6b7",
    "DOCCHAT_CORE_WORKLOAD_PRINCIPAL_ID": "7c9e6679-7425-40de-944b-e07fc1f90ae7",
    "DOCCHAT_AUTH_TENANT_ID": "5b0c2c8e-7a51-4c3e-9d6f-1f2a3b4c5d6e",
    "DOCCHAT_AUTH_API_CLIENT_ID": "8e3f1a52-4c6d-4b7e-a9f0-2c1d3e4f5a6b",
    "DOCCHAT_AUTH_WEB_CLIENT_ID": "1d2e3f4a-5b6c-4d7e-8f90-a1b2c3d4e5f6",
    "DOCCHAT_AUTH_TEST_CLIENT_ID": "9a8b7c6d-5e4f-4a3b-b2c1-d0e9f8a7b6c5",
    "DOCCHAT_BUILD_COMMIT": "0123456789abcdef0123456789abcdef01234567",
    "DOCCHAT_SQL_SERVER": "sql-docchat-dev-abc123.database.windows.net",
    "DOCCHAT_STORAGE_BLOB_ENDPOINT": "https://stdocchatdatadevabc123.blob.core.windows.net/",
    "DOCCHAT_STORAGE_QUEUE_ENDPOINT": "https://stdocchatdatadevabc123.queue.core.windows.net/",
    "DOCCHAT_STORAGE_MALWARE_SCANNING_ENABLED": "false",
    "DOCCHAT_SEARCH_MAX_INDEXES": "15",
    "DOCCHAT_SEARCH_ENDPOINT": "https://srch-docchat-dev.search.windows.net",
    "DOCCHAT_OPENAI_ENDPOINT": "https://oai-docchat-dev.openai.azure.com",
    "DOCCHAT_COSMOS_ENDPOINT": "https://cosmos-docchat-dev.documents.azure.com:443/",
    "DOCCHAT_CONTENTSAFETY_ENDPOINT": "https://cs-docchat-dev.cognitiveservices.azure.com",
    "DOCCHAT_API_WORKERS": "1",
}
"""A deployed API's settings in `dev`: what `gunicorn.conf.py` loads before it forks."""

PROBE: Final = '''
"""A probe application for the drain test: one NDJSON stream that reports the drain."""

import asyncio
import json
import os

from docchat_api.chat import tasks

RELEASE = os.environ["DRAIN_PROBE_RELEASE"]
_drained = asyncio.Event()
_begin_drain = tasks.begin_drain


def begin_drain():
    _begin_drain()
    _drained.set()


# The worker looks `begin_drain` up on this module when the signal arrives.
tasks.begin_drain = begin_drain


def _line(event):
    return {"type": "http.response.body", "body": json.dumps(event).encode() + b"\\n",
            "more_body": True}


async def app(scope, receive, send):
    if scope["type"] == "lifespan":
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                await send({"type": "lifespan.startup.complete"})
            elif message["type"] == "lifespan.shutdown":
                await send({"type": "lifespan.shutdown.complete"})
                return
    headers = [(b"content-type", b"application/x-ndjson")]
    await send({"type": "http.response.start", "status": 200, "headers": headers})
    if scope["path"] != "/stream":
        await send({"type": "http.response.body", "body": b""})
        return
    await send(_line({"type": "meta"}))
    await _drained.wait()
    await send(_line({"type": "drained"}))
    while not os.path.exists(RELEASE):
        await asyncio.sleep(0.05)
    await send({"type": "http.response.body", "body": b'{"type":"done"}\\n'})
'''


@pytest.fixture(scope="module")
def encodings_load() -> None:
    try:
        check_encodings(_settings_for_check())
    except Exception as failure:  # noqa: BLE001 - any loading failure means no encoding here
        pytest.skip(f"the tiktoken encodings can't be loaded here: {type(failure).__name__}")


def _settings_for_check() -> ApiSettings:

    return ApiSettings.model_validate(
        {
            "cosmos_endpoint": ENVIRONMENT["DOCCHAT_COSMOS_ENDPOINT"],
            "search_endpoint": ENVIRONMENT["DOCCHAT_SEARCH_ENDPOINT"],
            "openai_endpoint": ENVIRONMENT["DOCCHAT_OPENAI_ENDPOINT"],
            "contentsafety_endpoint": ENVIRONMENT["DOCCHAT_CONTENTSAFETY_ENDPOINT"],
            "api_workers": 1,
        }
    )


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@dataclass(frozen=True)
class Served:
    master: asyncio.subprocess.Process
    base_url: str
    release: Path


@pytest.fixture
def probe_dir(tmp_path: Path) -> Path:
    (tmp_path / "drain_probe.py").write_text(PROBE, encoding="utf-8")
    return tmp_path


@pytest.fixture
async def served(encodings_load: None, probe_dir: Path) -> AsyncIterator[Served]:
    port = free_port()
    release = probe_dir / "release"
    environment = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith(("DOCCHAT_", "WEBSITE_"))
    }
    environment |= ENVIRONMENT | {"DRAIN_PROBE_RELEASE": str(release)}
    master = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "gunicorn",
        "--config",
        str(CONFIG),
        "--bind",
        f"127.0.0.1:{port}",
        "--chdir",
        str(probe_dir),
        "drain_probe:app",
        env=environment,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    base_url = f"http://127.0.0.1:{port}"
    try:
        await _ready(master, base_url)
        yield Served(master=master, base_url=base_url, release=release)
    finally:
        if master.returncode is None:
            master.kill()
            await master.wait()


async def _ready(master: asyncio.subprocess.Process, base_url: str) -> None:
    async with httpx.AsyncClient(base_url=base_url, timeout=2.0) as client:
        deadline = time.monotonic() + START_SECONDS
        while time.monotonic() < deadline:
            if master.returncode is not None:
                stderr = b"" if master.stderr is None else await master.stderr.read()
                pytest.fail(f"gunicorn exited {master.returncode}: {stderr.decode()[-2000:]}")
            try:
                if (await client.get("/ready")).status_code == httpx.codes.OK:
                    return
            except httpx.TransportError:
                pass
            await asyncio.sleep(0.2)
    pytest.fail("gunicorn didn't serve within 30 s")


async def test_t066_sigterm_drains_the_open_stream_and_the_master_exits_0(
    served: Served,
) -> None:
    arrived: dict[str, float] = {}
    async with (
        httpx.AsyncClient(base_url=served.base_url, timeout=30.0) as client,
        client.stream("GET", "/stream") as response,
    ):
        lines = response.aiter_lines()
        assert json.loads(await anext(lines))["type"] == "meta"
        signalled = time.monotonic()
        os.kill(served.master.pid, signal.SIGTERM)
        drained = json.loads(await asyncio.wait_for(anext(lines), DRAIN_SECONDS + 1))
        arrived["drained"] = time.monotonic()
        assert served.master.returncode is None
        await asyncio.to_thread(served.release.touch)
        ended = time.monotonic()
        done = json.loads(await anext(lines))

    exit_code = await asyncio.wait_for(served.master.wait(), EXIT_SECONDS)

    assert drained["type"] == "drained"
    assert arrived["drained"] - signalled <= DRAIN_SECONDS
    assert done["type"] == "done"
    assert exit_code == 0
    assert time.monotonic() - ended <= EXIT_SECONDS
