"""Fakes and builders shared by the unit tests of the document activities.

SQL runs on a scripted connection: every statement is compiled for SQL Server, so a statement the
dialect can't render fails here, and each read or write is answered from the script in order
(inserts are recorded and answered without consuming it). Blob containers, search clients, the
OpenAI client and the parse pool are in-memory doubles of the ports the activities use.

Extract, Enrich and Chunk run the real methods, registered as the worker registers them, with
their ports over these doubles: the partition child's work runs in process (the real
`parse_worker.run_partition`, real `pikepdf` copying the part, a fake `unstructured` module,
and the parent's real validation of `partition.json`), the table summaries go through the real
`OpenAISummaryModel` to the fake client, and the layout call is the real async
`DocumentIntelligenceClient` given `ReplayTransport`. `FakeMethod` stays for the tests of an
activity's own rules. Each fixture returns a factory or a namespace, because test modules can't
import a conftest. The behaviour against SQL Server and Azurite is proven by the integration
tests.
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import io
import re
import sys
import tempfile
from collections import deque
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Final, cast, override
from uuid import UUID, uuid4

import openai as openai_module
import pikepdf
import pytest
from azure.ai.documentintelligence.aio import DocumentIntelligenceClient
from azure.core.credentials import AccessToken
from azure.core.exceptions import ResourceNotFoundError, ServiceRequestError
from azure.core.rest import HttpRequest
from azure.storage.blob.aio import ContainerClient
from pikepdf import Dictionary, Name, Stream
from sqlalchemy.dialects import mssql
from sqlalchemy.sql.dml import Insert
from sqlalchemy.sql.elements import ClauseElement

from docchat_core.ingestion.methods import (
    PAGEWISE_V1,
    RECURSIVE_V1,
    ChunkResult,
    EnrichResult,
    ExtractPlan,
    ExtractResult,
    ExtractSource,
    MethodContext,
    MethodRegistry,
    PageRange,
    PagewiseParametersV1,
    RecursiveParametersV1,
)
from docchat_core.ingestion.models import ContentType, ItemSettings
from docchat_core.ingestion.names import converted_path, source_path, work_prefix
from docchat_core.ingestion.settings import TokenEncoding
from docchat_core.telemetry import TestExporters
from docchat_ingestion.activities.documents import (
    BuildPlan,
    ConversionPlan,
    DocumentServices,
    PhysicalIndexPlan,
)
from docchat_ingestion.isolation import parse_worker
from docchat_ingestion.isolation.results import (
    PARTITION,
    PDF,
    SOURCE,
    ChildFileError,
    ChildRequest,
    Command,
    ExtractPartV1,
    InspectOk,
    Limits,
    parse_partition,
    render_max_total_bytes,
)
from docchat_ingestion.isolation.results import PageRange as ChildPages
from docchat_ingestion.isolation.runner import (
    ChildOutcome,
    ParseUsersUnavailableError,
    RenditionOutcome,
)
from docchat_ingestion.methods.pagewise import partition
from docchat_ingestion.methods.pagewise.method import (
    BoundPartitioner,
    PagewiseMethod,
    PagewisePorts,
)
from docchat_ingestion.methods.pagewise.summaries import OpenAISummaryModel
from docchat_ingestion.methods.recursive.layout import DocumentIntelligenceLayout
from docchat_ingestion.methods.recursive.method import (
    RecursiveMethod,
    RecursivePorts,
    current_pdf_source,
)
from docchat_ingestion.methods.store import BlobStore
from docchat_testing.clock import FakeClock
from docchat_testing.fakes.replay import Exchange, ReplayResponse, ReplayTransport

DIALECT = mssql.dialect()  # type: ignore[no-untyped-call]
TRACEPARENT = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
RULES_VERSION = 1
CONVERTER = "lo-4:7.4.7-1+deb12u14/0123456789ab"


def pdf_bytes(pages: int, label: str = "") -> bytes:
    """A real PDF whose page `n` draws `Page n` (and `label`), for the real `pikepdf` copy of
    the partition child and for the fake library to read back."""
    pdf = pikepdf.new()
    for number in range(1, pages + 1):
        page = pdf.add_blank_page(page_size=(200, 200))
        font = Dictionary(Type=Name.Font, Subtype=Name.Type1, BaseFont=Name.Helvetica)
        page.obj.Resources = Dictionary(Font=Dictionary(F1=font))
        drawn = f"BT /F1 12 Tf 10 100 Td (Page {number}) Tj ({label}) Tj ET"
        page.obj.Contents = Stream(pdf, drawn.encode("latin-1"))
    buffer = io.BytesIO()
    pdf.save(buffer)
    return buffer.getvalue()


PDF_BYTES = pdf_bytes(3, "the version")
CONVERTED_BYTES = pdf_bytes(3, "converted from office")


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# --- SQL ------------------------------------------------------------------------------------------


@dataclass
class Result:
    rows: list[Any] = field(default_factory=list)
    rowcount: int = 0

    def first(self) -> Any:
        return self.rows[0] if self.rows else None

    def one(self) -> Any:
        assert len(self.rows) == 1
        return self.rows[0]

    def all(self) -> list[Any]:
        return list(self.rows)

    def scalar_one(self) -> Any:
        row = self.one()
        return row[0] if isinstance(row, tuple) else row

    def scalar_one_or_none(self) -> Any:
        return None if not self.rows else self.scalar_one()


class Row(tuple[Any, ...]):
    """A result row read by attribute or position."""

    _names: tuple[str, ...]

    def __new__(cls, **values: Any) -> Row:
        row = super().__new__(cls, tuple(values.values()))
        row._names = tuple(values)
        return row

    def __getattr__(self, name: str) -> Any:
        try:
            return self[self._names.index(name)]
        except ValueError:
            raise AttributeError(name) from None

    def _asdict(self) -> dict[str, Any]:
        return dict(zip(self._names, self, strict=True))


@dataclass
class Executed:
    sql: str
    statement: ClauseElement

    @property
    def values(self) -> dict[str, Any]:
        return dict(self.statement.compile(dialect=DIALECT).params)


class ScriptedConnection:
    def __init__(self, *results: Result) -> None:
        self.script = deque(results)
        self.executed: list[Executed] = []

    def execute(self, statement: ClauseElement, parameters: Any = None) -> Result:
        compiled = statement.compile(dialect=DIALECT)
        self.executed.append(Executed(str(compiled), statement))
        if isinstance(statement, Insert) and statement.select is None:
            return Result(rowcount=1)
        assert self.script, f"no scripted answer for: {compiled}"
        return self.script.popleft()

    def statements(self, prefix: str) -> list[Executed]:
        return [entry for entry in self.executed if entry.sql.startswith(prefix)]

    def finished(self) -> None:
        assert not self.script, "the script has unused answers"


class FakeSql:
    """The data-access layer's `run` on one scripted connection, recording each unit's name."""

    def __init__(self, connection: ScriptedConnection | None = None) -> None:
        self.connection = connection or ScriptedConnection()
        self.units: list[tuple[str, str]] = []
        self.failures: deque[BaseException] = deque()

    def script(self, *results: Result) -> None:
        self.connection.script.extend(results)

    async def run(self, work: Callable[[Any], Any], *, name: str, retry: str) -> Any:
        self.units.append((name, retry))
        if self.failures:
            raise self.failures.popleft()
        return work(self.connection)


def fence_ok() -> Result:
    return Result(rowcount=1)


def build_facts(
    *,
    page_count: int | None = 3,
    pdf_sha256: bytes | None = None,
    chunk_count: int | None = None,
    parse_seconds_used: int = 0,
    state: str = "building",
) -> Result:
    return Result(
        rows=[
            Row(
                page_count=page_count,
                pdf_sha256=pdf_sha256,
                chunk_count=chunk_count,
                parse_seconds_used=parse_seconds_used,
                state=state,
            )
        ]
    )


# --- Blob -----------------------------------------------------------------------------------------


class FakeDownloader:
    def __init__(self, data: bytes, chunk_size: int) -> None:
        self.data = data
        self.chunk_size = chunk_size

    async def chunks(self) -> AsyncIterator[bytes]:
        for start in range(0, len(self.data), self.chunk_size):
            yield self.data[start : start + self.chunk_size]

    async def readall(self) -> bytes:
        return self.data


class FakeContainer:
    """An in-memory container that records every call; `fail` names operations that raise a
    storage error (an `AzureError` the client's retries didn't absorb)."""

    def __init__(self, name: str, log: list[tuple[str, str, str]] | None = None) -> None:
        self.name = name
        self.blobs: dict[str, bytes] = {}
        self.kwargs: dict[str, dict[str, Any]] = {}
        self.log = log if log is not None else []
        self.fail: set[str] = set()
        # Several chunks per test document, so streaming is exercised, without hundreds of
        # worker-thread hops per download under asyncio's debug mode.
        self.chunk_size = 512

    def _check(self, operation: str) -> None:
        if operation in self.fail:
            raise ServiceRequestError("storage down")

    async def upload_blob(self, name: str, data: Any, **kwargs: Any) -> None:
        self._check("upload")
        self.log.append((self.name, "upload", name))
        self.blobs[name] = bytes(data)
        self.kwargs[name] = kwargs

    async def download_blob(self, blob: str, **kwargs: Any) -> FakeDownloader:
        self._check("download")
        self.log.append((self.name, "download", blob))
        if blob not in self.blobs:
            raise ResourceNotFoundError("absent")
        return FakeDownloader(self.blobs[blob], self.chunk_size)

    async def delete_blob(self, blob: str, **kwargs: Any) -> None:
        self._check("delete")
        self.log.append((self.name, "delete", blob))
        if blob not in self.blobs:
            raise ResourceNotFoundError("absent")
        del self.blobs[blob]

    async def _names(self, prefix: str) -> AsyncIterator[SimpleNamespace]:
        self._check("list")
        for name in sorted(self.blobs):
            if name.startswith(prefix):
                yield SimpleNamespace(name=name)

    def list_blobs(self, **kwargs: Any) -> AsyncIterator[SimpleNamespace]:
        return self._names(kwargs.get("name_starts_with", ""))

    async def delete_blobs(self, *blobs: str, **kwargs: Any) -> AsyncIterator[Any]:
        self._check("delete_blobs")
        assert len(blobs) <= 256
        assert kwargs.get("raise_on_any_failure") is False
        statuses = []
        for name in blobs:
            self.log.append((self.name, "delete", name))
            statuses.append(202 if self.blobs.pop(name, None) is not None else 404)

        async def responses() -> AsyncIterator[Any]:
            for status in statuses:
                yield SimpleNamespace(status_code=status)

        return responses()


# --- Search ---------------------------------------------------------------------------------------


class FakeSearch:
    """One physical index's documents; `refuse` maps a key to the statuses its next uploads get,
    `errors` holds exceptions the next calls raise."""

    def __init__(self) -> None:
        self.documents: dict[str, dict[str, Any]] = {}
        self.uploads: list[list[dict[str, Any]]] = []
        self.deletes: list[list[dict[str, Any]]] = []
        self.refuse: dict[str, list[int]] = {}
        self.errors: deque[BaseException] = deque()

    async def upload_documents(self, documents: list[dict[str, Any]], **kwargs: Any) -> Any:
        if self.errors:
            raise self.errors.popleft()
        self.uploads.append(documents)
        results = []
        for document in documents:
            key = document["id"]
            pending = self.refuse.get(key)
            if pending:
                status = pending.pop(0)
                results.append(SimpleNamespace(key=key, succeeded=False, status_code=status))
                continue
            self.documents[key] = document
            results.append(SimpleNamespace(key=key, succeeded=True, status_code=201))
        return results

    async def delete_documents(self, documents: list[dict[str, Any]], **kwargs: Any) -> Any:
        if self.errors:
            raise self.errors.popleft()
        assert len(documents) <= 500
        self.deletes.append(documents)
        for document in documents:
            self.documents.pop(document["id"], None)
        return [SimpleNamespace(key=document["id"], succeeded=True) for document in documents]


# --- OpenAI ---------------------------------------------------------------------------------------


class FakeEmbeddings:
    def __init__(self, dimensions: int) -> None:
        self.dimensions = dimensions
        self.calls: list[dict[str, Any]] = []
        self.errors: deque[BaseException] = deque()
        self.wrong_length = False

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self.errors:
            raise self.errors.popleft()
        size = self.dimensions - 1 if self.wrong_length else kwargs["dimensions"]
        data = [
            SimpleNamespace(index=index, embedding=[float(index)] * size)
            for index in reversed(range(len(kwargs["input"])))
        ]
        return SimpleNamespace(
            data=data, usage=SimpleNamespace(prompt_tokens=10 * len(kwargs["input"]))
        )


class FakeCompletions:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.responses: deque[Any] = deque()

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self.responses:
            response = self.responses.popleft()
            if isinstance(response, BaseException):
                raise response
            return response
        return completion("A table of quarterly figures in euros with a total row.")


def completion(
    content: str | None, *, finish: str = "stop", refusal: str | None = None
) -> SimpleNamespace:
    message = SimpleNamespace(content=content, refusal=refusal)
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason=finish)],
        usage=SimpleNamespace(prompt_tokens=120, completion_tokens=40),
    )


class FakeOpenAI:
    def __init__(self, dimensions: int = 4) -> None:
        self.embeddings = FakeEmbeddings(dimensions)
        self.chat = SimpleNamespace(completions=FakeCompletions())


# --- Parse pool -----------------------------------------------------------------------------------


class FakeFile(io.BytesIO):
    def __init__(self, scratch: FakeScratch, name: str) -> None:
        super().__init__()
        self.scratch = scratch
        self.file_name = name

    @override
    def close(self) -> None:
        self.scratch.files[self.file_name] = self.getvalue()
        super().close()


class FakeScratch:
    """A scratch directory: the files the parent wrote, and whether a child had it."""

    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.given = False

    def open_new(self, name: str) -> FakeFile:
        assert name not in self.files
        assert not self.given, "a directory a child had takes no new file"
        return FakeFile(self, name)

    def write(self, name: str, data: bytes) -> None:
        assert name not in self.files
        self.files[name] = data

    def open_own(self, name: str) -> io.BytesIO:
        if self.given:
            raise PermissionError("the scratch directory was handed over")
        return io.BytesIO(self.files[name])


def outcome(
    command: Command,
    code: str | None = None,
    *,
    pages: int | None = 3,
    seconds: int = 2,
    exit_kind: str = "ok",
) -> ChildOutcome:
    result = None
    if code is None and command is Command.INSPECT:
        result = InspectOk.model_validate({"ok": True, "pageCount": pages})
    return ChildOutcome(command, exit_kind, code, result, seconds, 50)  # type: ignore[arg-type]


class FakeSlot:
    def __init__(self, pool: FakePool) -> None:
        self.pool = pool

    def scratch(self) -> FakeScratch:
        directory = FakeScratch()
        self.pool.scratches.append(directory)
        return directory

    async def inspect(self, directory: FakeScratch, content_type: str, *, max_pages: int) -> Any:
        self.pool.calls.append(("inspect", content_type, dict(directory.files)))
        directory.given = True
        return (
            self.pool.inspections.popleft()
            if self.pool.inspections
            else outcome(Command.INSPECT, pages=self.pool.pages)
        )

    async def convert(
        self, directory: FakeScratch, content_type: str, *, limit_seconds: float, max_pages: int
    ) -> Any:
        self.pool.calls.append(("convert", content_type, dict(directory.files)))
        directory.given = True
        self.pool.convert_limits.append(limit_seconds)
        if self.pool.conversion is not None:
            return self.pool.conversion
        return outcome(Command.CONVERT, seconds=5), CONVERTED_BYTES

    async def partition(
        self,
        directory: FakeScratch,
        *,
        pages: ChildPages,
        page_count: int,
        parameters: Any,
        limit_seconds: float,
        max_pages: int,
    ) -> tuple[ChildOutcome, bytes | None, ExtractPartV1 | None]:
        """The `partition` child's own work in process, then the parent's checks of its files."""
        self.pool.calls.append(("partition", f"{pages.first}-{pages.last}", dict(directory.files)))
        directory.given = True
        self.pool.partition_limits.append(limit_seconds)
        if self.pool.partitions:
            return self.pool.partitions.popleft(), None, None
        request = ChildRequest(
            command=Command.PARTITION,
            content_type=PDF,
            source_path=SOURCE,
            pages=pages,
            mode=None,
            parameters={**parameters, "pageCount": page_count},
            limits=Limits(max_pages=max_pages, render_max_total_bytes=render_max_total_bytes(0)),
        )
        result, raw = await asyncio.to_thread(_partition_child, request, directory.files[SOURCE])
        if not result.ok or raw is None:
            failed = str(getattr(result, "code", "parse_failed"))
            return outcome(Command.PARTITION, failed, seconds=3), None, None
        try:
            part = parse_partition(raw, page_count=page_count, first=pages.first, last=pages.last)
        except ChildFileError:
            return outcome(Command.PARTITION, "parse_failed", seconds=3), None, None
        return outcome(Command.PARTITION, seconds=3), raw, part

    async def render(
        self, directory: FakeScratch, *, page_count: int, pdf_size: int, max_pages: int, sink: Any
    ) -> RenditionOutcome:
        self.pool.calls.append(("render", str(page_count), dict(directory.files)))
        if self.pool.rendition is not None:
            for number in self.pool.render_uploads:
                await sink.upload_page(number, f"page {number}".encode())
            return self.pool.rendition
        for number in range(1, page_count + 1):
            await sink.upload_page(number, f"page {number}".encode())
        await sink.upload_document(b"whole document")
        return RenditionOutcome(None, page_count, "built", tuple(range(1, page_count + 1)))


def _partition_child(request: ChildRequest, source: bytes) -> tuple[Any, bytes | None]:
    """`parse_worker.run_partition` in a directory holding the parent's `source.bin`; returns
    its result and the `partition.json` it wrote."""
    with tempfile.TemporaryDirectory() as name:
        scratch = Path(name)
        (scratch / SOURCE).write_bytes(source)
        result = parse_worker.run_partition(request, scratch)
        written = scratch / PARTITION
        return result, written.read_bytes() if written.exists() else None


class FakePool:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, bytes]]] = []
        self.scratches: list[FakeScratch] = []
        self.inspections: deque[ChildOutcome] = deque()
        self.convert_limits: list[float] = []
        self.conversion: tuple[ChildOutcome, bytes | None] | None = None
        self.partitions: deque[ChildOutcome] = deque()
        self.partition_limits: list[float] = []
        self.rendition: RenditionOutcome | None = None
        self.render_uploads: list[int] = []
        self.pages = 3
        self.parse_timeout_seconds = 1200
        self.unavailable = False
        self.held = 0
        self.taken = 0

    @asynccontextmanager
    async def slot(self, first_timeout: float) -> AsyncIterator[FakeSlot]:
        if self.unavailable:
            raise ParseUsersUnavailableError
        self.held += 1
        self.taken += 1
        try:
            yield FakeSlot(self)
        finally:
            self.held -= 1

    def commands(self) -> list[str]:
        return [call[0] for call in self.calls]


# --- The real methods' boundaries -----------------------------------------------------------------

_TOKEN: Final = re.compile(r"\w+|[^\w\s]|\s+")
_PAGE_TEXT: Final = re.compile(rb"\(Page (\d+)\)")
TABLE_HTML = (
    "<table><tbody><tr><th>Item</th><th>EUR</th></tr>"
    "<tr><td>Tea</td><td>12</td></tr></tbody></table>"
)
DOCINTEL_ENDPOINT = "https://docintel-test.cognitiveservices.azure.com"
ANALYZE_PATH = "/documentintelligence/documentModels/prebuilt-layout:analyze"
RESULT_PATH = "/documentintelligence/documentModels/prebuilt-layout/analyzeResults/result-1"


class WordTokenizer:
    """One token per word, punctuation mark or whitespace run, decoding exactly; tiktoken's
    encodings aren't reachable here."""

    def __init__(self) -> None:
        self._ids: dict[str, int] = {}
        self._pieces: list[str] = []

    def encode(self, text: str) -> list[int]:
        tokens: list[int] = []
        for piece in _TOKEN.findall(text):
            if piece not in self._ids:
                self._ids[piece] = len(self._pieces)
                self._pieces.append(piece)
            tokens.append(self._ids[piece])
        return tokens

    def decode(self, tokens: Sequence[int]) -> str:
        return "".join(self._pieces[token] for token in tokens)


@dataclass
class FakeLibrary:
    """`unstructured.partition.pdf`, which can't be installed here (it needs `torch`): each page
    of the part `partition_pdf` receives, which the real `pikepdf` copy wrote, is read back for
    the page it draws (`Page n`), and gives a title and a paragraph, and a table on the pages in
    `tables`; `empty` pages give nothing."""

    tables: set[int] = field(default_factory=lambda: {1})
    empty: set[int] = field(default_factory=set)
    calls: list[dict[str, Any]] = field(default_factory=list)

    def partition_pdf(self, **arguments: Any) -> list[Any]:
        self.calls.append(arguments)
        elements: list[Any] = []
        with pikepdf.open(arguments["filename"]) as part:
            for index, page in enumerate(part.pages, start=1):
                found = _PAGE_TEXT.search(page.obj.Contents.read_bytes())
                number = int(found.group(1)) if found else 0
                if number in self.empty:
                    continue
                elements.append(_element("Title", f"Section {number}", index, depth=0))
                elements.append(_element("NarrativeText", f"Text of page {number}.", index))
                if number in self.tables:
                    elements.append(_element("Table", "Tea 12", index, html=TABLE_HTML))
        return elements


def _element(
    category: str, text: str, page: int, *, html: str | None = None, depth: int | None = None
) -> SimpleNamespace:
    return SimpleNamespace(
        category=category,
        text=text,
        metadata=SimpleNamespace(page_number=page, text_as_html=html, category_depth=depth),
    )


class _Credential:
    async def get_token(self, *scopes: str, **options: Any) -> AccessToken:
        del scopes, options
        return AccessToken("token", 4_102_444_800)

    async def close(self) -> None:
        return None


def analyze_result(pages: int = 3) -> dict[str, Any]:
    """A `prebuilt-layout` result of API 2024-11-30: one heading and one paragraph per page."""
    content = ""
    spans: list[dict[str, Any]] = []
    for number in range(1, pages + 1):
        text = f"# Heading {number}\n\nParagraph of page {number}.\n\n"
        spans.append({"offset": len(content), "length": len(text)})
        content += text
    return {
        "apiVersion": "2024-11-30",
        "modelId": "prebuilt-layout",
        "stringIndexType": "unicodeCodePoint",
        "contentFormat": "markdown",
        "content": content,
        "pages": [
            {"pageNumber": number, "angle": 0, "width": 8.5, "height": 11, "unit": "inch",
             "spans": [span]}
            for number, span in enumerate(spans, start=1)
        ],
    }  # fmt: skip


def docintel_exchange(
    method: str, path: str, status: int, json: Any = None, **headers: str
) -> Exchange:
    response: dict[str, Any] = {"status": status, "headers": headers}
    if json is not None:
        response["json"] = json
    return Exchange.from_json({"request": {"method": method, "path": path}, "response": response})


def layout_exchanges(pages: int = 3) -> list[Exchange]:
    """The analysis accepted, polled once as succeeded, then its result deleted."""
    operation = f"{DOCINTEL_ENDPOINT}{RESULT_PATH}?api-version=2024-11-30"
    polled = {
        "status": "succeeded",
        "createdDateTime": "2026-10-31T00:00:00Z",
        "lastUpdatedDateTime": "2026-10-31T00:00:01Z",
        "analyzeResult": analyze_result(pages),
    }
    return [
        docintel_exchange("POST", ANALYZE_PATH, 202, **{"Operation-Location": operation}),
        docintel_exchange("GET", RESULT_PATH, 200, polled),
        docintel_exchange("DELETE", RESULT_PATH, 204),
    ]


class LayoutService(ReplayTransport):
    """The Document Intelligence endpoint the real async client calls: the recorded exchanges,
    answered first by any `refusals` queued; each analyze request records whether a parse slot
    was held while the service analysed. One client and transport serve the session, because
    the client's object graph is large; `reset` gives each test a clean one."""

    def __init__(self) -> None:
        super().__init__(layout_exchanges())
        self.pool = FakePool()
        self.refusals: deque[Exchange] = deque()
        self.slot_held_at_analyze: list[bool] = []

    def reset(self, pool: FakePool) -> None:
        self.pool = pool
        self.refusals.clear()
        self.slot_held_at_analyze.clear()
        self.requests.clear()

    @override
    async def send(self, request: HttpRequest, **kwargs: Any) -> ReplayResponse:
        if request.method == "POST":
            self.slot_held_at_analyze.append(self.pool.held > 0)
        if self.refusals:
            return ReplayResponse(request, self.refusals.popleft().response)
        return await super().send(request, **kwargs)


async def _no_wait(seconds: float) -> None:
    del seconds


@dataclass(frozen=True)
class DocIntel:
    """The layout service's transport and the real async client over it."""

    layout: LayoutService
    client: DocumentIntelligenceClient


def docintel_service() -> DocIntel:
    layout = LayoutService()
    client = DocumentIntelligenceClient(
        DOCINTEL_ENDPOINT,
        _Credential(),  # type: ignore[arg-type]
        transport=layout,
        retry_total=0,
    )
    return DocIntel(layout, client)


def real_methods(
    containers: dict[str, FakeContainer], openai: FakeOpenAI, docintel: DocIntel, clock: Any
) -> MethodRegistry:
    """Both methods as the worker registers them, their ports over this test's fakes: the blob
    store over the in-memory containers, the partition port and the PDF source that Extract
    binds, the real `OpenAISummaryModel` over the fake client, and the real layout call over the
    real `DocumentIntelligenceClient` given `ReplayTransport`."""
    store = BlobStore(lambda name: cast("ContainerClient", containers[name]))
    tokenizer = WordTokenizer()
    summaries = OpenAISummaryModel(cast("openai_module.AsyncOpenAI", openai), "ingest-summary")
    analyzer = DocumentIntelligenceLayout(docintel.client, FakeClock(), _no_wait)

    def pagewise_ports(ctx: MethodContext) -> PagewisePorts:
        del ctx
        return PagewisePorts(
            store=store,
            partitioner=BoundPartitioner(),
            summaries=summaries,
            summary_encoding=TokenEncoding.O200K_BASE,
            tokenizers=lambda encoding: tokenizer,
            clock=clock,
        )

    def recursive_ports(ctx: MethodContext) -> RecursivePorts:
        del ctx
        return RecursivePorts(
            store=store,
            pdf=current_pdf_source(store),
            layout=analyzer,
            tokenizers=lambda encoding: tokenizer,
        )

    registry = MethodRegistry()
    registry.register(PAGEWISE_V1, PagewiseMethod(pagewise_ports))
    registry.register(RECURSIVE_V1, RecursiveMethod(recursive_ports))
    return registry


# --- Methods --------------------------------------------------------------------------------------


class FakeMethod:
    """An implementation of the method interface that records its calls."""

    def __init__(self, *, whole: bool = False) -> None:
        self.whole = whole
        self.extracted: list[tuple[Any, PageRange]] = []
        self.chunked: list[Any] = []
        self.enriched: list[Any] = []
        self.chunk_count = 4
        self.doc_intel_pages = 0
        self.failure: BaseException | None = None

    def extract_plan(self, source: ExtractSource) -> ExtractPlan:
        if self.whole:
            return ExtractPlan.whole_document(source)
        return ExtractPlan.in_parts(source)

    async def extract(self, ctx: Any, params: Any, part: PageRange) -> ExtractResult:
        if self.failure is not None:
            raise self.failure
        self.extracted.append((ctx, part))
        return ExtractResult(doc_intel_pages=self.doc_intel_pages)

    async def enrich(self, ctx: Any, params: Any) -> EnrichResult:
        self.enriched.append(ctx)
        return EnrichResult(done=True)

    async def chunk(self, ctx: Any, params: Any) -> ChunkResult:
        if self.failure is not None:
            raise self.failure
        self.chunked.append(ctx)
        return ChunkResult(chunk_count=self.chunk_count)


# --- Builders -------------------------------------------------------------------------------------

SETTINGS = ItemSettings(
    activity_time_budget_seconds=1200,
    max_pages_per_document=2000,
    pagewise_pages_per_extract=2,
    max_parse_seconds_per_document=7200,
    embedding_batch_max_inputs=2,
    embedding_batch_max_tokens=60_000,
    embedding_concurrency=2,
    index_batch_max_documents=100,
    index_batch_max_bytes=8_000_000,
    table_summary_concurrency=2,
    table_summary_max_input_tokens=6000,
    table_summary_max_output_tokens=300,
)


@dataclass
class Ids:
    item: UUID = field(default_factory=uuid4)
    run: UUID = field(default_factory=uuid4)
    index: UUID = field(default_factory=uuid4)
    document: UUID = field(default_factory=uuid4)
    version: UUID = field(default_factory=uuid4)
    chunk_set: UUID = field(default_factory=uuid4)
    physical: UUID = field(default_factory=uuid4)


def make_plan(
    ids: Ids,
    *,
    content_type: ContentType = ContentType.PDF,
    method: str = "pagewise",
    convert: bool = True,
    converted_sha256: str | None = None,
    summarize: bool = True,
    dimensions: int = 4,
    version_number: int = 1,
) -> BuildPlan:
    conversion = None
    if content_type is not ContentType.PDF:
        conversion = ConversionPlan(
            convert=convert,
            converted_blob_path=converted_path(ids.index, ids.document, version_number),
            converted_sha256=None if convert else (converted_sha256 or sha(CONVERTED_BYTES)),
        )
    if method == "pagewise":
        snapshot = PAGEWISE_V1.snapshot(PagewiseParametersV1(summarize_tables=summarize))
    else:
        snapshot = RECURSIVE_V1.snapshot(RecursiveParametersV1())
    return BuildPlan(
        chunk_set_id=ids.chunk_set,
        index_id=ids.index,
        document_id=ids.document,
        version_id=ids.version,
        version_number=version_number,
        content_type=content_type,
        source_blob_path=source_path(ids.index, ids.document, version_number),
        source_sha256=sha(PDF_BYTES),
        conversion=conversion,
        title="Annual report",
        work_prefix=work_prefix(ids.run, ids.document, version_number),
        physical_index=PhysicalIndexPlan(
            id=ids.physical,
            name="docchat-0123456789abcdef0123456789abcdef-g1",
            is_active=True,
            schema_version=1,
            dimensions=dimensions,
            encoding=TokenEncoding.CL100K_BASE,
            max_input_tokens=8192,
            ingestion_deployment="embed-ingest-te3-large-v1",
        ),
        method=snapshot,
    )


def envelope(ids: Ids, *, claim: int = 1, settings: ItemSettings = SETTINGS) -> dict[str, Any]:
    return {
        "itemId": str(ids.item),
        "claimNumber": claim,
        "runId": str(ids.run),
        "traceparent": TRACEPARENT,
        "tracestate": None,
        "settings": settings.model_dump(mode="json"),
    }


def plan_payload(ids: Ids, plan: BuildPlan, **extra: Any) -> dict[str, Any]:
    return {**envelope(ids), "plan": plan.model_dump(mode="json"), **extra}


@dataclass
class World:
    """Every fake of one test, and the services built from them; the real methods are
    registered, and `fake_methods` swaps in `FakeMethod`s for an activity's own rules."""

    sql: FakeSql
    documents: FakeContainer
    renditions: FakeContainer
    work: FakeContainer
    search: FakeSearch
    openai: FakeOpenAI
    pool: FakePool
    layout: LayoutService
    services: DocumentServices
    log: list[tuple[str, str, str]]
    clock: Any

    async def provider(self) -> DocumentServices:
        return self.services

    def fake_methods(self) -> tuple[FakeMethod, FakeMethod]:
        pagewise, recursive = FakeMethod(), FakeMethod(whole=True)
        registry = MethodRegistry()
        registry.register(PAGEWISE_V1, pagewise)
        registry.register(RECURSIVE_V1, recursive)
        self.services = dataclasses.replace(self.services, methods=registry)
        return pagewise, recursive


class StepClock:
    """A monotonic clock a test moves; `now` for the `Clock` port."""

    def __init__(self) -> None:
        self.value = 1000.0
        self.step = 0.0

    def monotonic(self) -> float:
        self.value += self.step
        return self.value

    def now(self) -> datetime:
        return datetime(2026, 1, 1, tzinfo=UTC)


def make_world(docintel: DocIntel | None = None) -> World:
    service = docintel or docintel_service()
    log: list[tuple[str, str, str]] = []
    sql = FakeSql()
    documents = FakeContainer("documents", log)
    renditions = FakeContainer("renditions", log)
    work = FakeContainer("ingestion-work", log)
    search = FakeSearch()
    openai = FakeOpenAI()
    pool = FakePool()
    layout = service.layout
    layout.reset(pool)
    clock = StepClock()
    containers = {container.name: container for container in (documents, renditions, work)}
    registry = real_methods(containers, openai, service, clock)
    services = DocumentServices(
        sql=sql,
        documents=documents,
        renditions=renditions,
        work=work,
        search=lambda name: search,
        openai=openai,
        parse_pool=pool,  # type: ignore[arg-type]
        methods=registry,
        clock=clock,
        rendition_rules_version=RULES_VERSION,
        converter_version=CONVERTER,
    )
    return World(
        sql,
        documents,
        renditions,
        work,
        search,
        openai,
        pool,
        layout,
        services,
        log,
        clock,
    )


def metric_points(exporters: TestExporters, name: str) -> list[Any]:
    """Every data point the in-memory pipeline holds for a metric."""
    data = exporters.metrics.get_metrics_data()
    if data is None:
        return []
    return [
        point
        for resource in data.resource_metrics
        for scope in resource.scope_metrics
        for metric in scope.metrics
        if metric.name == name
        for point in metric.data.data_points
    ]


@pytest.fixture
def library(monkeypatch: pytest.MonkeyPatch) -> FakeLibrary:
    """The fake `unstructured.partition.pdf` the page-wise `partition_part` imports."""
    fake = FakeLibrary()
    monkeypatch.setitem(sys.modules, partition.PARTITION_MODULE, fake)
    return fake


@pytest.fixture(scope="session")
def docintel() -> DocIntel:
    return docintel_service()


@pytest.fixture
def world(library: FakeLibrary, docintel: DocIntel) -> World:
    del library
    return make_world(docintel)


@pytest.fixture
def kit() -> SimpleNamespace:
    """The builders and fakes, for test modules that can't import this conftest."""
    return SimpleNamespace(
        Ids=Ids,
        Result=Result,
        Row=Row,
        ScriptedConnection=ScriptedConnection,
        make_plan=make_plan,
        envelope=envelope,
        plan_payload=plan_payload,
        fence_ok=fence_ok,
        build_facts=build_facts,
        outcome=outcome,
        completion=completion,
        sha=sha,
        metric_points=metric_points,
        pdf_bytes=pdf_bytes,
        docintel_exchange=docintel_exchange,
        ANALYZE_PATH=ANALYZE_PATH,
        TABLE_HTML=TABLE_HTML,
        PDF_BYTES=PDF_BYTES,
        CONVERTED_BYTES=CONVERTED_BYTES,
        CONVERTER=CONVERTER,
        RULES_VERSION=RULES_VERSION,
        SETTINGS=SETTINGS,
        TRACEPARENT=TRACEPARENT,
        WordTokenizer=WordTokenizer,
        make_world=make_world,
    )
