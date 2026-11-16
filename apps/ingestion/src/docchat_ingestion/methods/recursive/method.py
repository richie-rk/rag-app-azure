"""`RecursiveMethod`, the recursive implementation of `IngestionMethod` (`14-ingestion.md` sections
6.3 to 6.5 and 11; ADR 0048).

Extract sends the whole PDF the build reads to the layout service in one call, after the
Extract activity's `inspect` child has checked it and counted its pages, and writes
`extract/document.json`; Enrich has nothing to do; Chunk runs only on a complete extract file.
Every port comes from the resolver the method was built with, called with the item's context.

`IMPLEMENTATION` is the instance `docchat_ingestion.function_app` registers: its ports come from
the worker process (`process_ports`). It downloads the PDF the build reads unless the Extract
activity binds the copy it already downloaded and inspected with `bound_pdf_source`.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Final

from docchat_core.clients import ClientRegistry
from docchat_core.ingestion.methods import (
    ChunkResult,
    EnrichResult,
    ExtractPlan,
    ExtractResult,
    ExtractSource,
    MethodContext,
    MethodFailedError,
    MethodParameters,
    PageRange,
    RecursiveParametersV1,
)
from docchat_core.ingestion.names import BlobContainer, chunks_path, extract_document_path
from docchat_core.models import SystemClock
from docchat_ingestion import telemetry
from docchat_ingestion.methods.chunks import check_chunk_count, chunk_lines, encode_chunks_file
from docchat_ingestion.methods.process import process_store
from docchat_ingestion.methods.recursive.chunker import chunk_document
from docchat_ingestion.methods.recursive.layout import (
    DocumentIntelligenceLayout,
    LayoutAnalyzer,
    extract_document,
)
from docchat_ingestion.methods.store import BlobPdfSource, BlobStore, PdfSource, WorkStore
from docchat_ingestion.methods.text import TokenizerSource, load_tokenizer
from docchat_ingestion.workfiles import ExtractDocument, WorkFileError, decode_extract_document

WORK = BlobContainer.INGESTION_WORK


@dataclass(frozen=True, slots=True)
class RecursivePorts:
    """What the recursive method calls: the blob store, the PDF the build reads, the layout
    service and the tokenizers."""

    store: WorkStore
    pdf: PdfSource
    layout: LayoutAnalyzer
    tokenizers: TokenizerSource = load_tokenizer


type RecursivePortResolver = Callable[[MethodContext], RecursivePorts]


def _parameters(params: MethodParameters) -> RecursiveParametersV1:
    if not isinstance(params, RecursiveParametersV1):
        raise TypeError("the recursive method takes RecursiveParametersV1")
    return params


def _encode(document: ExtractDocument) -> bytes:
    return document.encode()


class RecursiveMethod:
    """`recursive` version 1."""

    def __init__(self, ports: RecursivePortResolver) -> None:
        self._ports = ports

    def extract_plan(self, source: ExtractSource) -> ExtractPlan:
        """The whole document in one part: one layout call per document."""
        return ExtractPlan.whole_document(source)

    async def extract(
        self, ctx: MethodContext, params: MethodParameters, part: PageRange
    ) -> ExtractResult:
        """Analyse the PDF and write `extract/document.json`; `doc_intel_pages` is the number of
        pages the service analysed and billed."""
        _parameters(params)
        if ctx.page_count > ctx.settings.max_pages_per_document:
            raise MethodFailedError("page_limit_exceeded")
        if part != PageRange(1, ctx.page_count):
            raise ValueError("the recursive method extracts the whole document")
        ports = self._ports(ctx)
        async with ports.pdf.open(ctx) as stream:
            result = await ports.layout.analyze(stream)
        document = extract_document(result, ctx.page_count)
        data = await asyncio.to_thread(_encode, document)
        await ports.store.write(WORK, extract_document_path(ctx.work_prefix), data)
        telemetry.record_docintel_pages(document.doc_intel_pages)
        return ExtractResult(doc_intel_pages=document.doc_intel_pages)

    async def enrich(self, ctx: MethodContext, params: MethodParameters) -> EnrichResult:
        """The recursive method makes no model call besides embeddings."""
        del ctx
        _parameters(params)
        return EnrichResult(done=True)

    async def chunk(self, ctx: MethodContext, params: MethodParameters) -> ChunkResult:
        """Chunk the extract file into `chunks/{chunkSetId}.jsonl` (section 6.5)."""
        parameters = _parameters(params)
        ports = self._ports(ctx)
        raw = await ports.store.read(WORK, extract_document_path(ctx.work_prefix))
        if raw is None:
            raise WorkFileError

        def build() -> tuple[bytes, int]:
            document = decode_extract_document(raw, ctx.page_count)
            tokenizer = ports.tokenizers(ctx.encoding)
            chunks = chunk_document(document, parameters, tokenizer)
            check_chunk_count(len(chunks))
            return encode_chunks_file(ctx, chunk_lines(chunks, tokenizer)), len(chunks)

        data, count = await asyncio.to_thread(build)
        await ports.store.write(WORK, chunks_path(ctx.work_prefix, ctx.chunk_set_id), data)
        return ChunkResult(chunk_count=count)


# --- The worker process's instance ----------------------------------------------------------------

_BOUND_PDF: Final[ContextVar[PdfSource | None]] = ContextVar("docchat_recursive_pdf", default=None)


@contextmanager
def bound_pdf_source(source: PdfSource) -> Iterator[None]:
    """Give the layout call the Extract activity's own copy of the PDF for the method calls
    inside the block, instead of a second download. The binding is a context variable, so
    concurrent activities of one process each see their own."""
    token = _BOUND_PDF.set(source)
    try:
        yield
    finally:
        _BOUND_PDF.reset(token)


def current_pdf_source(store: BlobStore) -> PdfSource:
    """The PDF source bound for this call (`bound_pdf_source`), or else a checked download."""
    return _BOUND_PDF.get() or BlobPdfSource(store)


def process_ports(ctx: MethodContext) -> RecursivePorts:
    """The ports of the worker process: the open registry's blob containers and its
    `DocumentIntelligenceClient`, and the PDF source bound for this call or a checked download."""
    del ctx
    store = process_store()
    return RecursivePorts(
        store=store,
        pdf=current_pdf_source(store),
        layout=DocumentIntelligenceLayout(
            ClientRegistry.current().document_intelligence, SystemClock(), asyncio.sleep
        ),
    )


IMPLEMENTATION: Final = RecursiveMethod(process_ports)
"""`recursive` version 1 as the worker process runs it."""
