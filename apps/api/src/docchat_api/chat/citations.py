"""The streaming file response of the three file operations and what they share
(`12-chat-pipeline.md` sections 14.8, 16.3 to 16.6; `11-api.md` sections 1.10, 1.11, 3.5.2, 3.5.3
and 3.11.10; `82-security.md` SEC-54, SEC-60 and SEC-94; ADR 0148).

`getCitationContent`, `getCitationDocument` and `adminGetDocumentPdf` serve verified renditions
from the container `renditions` only, never an uploaded source or a converted PDF of `documents`,
and every blob path comes from `docchat_core.ingestion.names`. What they share:

- the download limits (`hold_download`): the caller's download window, then one of their download
  leases, in their own `usage` partition, failing closed (`429 rate_limited`,
  `429 too_many_downloads`, `503 dependency_unavailable`); the 300-second duration cap counts
  from the moment the lease is taken;
- the blob (`open_rendition`): `BlobClient.download_blob()` reads the first 1 MiB and the blob's
  size before any header goes out, so a missing blob still gets its problem answer;
- `CitationFileResponse`, a Starlette `StreamingResponse` whose body is the blob's 1 MiB chunks,
  written in 64 KiB pieces, each send bounded by 30 seconds and the whole download by the lease's
  300 seconds; it owns the lease's release, whatever ends the response;
- the file names of section 16.3, which only `content_disposition` turns into a header.

An aborted response has already sent its headers, so it returns without completing its body and
the server closes the connection; the client sees a body shorter than `Content-Length`. The
shutdown drain aborts every running download through `abort_running_downloads`.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any, Final, Literal, Protocol, cast, override
from uuid import UUID

from azure.core.exceptions import AzureError, ResourceNotFoundError
from starlette.responses import StreamingResponse
from starlette.types import Message, Receive, Scope, Send

from docchat_api.http.headers import FILE_CSP, content_disposition
from docchat_api.http.problems import DependencyUnavailable
from docchat_api.usage import UsageService
from docchat_core.clients import ClientRegistry
from docchat_core.cosmos.usage import DOWNLOAD_LEASE_SECONDS
from docchat_core.errors import DocChatProblem
from docchat_core.ingestion.names import BlobContainer
from docchat_core.models.clock import Clock, SystemClock
from docchat_core.opsconfig import RateLimitOps
from docchat_core.telemetry import LOGGER_NAME, log_event, qualified_type_name
from docchat_core.telemetry.instruments import counter

PDF_MEDIA_TYPE: Final = "application/pdf"
PDF_EXTENSION: Final = ".pdf"
PIECE_BYTES: Final = 65_536
"""A chunk is written in pieces of 64 KiB, so a stalled reader is noticed within one piece."""
PIECE_TIMEOUT_SECONDS: Final = 30.0
"""A client that accepts less than 64 KiB in 30 s (about 17 kbit/s) has stopped reading."""
DOWNLOAD_SECONDS: Final = DOWNLOAD_LEASE_SECONDS
"""A download never outlives its lease: 300 s carries the largest whole-document rendition at
about 2.8 Mbit/s."""
FILE_NAME_MAX_CODE_POINTS: Final = 255
PDF_RESPONSES: Final[dict[int | str, dict[str, Any]]] = {
    200: {
        "description": "The rendition's bytes, always the whole file.",
        "content": {PDF_MEDIA_TYPE: {"schema": {"type": "string", "format": "binary"}}},
    }
}
"""The `200` of a file operation in the OpenAPI document."""

DOWNLOAD_ABORTED: Final = "citation_download_aborted"
STREAM_FAILED: Final = "citation_stream_failed"
DOWNLOAD_ABORTS: Final = counter("docchat.citation.download_aborts")

type AbortReason = Literal["slow_client", "duration", "drain"]
type StreamOutcome = Literal["completed", "disconnected", "aborted", "failed"]
"""How a file response ended: its whole body sent, the client gone, aborted by a limit or the
drain, or a storage failure after the headers."""

_logger: Final = logging.getLogger(LOGGER_NAME)
_SYSTEM_CLOCK: Final = SystemClock()


# --- Ports -------------------------------------------------------------------------------------


class BlobDownload(Protocol):
    """What the response reads of `StorageStreamDownloader`: the blob's size and its chunks."""

    @property
    def size(self) -> int: ...

    def chunks(self) -> AsyncIterator[bytes]: ...


class RenditionBlob(Protocol):
    async def download_blob(self) -> BlobDownload: ...


class RenditionsContainer(Protocol):
    """The one call the file operations make on the `renditions` container client."""

    def get_blob_client(self, blob: str) -> RenditionBlob: ...


class Releasable(Protocol):
    """A held download lease (`docchat_api.usage.Lease`)."""

    async def release(self) -> None: ...


def renditions_container() -> RenditionsContainer:
    """The process's `renditions` container client, from the client registry the lifespan
    opened, sharing the data account's connections; tests replace the dependency."""
    # The SDK's signatures are wider than the protocol, which names only the call made.
    container = ClientRegistry.current().blob_container(BlobContainer.RENDITIONS)
    return cast("RenditionsContainer", container)


def download_clock() -> Clock:
    """The clock the duration cap reads; tests replace the dependency with a `FakeClock`."""
    return _SYSTEM_CLOCK


# --- The download limits and the blob ----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class HeldDownload:
    """A download lease and the instant on the clock's monotonic scale the download must end by."""

    lease: Releasable
    deadline: float


async def hold_download(
    usage: UsageService,
    oid: UUID,
    ratelimit: RateLimitOps,
    *,
    session_id: UUID | None,
    turn_id: UUID | None,
    clock: Clock,
) -> HeldDownload:
    """The download window, then a download lease: one window and one set of leases per user for
    the three file operations. `session_id` and `turn_id` name the cited turn, both `None` for an
    administrator's download. Raises `RateLimited`, `TooManyDownloads` or
    `DependencyUnavailable`."""
    await usage.check_download_rate(oid, ratelimit)
    # Read before the lease is written, so the cap ends no later than the lease's own expiry.
    started = clock.monotonic()
    lease = await usage.acquire_download_lease(
        oid, session_id=session_id, turn_id=turn_id, ratelimit=ratelimit
    )
    return HeldDownload(lease=lease, deadline=started + DOWNLOAD_SECONDS)


class DocumentPdfUnavailable(DocChatProblem):
    """`409`: the version has no whole-document rendition (`too_large`, or none recorded); its
    cited pages still open. No blob is read."""

    code = "document_pdf_unavailable"


class RenditionMissing(Exception):  # noqa: N818 - a storage outcome, not a defect
    """The storage service answered `404` for a rendition the database says exists."""


async def open_rendition(container: RenditionsContainer, path: str) -> BlobDownload:
    """Start reading the blob at `path` of `renditions`: the first 1 MiB and the size. A storage
    `404` raises `RenditionMissing`; any other storage failure, after the Blob client's retries,
    `DependencyUnavailable`."""
    try:
        return await container.get_blob_client(path).download_blob()
    except ResourceNotFoundError:
        raise RenditionMissing from None
    except AzureError as error:
        raise DependencyUnavailable() from error


# --- File names --------------------------------------------------------------------------------


def file_stem(source_name: str) -> str:
    """The document's source name without its extension (the text after its last `.`)."""
    stem, dot, _ = source_name.rpartition(".")
    return stem if dot else source_name


def page_file_name(source_name: str, page: int) -> str:
    """`<stem>-page-<n>.pdf`, the stem cut first so the name never exceeds 255 code points."""
    suffix = f"-page-{page}{PDF_EXTENSION}"
    return file_stem(source_name)[: FILE_NAME_MAX_CODE_POINTS - len(suffix)] + suffix


def document_file_name(source_name: str) -> str:
    """`<stem>.pdf`, the stem cut to 251 code points."""
    stem = file_stem(source_name)[: FILE_NAME_MAX_CODE_POINTS - len(PDF_EXTENSION)]
    return stem + PDF_EXTENSION


# --- The response ------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DownloadLabels:
    """What the download's log records name: the cited turn and the citation number, all absent
    for an administrator's download."""

    session_id: UUID | None = None
    turn_id: UUID | None = None
    citation_number: int | None = None

    def attributes(self) -> dict[str, object]:
        attributes: dict[str, object] = {}
        if self.session_id is not None:
            attributes["docchat.session_id"] = str(self.session_id)
        if self.turn_id is not None:
            attributes["docchat.turn_id"] = str(self.turn_id)
        if self.citation_number is not None:
            attributes["docchat.citation.number"] = self.citation_number
        return attributes


class _Halted(Exception):  # noqa: N818 - internal control flow
    """The download stops: aborted for `reason`, or the client went away (`reason` `None`)."""

    def __init__(self, reason: AbortReason | None) -> None:
        super().__init__(reason or "disconnected")
        self.reason: AbortReason | None = reason


class _ReadFailed(Exception):  # noqa: N818 - internal control flow
    def __init__(self, error: AzureError) -> None:
        super().__init__(type(error).__name__)
        self.error = error


_RUNNING: Final[set[CitationFileResponse]] = set()
"""The file responses this worker is streaming, which the drain aborts."""


def _retrieve(task: asyncio.Task[None]) -> None:
    # The watcher's outcome is never needed; reading it keeps asyncio from reporting it.
    if not task.cancelled():
        task.exception()


class CitationFileResponse(StreamingResponse):
    """A verified PDF rendition streamed from the `renditions` container.

    The headers are fixed before the first byte: `Content-Type: application/pdf`,
    `Content-Length` (the blob's size), `Content-Disposition` from `content_disposition` and
    `Content-Security-Policy: sandbox`; the security-header middleware adds the rest. The body is
    always the whole blob: no range support and no compression.

    Each piece's send runs under a 30-second timeout and every wait under the download's
    deadline; a piece not accepted in time aborts with `slow_client`, the deadline with
    `duration`, the drain with `drain`, each logged as `citation_download_aborted` and counted in
    `docchat.citation.download_aborts`. A storage failure after the headers ends the body early
    and logs `citation_stream_failed`. A client that disconnects stops the reads. The lease is
    released, shielded from cancellation, whenever the response ends; `finished` then learns how
    it ended.
    """

    media_type = PDF_MEDIA_TYPE

    def __init__(
        self,
        download: BlobDownload,
        *,
        file_name: str,
        held: HeldDownload,
        clock: Clock,
        labels: DownloadLabels | None = None,
        finished: Callable[[StreamOutcome], None] | None = None,
        piece_timeout: float = PIECE_TIMEOUT_SECONDS,
    ) -> None:
        super().__init__(
            download.chunks(),
            media_type=PDF_MEDIA_TYPE,
            headers={
                "Content-Length": str(download.size),
                "Content-Disposition": content_disposition(file_name),
                "Content-Security-Policy": FILE_CSP,
            },
        )
        self._held = held
        self._clock = clock
        self._labels = labels or DownloadLabels()
        self._finished = finished
        self._piece_timeout = piece_timeout
        self._timer: asyncio.Timeout | None = None
        self._stopping = False
        self._stop_reason: AbortReason | None = None
        self.bytes_sent = 0

    def abort(self, reason: AbortReason) -> None:
        """Stop the download at its next wait, or at once when it is waiting now."""
        self._stop(reason)

    @override
    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        _RUNNING.add(self)
        outcome: StreamOutcome = "failed"
        try:
            outcome = await self._serve(receive, send)
        except asyncio.CancelledError:
            outcome = "disconnected"
            raise
        finally:
            _RUNNING.discard(self)
            try:
                await self._held.lease.release()
            finally:
                if self._finished is not None:
                    self._finished(outcome)

    async def _serve(self, receive: Receive, send: Send) -> StreamOutcome:
        watcher = asyncio.create_task(self._watch(receive))
        watcher.add_done_callback(_retrieve)
        try:
            await self._send(
                send,
                {
                    "type": "http.response.start",
                    "status": self.status_code,
                    "headers": self.raw_headers,
                },
            )
            chunks = aiter(self.body_iterator)
            while (chunk := await self._read(chunks)) is not None:
                for start in range(0, len(chunk), PIECE_BYTES):
                    piece = chunk[start : start + PIECE_BYTES]
                    await self._send(
                        send, {"type": "http.response.body", "body": piece, "more_body": True}
                    )
                    self.bytes_sent += len(piece)
            await self._send(send, {"type": "http.response.body", "body": b"", "more_body": False})
        except _Halted as halted:
            if halted.reason is None:
                return "disconnected"
            self._report_abort(halted.reason)
            return "aborted"
        except OSError:
            # A server on ASGI spec 2.4 raises here when the client has gone away.
            return "disconnected"
        except _ReadFailed as failed:
            self._report_failure(failed.error)
            return "failed"
        finally:
            watcher.cancel()
        return "completed"

    async def _watch(self, receive: Receive) -> None:
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                self._stop(None)
                return

    def _stop(self, reason: AbortReason | None) -> None:
        if self._stopping:
            return
        self._stopping = True
        self._stop_reason = reason
        timer = self._timer
        if timer is not None and not timer.expired():
            timer.reschedule(asyncio.get_running_loop().time())

    def _limit(self, *, piece: bool) -> tuple[float, AbortReason]:
        """How long the next wait may last and what it means when it lasts that long."""
        if self._stopping:
            raise _Halted(self._stop_reason)
        remaining = self._held.deadline - self._clock.monotonic()
        if remaining <= 0:
            raise _Halted("duration")
        if piece and self._piece_timeout < remaining:
            return self._piece_timeout, "slow_client"
        return remaining, "duration"

    def _halted(self, reason: AbortReason) -> _Halted:
        return _Halted(self._stop_reason if self._stopping else reason)

    async def _send(self, send: Send, message: Message) -> None:
        limit, reason = self._limit(piece=True)
        try:
            async with asyncio.timeout(limit) as timer:
                self._timer = timer
                await send(message)
        except TimeoutError:
            raise self._halted(reason) from None
        finally:
            self._timer = None

    async def _read(self, chunks: AsyncIterator[str | bytes | memoryview]) -> bytes | None:
        limit, reason = self._limit(piece=False)
        try:
            async with asyncio.timeout(limit) as timer:
                self._timer = timer
                chunk = await anext(chunks, None)
        except TimeoutError:
            raise self._halted(reason) from None
        except AzureError as error:
            raise _ReadFailed(error) from error
        finally:
            self._timer = None
        if chunk is None or isinstance(chunk, bytes):
            return chunk
        return chunk.encode(self.charset) if isinstance(chunk, str) else bytes(chunk)

    def _report_abort(self, reason: AbortReason) -> None:
        log_event(
            _logger,
            DOWNLOAD_ABORTED,
            logging.WARNING,
            **self._labels.attributes(),
            **{
                "docchat.citation.bytes_sent": self.bytes_sent,
                "docchat.citation.abort_reason": reason,
            },
        )
        DOWNLOAD_ABORTS.add(1, reason=reason)

    def _report_failure(self, error: AzureError) -> None:
        log_event(
            _logger,
            STREAM_FAILED,
            logging.ERROR,
            **self._labels.attributes(),
            **{
                "docchat.citation.bytes_sent": self.bytes_sent,
                "error.type": qualified_type_name(type(error)),
            },
        )


def running_downloads() -> int:
    """How many file responses this worker is streaming now."""
    return len(_RUNNING)


def abort_running_downloads() -> int:
    """The drain point's step 3: abort every running download with reason `drain`, each
    releasing its lease as it ends; returns how many there were."""
    running = tuple(_RUNNING)
    for response in running:
        response.abort("drain")
    return len(running)
