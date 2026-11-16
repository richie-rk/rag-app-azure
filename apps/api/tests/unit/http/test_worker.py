from __future__ import annotations

import asyncio
import signal
import sys
from types import SimpleNamespace
from typing import Any

import pytest
import uvicorn
from gunicorn.arbiter import Arbiter
from uvicorn_worker import UvicornWorker

from docchat_api.http.worker import DRAIN_MODULE, DocChatServer, DocChatUvicornWorker


async def _app(scope: Any, receive: Any, send: Any) -> None:
    return


@pytest.fixture
def server() -> DocChatServer:
    return DocChatServer(uvicorn.Config(_app))


@pytest.fixture
def drains(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """`begin_drain` replaced by a recorder, on the module the server looks it up on."""
    calls: list[str] = []
    module = SimpleNamespace(begin_drain=lambda: calls.append("drain"))
    monkeypatch.setitem(sys.modules, DRAIN_MODULE, module)
    return calls


async def test_t065_the_first_sigterm_schedules_one_drain_on_the_loop(
    server: DocChatServer, drains: list[str]
) -> None:
    server.loop = asyncio.get_running_loop()

    server.handle_exit(signal.SIGTERM, None)

    assert drains == []
    assert server.should_exit
    await asyncio.sleep(0)
    assert drains == ["drain"]

    server.handle_exit(signal.SIGTERM, None)
    await asyncio.sleep(0)
    assert drains == ["drain"]


async def test_t065_sigint_schedules_no_drain(server: DocChatServer, drains: list[str]) -> None:
    server.loop = asyncio.get_running_loop()

    server.handle_exit(signal.SIGINT, None)
    await asyncio.sleep(0)

    assert drains == []
    assert server.should_exit


async def test_without_the_chat_tracker_nothing_is_drained(
    server: DocChatServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delitem(sys.modules, DRAIN_MODULE, raising=False)
    server.loop = asyncio.get_running_loop()

    server.handle_exit(signal.SIGTERM, None)
    await asyncio.sleep(0)

    assert server.should_exit


def test_before_serving_a_signal_only_stops(server: DocChatServer, drains: list[str]) -> None:
    server.handle_exit(signal.SIGTERM, None)

    assert server.should_exit
    assert drains == []


async def test_serve_keeps_the_running_loop(
    server: DocChatServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def base_serve(self: uvicorn.Server, sockets: Any = None) -> None:
        return

    monkeypatch.setattr(uvicorn.Server, "serve", base_serve)

    await server.serve()

    assert server.loop is asyncio.get_running_loop()


def test_the_worker_turns_off_the_server_header_and_access_log() -> None:
    assert issubclass(DocChatUvicornWorker, UvicornWorker)
    assert DocChatUvicornWorker.CONFIG_KWARGS["server_header"] is False
    assert DocChatUvicornWorker.CONFIG_KWARGS["access_log"] is False
    assert DocChatUvicornWorker.CONFIG_KWARGS["timeout_keep_alive"] == 65


async def test_the_worker_serves_with_the_docchat_server(monkeypatch: pytest.MonkeyPatch) -> None:
    served: list[object] = []

    async def serve(self: DocChatServer, sockets: Any = None) -> None:
        served.append(self)
        self.started = True

    monkeypatch.setattr(DocChatServer, "serve", serve)
    fake: Any = SimpleNamespace(
        config=uvicorn.Config(_app),
        wsgi=_app,
        sockets=[],
        _install_sigquit_handler=lambda: None,
    )

    await DocChatUvicornWorker._serve(fake)

    (server,) = served
    assert isinstance(server, DocChatServer)
    assert fake.config.app is _app


async def test_a_worker_that_never_started_exits_with_the_boot_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def serve(self: DocChatServer, sockets: Any = None) -> None:
        return

    monkeypatch.setattr(DocChatServer, "serve", serve)
    fake: Any = SimpleNamespace(
        config=uvicorn.Config(_app), wsgi=_app, sockets=[], _install_sigquit_handler=lambda: None
    )

    with pytest.raises(SystemExit) as exited:
        await DocChatUvicornWorker._serve(fake)

    assert exited.value.code == Arbiter.WORKER_BOOT_ERROR
