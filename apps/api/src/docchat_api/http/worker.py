"""The Gunicorn worker class and the Uvicorn server it runs (`11-api.md` sections 1.10 and 1.15;
`12-chat-pipeline.md` section 14.8; ADR 0007).

`DocChatUvicornWorker` subclasses uvicorn-worker's `UvicornWorker`: no `Server` header and no
access log (request lines would carry search text and identifiers into logs; request telemetry
comes from spans), and a 65-second keep-alive, above the 60-second idle timeout common to HTTP
proxies, so the front end never reuses a connection the server is closing. Its `_serve()` is
uvicorn-worker 0.4.0's with `DocChatServer` in place of `uvicorn.Server`.

`DocChatServer.handle_exit` is the handler Uvicorn installs for SIGINT and SIGTERM. On the first
SIGTERM it schedules `docchat_api.chat.tasks.begin_drain` on the worker's loop with
`call_soon_threadsafe`, because a signal handler interrupts the loop's own thread between
bytecodes, where `call_soon` isn't safe; the drain therefore starts within milliseconds while the
streams are still open. Then Uvicorn's own shutdown proceeds unchanged. `begin_drain` is looked up
in `sys.modules` when the signal arrives: importing inside a signal handler could deadlock on the
import lock, and a process that never loaded the chat task tracker has no turn to drain.
"""

from __future__ import annotations

import asyncio
import signal
import socket
import sys
from collections.abc import Callable
from types import FrameType
from typing import Any, Final, override

import uvicorn
from gunicorn.arbiter import Arbiter
from uvicorn_worker import UvicornWorker

DRAIN_MODULE: Final = "docchat_api.chat.tasks"
DRAIN_FUNCTION: Final = "begin_drain"
KEEP_ALIVE_SECONDS: Final = 65


class DocChatServer(uvicorn.Server):
    """`uvicorn.Server` that starts the chat drain on the first SIGTERM."""

    def __init__(self, config: uvicorn.Config) -> None:
        super().__init__(config)
        self.loop: asyncio.AbstractEventLoop | None = None
        self._drain_scheduled = False

    @override
    async def serve(self, sockets: list[socket.socket] | None = None) -> None:
        self.loop = asyncio.get_running_loop()
        await super().serve(sockets=sockets)

    @override
    def handle_exit(self, sig: int, frame: FrameType | None) -> None:
        if sig == signal.SIGTERM and not self._drain_scheduled and self.loop is not None:
            self._drain_scheduled = True
            drain = _drain_function()
            if drain is not None:
                self.loop.call_soon_threadsafe(drain)
        super().handle_exit(sig, frame)


def _drain_function() -> Callable[[], None] | None:
    module = sys.modules.get(DRAIN_MODULE)
    drain = getattr(module, DRAIN_FUNCTION, None)
    return drain if callable(drain) else None


class DocChatUvicornWorker(UvicornWorker):
    """The `worker_class` of `gunicorn.conf.py`."""

    # The base class declares this as a plain class attribute, which a ClassVar can't override.
    CONFIG_KWARGS: dict[str, Any] = {  # noqa: RUF012
        **UvicornWorker.CONFIG_KWARGS,
        "server_header": False,
        "access_log": False,
        "timeout_keep_alive": KEEP_ALIVE_SECONDS,
    }

    @override
    async def _serve(self) -> None:
        # uvicorn-worker 0.4.0's `_serve`, with DocChatServer for uvicorn.Server.
        self.config.app = self.wsgi
        server = DocChatServer(config=self.config)
        self._install_sigquit_handler()
        await server.serve(sockets=self.sockets)
        if not server.started:
            sys.exit(Arbiter.WORKER_BOOT_ERROR)
