"""Unit tests of the file operations' shared parts: the download limits, the blob, the file names
(T-277) and `CitationFileResponse` (the in-process halves of T-278 and T-279;
`12-chat-pipeline.md` sections 14.8, 16.3 to 16.6).

The response is driven through its ASGI interface with a recording `send`, so a test can hold a
piece, drop the connection or move the fake clock between pieces; its body comes from a real
`ContainerClient` over `BlobTransport`, so `download_blob()` and `chunks()` run their own code.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Final, Protocol, cast
from uuid import UUID

import pytest
from azure.storage.blob.aio import ContainerClient
from starlette.types import Message

from docchat_api.chat.citations import (
    DOWNLOAD_SECONDS,
    PIECE_BYTES,
    CitationFileResponse,
    DocumentPdfUnavailable,
    DownloadLabels,
    HeldDownload,
    RenditionMissing,
    RenditionsContainer,
    StreamOutcome,
    abort_running_downloads,
    document_file_name,
    download_clock,
    file_stem,
    hold_download,
    open_rendition,
    page_file_name,
    renditions_container,
    running_downloads,
)
from docchat_api.http.headers import content_disposition
from docchat_api.http.problems import DependencyUnavailable
from docchat_api.usage import RateLimited, TooManyDownloads, UsageService
from docchat_core.clients import ClientRegistry
from docchat_core.cosmos.containers import USAGE
from docchat_core.cosmos.usage import download_lease_id, download_rate_window_id, epoch_minute
from docchat_core.models.clock import SystemClock
from docchat_core.opsconfig import OpsConfig, RateLimitOps
from docchat_testing.clock import FakeClock
from docchat_testing.fakes.blobs import BlobTransport
from docchat_testing.fakes.cosmos import FakeContainer

ACCOUNT: Final = "https://stdocchatdatatest.blob.core.windows.net"
MIB: Final = 1_048_576
INDEX: Final = "a1b2c3d4-0000-4000-8000-000000000001"
DOCUMENT: Final = "b2c3d4e5-0000-4000-8000-000000000002"
BLOB_PATH: Final = f"{INDEX}/{DOCUMENT}/3/document.pdf"
URL_PATH: Final = f"/renditions/{BLOB_PATH}"
BLOB: Final = bytes(range(256)) * 10_000
SESSION: Final = UUID("c3d4e5f6-0000-4000-8000-000000000003")
TURN: Final = UUID("d4e5f6a7-0000-4000-8000-000000000004")
OID: Final = UUID("0a11ce00-0000-4000-8000-000000000001")
LABELS: Final = DownloadLabels(session_id=SESSION, turn_id=TURN, citation_number=2)
SCOPE: Final[dict[str, Any]] = {"type": "http", "asgi": {"spec_version": "2.3"}}


class Captured(Protocol):
    def records(self, message: str) -> list[logging.LogRecord]: ...

    def points(self, metric_name: str) -> dict[tuple[tuple[str, object], ...], float]: ...


# --- File names (T-277) ------------------------------------------------------------------------


def test_t277_a_page_name_from_a_255_code_point_name_fits_and_keeps_its_suffix() -> None:
    source_name = "é" * 250 + ".docx"
    assert len(source_name) == 255

    name = page_file_name(source_name, 2_000)

    assert len(name) == 255
    assert name.endswith("-page-2000.pdf")
    assert name.startswith("é" * 241)


def test_t277_a_whole_document_name_from_the_same_name_fits_and_ends_in_pdf() -> None:
    name = document_file_name("é" * 250 + ".docx")

    assert len(name) <= 255
    assert name == "é" * 250 + ".pdf"
    assert document_file_name("é" * 255) == "é" * 251 + ".pdf"


def test_t277_a_short_name_keeps_its_stem() -> None:
    assert page_file_name("Travel Policy.pdf", 2) == "Travel Policy-page-2.pdf"
    assert document_file_name("Board pack v2.final.pptx") == "Board pack v2.final.pdf"
    assert document_file_name("x" * 252 + ".pdf") == "x" * 251 + ".pdf"


def test_the_stem_drops_only_the_last_extension() -> None:
    assert file_stem("report.v2.docx") == "report.v2"
    assert file_stem("no extension") == "no extension"
    assert file_stem(".pdf") == ""
    assert document_file_name(".pdf") == ".pdf"


# --- The download limits -----------------------------------------------------------------------


class StaticOps:
    async def snapshot(self) -> OpsConfig:
        raise AssertionError("the download limits read the request's snapshot only")


def ratelimit(downloads: int = 30, concurrent: int = 2) -> RateLimitOps:
    return RateLimitOps.model_validate(
        {
            "ratelimit.chat_requests_per_minute": 3,
            "ratelimit.max_concurrent_streams": 2,
            "ratelimit.citation_downloads_per_minute": downloads,
            "ratelimit.max_concurrent_downloads": concurrent,
        }
    )


@pytest.fixture
def usage_container(clock: FakeClock) -> FakeContainer:
    return FakeContainer.of(USAGE, clock=clock)


@pytest.fixture
async def usage(usage_container: FakeContainer, clock: FakeClock) -> AsyncIterator[UsageService]:
    service = UsageService(cast("Any", usage_container), ops=StaticOps(), clock=clock)
    yield service
    await service.aclose()


async def test_a_download_counts_in_the_window_then_takes_a_lease(
    usage: UsageService, usage_container: FakeContainer, clock: FakeClock
) -> None:
    held = await hold_download(
        usage, OID, ratelimit(), session_id=SESSION, turn_id=TURN, clock=clock
    )

    minute = epoch_minute(clock.now())
    window = usage_container.get(str(OID), download_rate_window_id(minute))
    lease = usage_container.get(str(OID), download_lease_id(1))
    assert window is not None
    assert window["count"] == 1
    assert lease is not None
    assert (lease["sessionId"], lease["turnId"], lease["ttl"]) == (str(SESSION), str(TURN), 300)
    assert held.deadline == clock.monotonic() + DOWNLOAD_SECONDS
    await held.lease.release()
    assert usage_container.get(str(OID), download_lease_id(1)) is None


async def test_an_administrators_download_lease_names_no_turn(
    usage: UsageService, usage_container: FakeContainer, clock: FakeClock
) -> None:
    held = await hold_download(usage, OID, ratelimit(), session_id=None, turn_id=None, clock=clock)

    lease = usage_container.get(str(OID), download_lease_id(1))
    assert lease is not None
    assert (lease["sessionId"], lease["turnId"]) == (None, None)
    await held.lease.release()


async def test_a_full_window_refuses_before_any_lease(
    usage: UsageService, usage_container: FakeContainer, clock: FakeClock
) -> None:
    minute = epoch_minute(clock.now())
    usage_container.put(
        {
            "id": download_rate_window_id(minute),
            "userId": str(OID),
            "type": "download_rate_window",
            "count": 1,
            "ttl": 180,
        }
    )

    with pytest.raises(RateLimited) as refused:
        await hold_download(
            usage, OID, ratelimit(downloads=1), session_id=SESSION, turn_id=TURN, clock=clock
        )

    assert refused.value.retry_after_seconds is not None
    assert usage_container.calls_of("create_item") == []


async def test_every_live_lease_refuses_with_10_seconds(
    usage: UsageService, clock: FakeClock
) -> None:
    first = await hold_download(
        usage, OID, ratelimit(concurrent=1), session_id=SESSION, turn_id=TURN, clock=clock
    )

    with pytest.raises(TooManyDownloads) as refused:
        await hold_download(
            usage, OID, ratelimit(concurrent=1), session_id=SESSION, turn_id=TURN, clock=clock
        )

    assert refused.value.retry_after_seconds == 10
    await first.lease.release()


def test_the_download_clock_is_the_system_clock() -> None:
    assert isinstance(download_clock(), SystemClock)


def test_the_renditions_container_comes_from_the_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asked: list[str] = []
    container = object()

    def blob_container(name: str) -> object:
        asked.append(name)
        return container

    registry = SimpleNamespace(blob_container=blob_container)
    monkeypatch.setattr(ClientRegistry, "current", classmethod(lambda cls: registry))

    assert renditions_container() is container
    assert asked == ["renditions"]


def test_the_whole_document_refusal_is_a_409() -> None:
    problem = DocumentPdfUnavailable()

    assert (problem.status, problem.entry.code) == (409, "document_pdf_unavailable")


# --- The blob ----------------------------------------------------------------------------------


@pytest.fixture
def transport() -> BlobTransport:
    return BlobTransport(blobs={URL_PATH: BLOB})


@pytest.fixture
async def renditions(transport: BlobTransport) -> AsyncIterator[RenditionsContainer]:
    client = ContainerClient(
        ACCOUNT,
        "renditions",
        transport=transport,
        retry_total=0,
        max_single_get_size=MIB,
        max_chunk_get_size=MIB,
    )
    async with client:
        yield cast("RenditionsContainer", client)


async def test_opening_a_rendition_reads_its_first_chunk_and_size(
    renditions: RenditionsContainer, transport: BlobTransport
) -> None:
    download = await open_rendition(renditions, BLOB_PATH)

    assert download.size == len(BLOB)
    assert transport.paths() == [URL_PATH]


async def test_a_missing_rendition_is_reported_as_missing(
    renditions: RenditionsContainer,
) -> None:
    with pytest.raises(RenditionMissing):
        await open_rendition(renditions, BLOB_PATH.replace("document.pdf", "pages/1.pdf"))


async def test_another_storage_failure_is_a_503(
    renditions: RenditionsContainer, transport: BlobTransport
) -> None:
    transport.failing[URL_PATH] = 0

    with pytest.raises(DependencyUnavailable):
        await open_rendition(renditions, BLOB_PATH)


# --- The response ------------------------------------------------------------------------------


@dataclass
class FakeLease:
    releases: int = 0

    async def release(self) -> None:
        self.releases += 1


@dataclass
class Wire:
    """The connection's side of the ASGI call: what was sent, and the controls a test uses."""

    sent: list[Message] = field(default_factory=list)
    hold_from: int | None = None
    """Block every body message from this index on, as a reader that stopped reading."""
    on_send: Callable[[int], None] | None = None
    raise_on_send: BaseException | None = None
    disconnect: asyncio.Event = field(default_factory=asyncio.Event)
    first_body: asyncio.Event = field(default_factory=asyncio.Event)
    _requested: bool = False

    async def send(self, message: Message) -> None:
        if self.raise_on_send is not None and message["type"] == "http.response.body":
            raise self.raise_on_send
        index = len(self.bodies())
        body = message["type"] == "http.response.body"
        if body and self.hold_from is not None and index >= self.hold_from:
            await asyncio.Event().wait()
        self.sent.append(message)
        if body:
            self.first_body.set()
        if self.on_send is not None:
            self.on_send(index)

    async def receive(self) -> Message:
        if not self._requested:
            self._requested = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await self.disconnect.wait()
        return {"type": "http.disconnect"}

    def bodies(self) -> list[bytes]:
        return [m.get("body", b"") for m in self.sent if m["type"] == "http.response.body"]

    def headers(self) -> dict[str, str]:
        (start,) = [m for m in self.sent if m["type"] == "http.response.start"]
        return {name.decode(): value.decode() for name, value in start["headers"]}

    def completed(self) -> bool:
        bodies = [m for m in self.sent if m["type"] == "http.response.body"]
        return bool(bodies) and bodies[-1].get("more_body") is False


@dataclass
class Ending:
    outcomes: list[StreamOutcome] = field(default_factory=list)

    def __call__(self, outcome: StreamOutcome) -> None:
        self.outcomes.append(outcome)


async def respond(
    renditions: RenditionsContainer,
    clock: FakeClock,
    *,
    lease: FakeLease,
    ending: Ending,
    labels: DownloadLabels = LABELS,
    deadline: float | None = None,
    piece_timeout: float = 30.0,
) -> CitationFileResponse:
    download = await open_rendition(renditions, BLOB_PATH)
    held = HeldDownload(
        lease=lease,
        deadline=clock.monotonic() + DOWNLOAD_SECONDS if deadline is None else deadline,
    )
    return CitationFileResponse(
        download,
        file_name="Travel Policy.pdf",
        held=held,
        clock=clock,
        labels=labels,
        finished=ending,
        piece_timeout=piece_timeout,
    )


async def test_a_completed_download_sends_the_whole_blob_in_64_kib_pieces(
    renditions: RenditionsContainer, clock: FakeClock
) -> None:
    lease, ending, wire = FakeLease(), Ending(), Wire()
    response = await respond(renditions, clock, lease=lease, ending=ending)

    await response(SCOPE, wire.receive, wire.send)

    bodies = wire.bodies()
    assert b"".join(bodies) == BLOB
    assert all(0 < len(piece) <= PIECE_BYTES for piece in bodies[:-1])
    assert len([piece for piece in bodies if len(piece) == PIECE_BYTES]) == len(BLOB) // PIECE_BYTES
    assert wire.completed()
    assert wire.headers() == {
        "content-length": str(len(BLOB)),
        "content-disposition": content_disposition("Travel Policy.pdf"),
        "content-security-policy": "sandbox",
        "content-type": "application/pdf",
    }
    assert response.bytes_sent == len(BLOB)
    assert lease.releases == 1
    assert ending.outcomes == ["completed"]
    assert running_downloads() == 0


async def test_t279_a_reader_that_stops_is_aborted_as_a_slow_client(
    renditions: RenditionsContainer, clock: FakeClock, captured: Captured
) -> None:
    lease, ending, wire = FakeLease(), Ending(), Wire(hold_from=1)
    response = await respond(renditions, clock, lease=lease, ending=ending, piece_timeout=0.05)

    await response(SCOPE, wire.receive, wire.send)

    assert wire.bodies() == [BLOB[:PIECE_BYTES]]
    assert not wire.completed()
    (record,) = captured.records("citation_download_aborted")
    assert record.levelno == logging.WARNING
    assert record.__dict__["docchat.citation.abort_reason"] == "slow_client"
    assert record.__dict__["docchat.citation.bytes_sent"] == PIECE_BYTES
    assert record.__dict__["docchat.session_id"] == str(SESSION)
    assert record.__dict__["docchat.turn_id"] == str(TURN)
    assert record.__dict__["docchat.citation.number"] == 2
    assert captured.points("docchat.citation.download_aborts") == {(("reason", "slow_client"),): 1}
    assert lease.releases == 1
    assert ending.outcomes == ["aborted"]


async def test_t279_a_download_still_running_at_300_seconds_is_aborted(
    renditions: RenditionsContainer, clock: FakeClock, captured: Captured
) -> None:
    lease, ending = FakeLease(), Ending()

    def time_passes(index: int) -> None:
        if index == 2:
            clock.advance(DOWNLOAD_SECONDS)

    wire = Wire(on_send=time_passes)
    response = await respond(renditions, clock, lease=lease, ending=ending)

    await response(SCOPE, wire.receive, wire.send)

    assert len(wire.bodies()) == 3
    (record,) = captured.records("citation_download_aborted")
    assert record.__dict__["docchat.citation.abort_reason"] == "duration"
    assert record.__dict__["docchat.citation.bytes_sent"] == 3 * PIECE_BYTES
    assert lease.releases == 1
    assert ending.outcomes == ["aborted"]


async def test_a_stuck_storage_read_meets_the_duration_cap(
    renditions: RenditionsContainer,
    transport: BlobTransport,
    clock: FakeClock,
    captured: Captured,
) -> None:
    transport.stalled[URL_PATH] = MIB
    lease, ending, wire = FakeLease(), Ending(), Wire()
    response = await respond(
        renditions, clock, lease=lease, ending=ending, deadline=clock.monotonic() + 0.1
    )

    await response(SCOPE, wire.receive, wire.send)

    assert b"".join(wire.bodies()) == BLOB[:MIB]
    (record,) = captured.records("citation_download_aborted")
    assert record.__dict__["docchat.citation.abort_reason"] == "duration"
    assert ending.outcomes == ["aborted"]


async def test_the_drain_aborts_every_running_download(
    renditions: RenditionsContainer, clock: FakeClock, captured: Captured
) -> None:
    lease, ending, wire = FakeLease(), Ending(), Wire(hold_from=1)
    response = await respond(renditions, clock, lease=lease, ending=ending)
    task = asyncio.create_task(response(SCOPE, wire.receive, wire.send))
    await wire.first_body.wait()
    await asyncio.sleep(0)

    assert running_downloads() == 1
    assert abort_running_downloads() == 1
    await task

    (record,) = captured.records("citation_download_aborted")
    assert record.__dict__["docchat.citation.abort_reason"] == "drain"
    assert captured.points("docchat.citation.download_aborts") == {(("reason", "drain"),): 1}
    assert lease.releases == 1
    assert ending.outcomes == ["aborted"]
    assert running_downloads() == 0
    assert abort_running_downloads() == 0


async def test_a_client_that_disconnects_stops_the_reads_without_an_abort(
    renditions: RenditionsContainer,
    transport: BlobTransport,
    clock: FakeClock,
    captured: Captured,
) -> None:
    lease, ending = FakeLease(), Ending()
    wire = Wire(hold_from=1)
    response = await respond(renditions, clock, lease=lease, ending=ending)
    task = asyncio.create_task(response(SCOPE, wire.receive, wire.send))
    await wire.first_body.wait()

    wire.disconnect.set()
    await task

    assert captured.records("citation_download_aborted") == []
    assert transport.paths() == [URL_PATH]
    assert lease.releases == 1
    assert ending.outcomes == ["disconnected"]


async def test_a_send_that_raises_oserror_is_a_disconnect(
    renditions: RenditionsContainer, clock: FakeClock
) -> None:
    lease, ending = FakeLease(), Ending()
    wire = Wire(raise_on_send=OSError("gone"))
    response = await respond(renditions, clock, lease=lease, ending=ending)

    await response(SCOPE, wire.receive, wire.send)

    assert lease.releases == 1
    assert ending.outcomes == ["disconnected"]


async def test_a_storage_failure_after_the_headers_ends_the_body_early(
    renditions: RenditionsContainer,
    transport: BlobTransport,
    clock: FakeClock,
    captured: Captured,
) -> None:
    transport.failing[URL_PATH] = MIB
    lease, ending, wire = FakeLease(), Ending(), Wire()
    response = await respond(renditions, clock, lease=lease, ending=ending)

    await response(SCOPE, wire.receive, wire.send)

    assert b"".join(wire.bodies()) == BLOB[:MIB]
    assert not wire.completed()
    (record,) = captured.records("citation_stream_failed")
    assert record.levelno == logging.ERROR
    assert record.__dict__["docchat.citation.bytes_sent"] == MIB
    assert record.__dict__["error.type"] == "azure.core.exceptions.HttpResponseError"
    assert lease.releases == 1
    assert ending.outcomes == ["failed"]


async def test_a_cancelled_response_still_releases_its_lease(
    renditions: RenditionsContainer, clock: FakeClock
) -> None:
    lease, ending, wire = FakeLease(), Ending(), Wire(hold_from=0)
    response = await respond(renditions, clock, lease=lease, ending=ending)
    task = asyncio.create_task(response(SCOPE, wire.receive, wire.send))
    await asyncio.sleep(0.01)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert lease.releases == 1
    assert ending.outcomes == ["disconnected"]


async def test_an_administrators_download_logs_no_turn(
    renditions: RenditionsContainer, clock: FakeClock, captured: Captured
) -> None:
    lease, ending, wire = FakeLease(), Ending(), Wire(hold_from=0)
    response = await respond(
        renditions, clock, lease=lease, ending=ending, labels=DownloadLabels(), piece_timeout=0.02
    )

    await response(SCOPE, wire.receive, wire.send)

    (record,) = captured.records("citation_download_aborted")
    for key in ("docchat.session_id", "docchat.turn_id", "docchat.citation.number"):
        assert key not in record.__dict__
    assert record.__dict__["docchat.citation.bytes_sent"] == 0


async def test_an_abort_before_the_headers_sends_nothing(
    renditions: RenditionsContainer, clock: FakeClock
) -> None:
    lease, ending, wire = FakeLease(), Ending(), Wire()
    response = await respond(renditions, clock, lease=lease, ending=ending)
    response.abort("drain")

    await response(SCOPE, wire.receive, wire.send)

    assert wire.sent == []
    assert ending.outcomes == ["aborted"]
    assert lease.releases == 1


async def test_without_a_finished_callback_the_response_still_releases(
    renditions: RenditionsContainer, clock: FakeClock
) -> None:
    lease, wire = FakeLease(), Wire()
    download = await open_rendition(renditions, BLOB_PATH)
    response = CitationFileResponse(
        download,
        file_name="x.pdf",
        held=HeldDownload(lease=lease, deadline=clock.monotonic() + 1),
        clock=clock,
    )

    await response(SCOPE, wire.receive, wire.send)

    assert wire.completed()
    assert lease.releases == 1


@dataclass
class TextChunks:
    """A body whose chunks are text and a memory view, which the response sends as bytes."""

    size: int = 6

    async def _chunks(self) -> AsyncIterator[Any]:
        yield "abc"
        yield memoryview(b"def")

    def chunks(self) -> AsyncIterator[bytes]:
        return cast("AsyncIterator[bytes]", self._chunks())


async def test_text_and_memory_view_chunks_go_out_as_bytes(clock: FakeClock) -> None:
    lease, wire = FakeLease(), Wire()
    response = CitationFileResponse(
        TextChunks(),
        file_name="x.pdf",
        held=HeldDownload(lease=lease, deadline=clock.monotonic() + 1),
        clock=clock,
    )

    await response(SCOPE, wire.receive, wire.send)

    assert wire.bodies() == [b"abc", b"def", b""]
