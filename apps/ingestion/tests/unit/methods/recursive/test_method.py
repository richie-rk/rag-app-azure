"""Unit tests of `RecursiveMethod` (T-183; `14-ingestion.md` sections 6.3 to 6.5 and 11;
ADR 0048).

The layout results recorded from the service for the four fixture documents can't be recorded on
this machine (no Azure), so these tests run on a hand-written `AnalyzeResult` of the 2024-11-30
schema. The T-183 fixture tests compare the chunks of each recorded result,
`tests/fixtures/documents/<file>.layout.json` (the `analyzeResult` object, offsets in Unicode code
points), with the `recursive` key of `<file>.expected.json` (the chunk lines of the chunks file),
using `cl100k_base`; each missing prerequisite skips with its reason.
"""

from __future__ import annotations

import importlib
import io
import json
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import IO, Any

import pytest
from azure.ai.documentintelligence.models import AnalyzeResult

from docchat_core.clients import ClientRegistry
from docchat_core.ingestion.methods import (
    RECURSIVE_V1,
    ExtractPlan,
    ExtractSource,
    IngestionMethod,
    MethodContext,
    MethodFailedError,
    MethodRegistry,
    PageRange,
    PagewiseParametersV1,
    RecursiveParametersV1,
)
from docchat_core.ingestion.names import BlobContainer, chunks_path, extract_document_path
from docchat_core.ingestion.settings import TokenEncoding
from docchat_ingestion.methods.recursive import method as recursive_method
from docchat_ingestion.methods.recursive.layout import (
    DocumentIntelligenceLayout,
)
from docchat_ingestion.methods.recursive.method import RecursiveMethod, RecursivePorts
from docchat_ingestion.methods.store import BlobPdfSource, WorkStore
from docchat_ingestion.methods.text import TiktokenTokenizer, Tokenizer
from docchat_ingestion.workfiles import ExtractDocument, WorkFileError, decode_chunks

type ContextFactory = Callable[..., MethodContext]
type MetricPoints = Callable[[str], list[Any]]
WORK = BlobContainer.INGESTION_WORK
FIXTURES = Path(__file__).resolve().parents[6] / "tests" / "fixtures" / "documents"

CONTENT = (
    "# Annual report\n\n"
    "The year closed with growth in every region. Revenue rose by a tenth.\n\n"
    "<!-- PageBreak -->\n\n"
    "## Regions\n\n"
    "<table><tr><th>Region</th><th>Revenue</th></tr><tr><td>North</td><td>12</td></tr></table>\n"
)
ANALYZE_RESULT: dict[str, Any] = {
    "apiVersion": "2024-11-30",
    "modelId": "prebuilt-layout",
    "stringIndexType": "unicodeCodePoint",
    "contentFormat": "markdown",
    "content": CONTENT,
    "pages": [
        {"pageNumber": 1, "spans": [{"offset": 0, "length": CONTENT.index("<!--") - 2}]},
        {"pageNumber": 2, "spans": [{"offset": CONTENT.index("## Regions"),
                                     "length": len(CONTENT) - CONTENT.index("## Regions")}]},
    ],
}  # fmt: skip


class _Layout:
    def __init__(self, result: AnalyzeResult | Exception) -> None:
        self.result = result
        self.received: list[bytes] = []

    async def analyze(self, pdf: IO[bytes]) -> AnalyzeResult:
        self.received.append(pdf.read())
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class _Pdf:
    def __init__(self) -> None:
        self.opened: list[MethodContext] = []
        self.closed = False

    @asynccontextmanager
    async def _open(self, ctx: MethodContext) -> AsyncIterator[IO[bytes]]:
        self.opened.append(ctx)
        try:
            yield io.BytesIO(b"%PDF-1.7 version bytes")
        finally:
            self.closed = True

    def open(self, ctx: MethodContext) -> AbstractAsyncContextManager[IO[bytes]]:
        return self._open(ctx)


def method_for(
    store: WorkStore, tokenizer: Tokenizer, layout: _Layout | None = None, pdf: _Pdf | None = None
) -> RecursiveMethod:
    ports = RecursivePorts(
        store=store,
        pdf=pdf or _Pdf(),
        layout=layout or _Layout(AnalyzeResult(ANALYZE_RESULT)),
        tokenizers=lambda encoding: tokenizer,
    )
    return RecursiveMethod(lambda ctx: ports)


PARAMETERS = RecursiveParametersV1()


def test_the_plan_is_the_whole_document(store: WorkStore, tokenizer: Tokenizer) -> None:
    plan = method_for(store, tokenizer).extract_plan(
        ExtractSource(page_count=85, pages_per_extract=40)
    )

    assert plan == ExtractPlan(parts=(PageRange(1, 85),))


async def test_extract_analyses_the_pdf_and_writes_the_document(
    store: WorkStore,
    tokenizer: Tokenizer,
    make_context: ContextFactory,
    metric_points: MetricPoints,
) -> None:
    ctx = make_context(page_count=2)
    layout, pdf = _Layout(AnalyzeResult(ANALYZE_RESULT)), _Pdf()

    result = await method_for(store, tokenizer, layout, pdf).extract(
        ctx, PARAMETERS, PageRange(1, 2)
    )

    assert result.doc_intel_pages == 2
    assert layout.received == [b"%PDF-1.7 version bytes"]
    assert pdf.opened == [ctx]
    assert pdf.closed
    raw = await store.read(WORK, extract_document_path(ctx.work_prefix))
    assert raw is not None
    written = json.loads(raw)
    assert written["markdown"] == CONTENT
    assert written["pageOffsets"][1]["pageNumber"] == 2
    assert written["docIntelPages"] == 2
    assert sum(point.value for point in metric_points("docchat.ingestion.docintel.pages")) == 2


async def test_extract_refusals(
    store: WorkStore, tokenizer: Tokenizer, make_context: ContextFactory
) -> None:
    failing = method_for(
        store, tokenizer, _Layout(MethodFailedError("docintel_throttled", retry_after_seconds=9))
    )

    with pytest.raises(MethodFailedError) as capped:
        await failing.extract(make_context(page_count=2001), PARAMETERS, PageRange(1, 2001))
    assert capped.value.code == "page_limit_exceeded"
    with pytest.raises(ValueError, match="whole document"):
        await failing.extract(make_context(page_count=3), PARAMETERS, PageRange(1, 2))
    with pytest.raises(TypeError):
        await failing.extract(make_context(page_count=3), PagewiseParametersV1(), PageRange(1, 3))
    ctx = make_context(page_count=3)
    with pytest.raises(MethodFailedError) as throttled:
        await failing.extract(ctx, PARAMETERS, PageRange(1, 3))
    assert (throttled.value.code, throttled.value.retry_after_seconds) == ("docintel_throttled", 9)
    assert await store.read(WORK, extract_document_path(ctx.work_prefix)) is None


async def test_enrich_has_nothing_to_do(
    store: WorkStore, tokenizer: Tokenizer, make_context: ContextFactory
) -> None:
    result = await method_for(store, tokenizer).enrich(make_context(), PARAMETERS)

    assert result.done
    assert (result.tables, result.input_tokens) == (0, 0)


async def test_chunk_writes_the_chunks_with_their_pages(
    store: WorkStore, tokenizer: Tokenizer, make_context: ContextFactory
) -> None:
    ctx = make_context(page_count=2)
    method = method_for(store, tokenizer)
    await method.extract(ctx, PARAMETERS, PageRange(1, 2))

    result = await method.chunk(ctx, PARAMETERS)

    raw = await store.read(WORK, chunks_path(ctx.work_prefix, ctx.chunk_set_id))
    assert raw is not None
    decoded = decode_chunks(raw)
    header, lines = decoded.header, decoded.chunks
    assert result.chunk_count == header.chunk_count == 2
    assert [(line.page_number, line.heading_path) for line in lines] == [
        (1, ["Annual report"]),
        (2, ["Annual report", "Regions"]),
    ]
    assert lines[0].chunk_text.startswith("Annual report\n\nThe year closed")
    assert lines[1].chunk_text.startswith("Regions\n\n<table><tbody><tr><th>Region</th>")


async def test_chunk_runs_only_on_a_complete_extract_file(
    store: WorkStore, tokenizer: Tokenizer, make_context: ContextFactory
) -> None:
    ctx = make_context(page_count=2)
    method = method_for(store, tokenizer)
    path = extract_document_path(ctx.work_prefix)

    with pytest.raises(WorkFileError):
        await method.chunk(ctx, PARAMETERS)
    await store.write(WORK, path, b'{"formatVersion": 1}')
    with pytest.raises(WorkFileError):
        await method.chunk(ctx, PARAMETERS)
    other = ExtractDocument(
        format_version=1,
        method="recursive",
        page_count=5,
        markdown="x",
        page_offsets=[],
        doc_intel_pages=5,
    )
    await store.write(WORK, path, other.model_dump_json().encode("utf-8"))
    with pytest.raises(WorkFileError):
        await method.chunk(ctx, PARAMETERS)


async def test_a_document_without_text_fails_with_no_extractable_text(
    store: WorkStore, tokenizer: Tokenizer, make_context: ContextFactory
) -> None:
    ctx = make_context(page_count=1)
    empty = ExtractDocument(
        format_version=1,
        method="recursive",
        page_count=1,
        markdown="<!-- PageBreak -->\n",
        page_offsets=[],
        doc_intel_pages=1,
    )
    await store.write(
        WORK, extract_document_path(ctx.work_prefix), empty.model_dump_json().encode()
    )

    with pytest.raises(MethodFailedError) as failed:
        await method_for(store, tokenizer).chunk(ctx, PARAMETERS)

    assert failed.value.code == "no_extractable_text"


# --- T-183 on the four recorded layout results ----------------------------------------------------

FIXTURE_DOCUMENTS = ("tables.pdf", "scanned.pdf", "headings.docx", "slides.pptx")


def _fixture(name: str, suffix: str) -> Path:
    path = FIXTURES / f"{name}{suffix}"
    if not path.is_file():
        pytest.skip(
            f"{path.name} isn't in tests/fixtures/documents: the recorded layout results and the "
            "expected chunks need the Document Intelligence service, which this machine can't reach"
        )
    return path


@pytest.mark.parametrize("name", FIXTURE_DOCUMENTS)
async def test_t183_the_recorded_layout_results_match_their_expectations(
    name: str, store: WorkStore, make_context: ContextFactory
) -> None:
    layout = json.loads(_fixture(name, ".layout.json").read_text(encoding="utf-8"))
    expected = json.loads(_fixture(name, ".expected.json").read_text(encoding="utf-8"))
    if "recursive" not in expected:
        pytest.skip(f"{name}.expected.json holds no recursive expectation")
    try:
        tokenizer = TiktokenTokenizer(TokenEncoding.CL100K_BASE)
    except Exception as failure:  # noqa: BLE001 - any loading failure means no cached encoding
        pytest.skip(f"the cl100k_base encoding isn't available here: {type(failure).__name__}")
    result = AnalyzeResult(layout.get("analyzeResult", layout))
    assert result.string_index_type == "unicodeCodePoint"
    page_count = len(result.pages or [])
    ctx = make_context(page_count=page_count)
    method = method_for(store, tokenizer, _Layout(result))

    await method.extract(ctx, PARAMETERS, PageRange(1, page_count))
    await method.chunk(ctx, PARAMETERS)

    raw = await store.read(WORK, chunks_path(ctx.work_prefix, ctx.chunk_set_id))
    assert raw is not None
    lines = decode_chunks(raw).chunks
    assert [line.model_dump(mode="json") for line in lines] == expected["recursive"]
    assert all(1 <= line.page_number <= page_count for line in lines)
    assert all(line.token_count <= 512 + 128 or "<table" in line.chunk_text for line in lines)


# --- The worker process's instance ----------------------------------------------------------------


def test_the_package_exposes_the_instance_the_function_app_registers() -> None:
    package = importlib.import_module("docchat_ingestion.methods.recursive")
    registry = MethodRegistry()

    registry.register(RECURSIVE_V1, package.IMPLEMENTATION)

    assert registry.get("recursive", 1).implementation is recursive_method.IMPLEMENTATION
    assert isinstance(package.IMPLEMENTATION, IngestionMethod)


@pytest.fixture
def worker(monkeypatch: pytest.MonkeyPatch, store: WorkStore) -> None:
    """The open registry of a worker process, as a fake."""
    registry = SimpleNamespace(document_intelligence=object(), blob_container=lambda name: None)
    monkeypatch.setattr(ClientRegistry, "_open", registry)
    monkeypatch.setattr(recursive_method, "process_store", lambda: store)


@pytest.mark.usefixtures("worker")
def test_the_process_ports_download_unless_a_copy_is_bound(
    store: WorkStore, make_context: ContextFactory
) -> None:
    ports = recursive_method.process_ports(make_context())

    assert ports.store is store
    assert isinstance(ports.pdf, BlobPdfSource)
    assert isinstance(ports.layout, DocumentIntelligenceLayout)
    copy = _Pdf()
    with recursive_method.bound_pdf_source(copy):
        assert recursive_method.process_ports(make_context()).pdf is copy
    assert isinstance(recursive_method.process_ports(make_context()).pdf, BlobPdfSource)
