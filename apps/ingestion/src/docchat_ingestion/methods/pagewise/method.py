"""`PagewiseMethod`, the page-wise implementation of `IngestionMethod` (`14-ingestion.md` sections
6.3 to 6.5, 10 and 12; ADR 0047).

Extract runs the `partition` child on one part of the version's PDF through the `Partitioner`
port, which the Extract activity binds to the parse slot whose first user ran that PDF's
`inspect`, and uploads the bytes the parent read and validated as the part's work file. Enrich
summarises the tables; Chunk turns the parts and the summaries into the chunks file. Every port
comes from the resolver the method was built with, called with the item's context, so nothing per
item lives on the method.

`IMPLEMENTATION` is the instance `docchat_ingestion.function_app` registers: its ports come from
the worker process (`process_ports`), and its partition port is the one the Extract activity
binds around its call with `bound_partitioner`, because that port belongs to the activity's parse
slot.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Final, Protocol

from pydantic import JsonValue

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
    PagewiseParametersV1,
)
from docchat_core.ingestion.names import (
    BlobContainer,
    chunks_path,
    extract_part_path,
    table_summaries_path,
)
from docchat_core.ingestion.settings import TokenEncoding
from docchat_core.models import Clock, SystemClock
from docchat_ingestion.isolation.results import ChildFileError, ExtractPartV1, parse_partition
from docchat_ingestion.methods.chunks import check_chunk_count, chunk_lines, encode_chunks_file
from docchat_ingestion.methods.pagewise.chunker import chunk_pages
from docchat_ingestion.methods.pagewise.summaries import (
    OpenAISummaryModel,
    SummaryModel,
    TableSummariser,
    summary_texts,
    table_jobs,
)
from docchat_ingestion.methods.process import app_settings, process_store
from docchat_ingestion.methods.store import WorkStore
from docchat_ingestion.methods.text import TokenizerSource, load_tokenizer
from docchat_ingestion.workfiles import (
    TableSummaryLine,
    WorkFileError,
    decode_table_summaries,
    encode_table_summaries,
)

WORK = BlobContainer.INGESTION_WORK


class Partitioner(Protocol):
    """Runs the `partition` child on pages `part` of the PDF the build reads, under the parse
    slot of the Extract call, counting its seconds against the build's parse budget. Returns the
    `partition.json` bytes the parent read and validated against the version's page count and
    the part; raises `MethodFailedError` with the child's code (`parse_timeout`,
    `parse_memory_exceeded`, `parse_failed`, `corrupt_document`)."""

    async def partition(
        self, ctx: MethodContext, part: PageRange, parameters: Mapping[str, JsonValue]
    ) -> bytes: ...


@dataclass(frozen=True, slots=True)
class PagewisePorts:
    """What the page-wise method calls: the blob store, the partition port, the table-summary
    deployment and its encoding (`DOCCHAT_OPENAI_TABLE_SUMMARY_ENCODING`), the tokenizers and a
    clock for Enrich's time budget."""

    store: WorkStore
    partitioner: Partitioner
    summaries: SummaryModel
    summary_encoding: TokenEncoding
    tokenizers: TokenizerSource = load_tokenizer
    clock: Clock = field(default_factory=SystemClock)


type PagewisePortResolver = Callable[[MethodContext], PagewisePorts]


def _parameters(params: MethodParameters) -> PagewiseParametersV1:
    if not isinstance(params, PagewiseParametersV1):
        raise TypeError("the page-wise method takes PagewiseParametersV1")
    return params


def _plan(ctx: MethodContext) -> ExtractPlan:
    return ExtractPlan.in_parts(
        ExtractSource(ctx.page_count, ctx.settings.pagewise_pages_per_extract)
    )


class PagewiseMethod:
    """`pagewise` version 1."""

    def __init__(self, ports: PagewisePortResolver) -> None:
        self._ports = ports

    def extract_plan(self, source: ExtractSource) -> ExtractPlan:
        """Parts of `pages_per_extract` pages (section 6.3 step 7)."""
        return ExtractPlan.in_parts(source)

    async def extract(
        self, ctx: MethodContext, params: MethodParameters, part: PageRange
    ) -> ExtractResult:
        """Partition one part and write `extract/part-{first:05d}-{last:05d}.json`."""
        parameters = _parameters(params)
        if ctx.page_count > ctx.settings.max_pages_per_document:
            raise MethodFailedError("page_limit_exceeded")
        if part not in _plan(ctx).parts:
            raise ValueError("a page-wise part is one of the plan's parts")
        ports = self._ports(ctx)
        raw = await ports.partitioner.partition(ctx, part, parameters.dumped())
        path = extract_part_path(ctx.work_prefix, part.first_page, part.last_page)
        await ports.store.write(WORK, path, raw)
        return ExtractResult()

    async def _parts(self, ctx: MethodContext, store: WorkStore) -> list[ExtractPartV1]:
        """Exactly the parts the plan gives for the version's page count and the item's part
        size, so files an earlier claim wrote with other ranges are ignored."""
        parts: list[ExtractPartV1] = []
        for part in _plan(ctx).parts:
            raw = await store.read(
                WORK, extract_part_path(ctx.work_prefix, part.first_page, part.last_page)
            )
            if raw is None:
                raise WorkFileError
            try:
                parts.append(
                    await asyncio.to_thread(
                        parse_partition,
                        raw,
                        page_count=ctx.page_count,
                        first=part.first_page,
                        last=part.last_page,
                    )
                )
            except ChildFileError:
                raise WorkFileError from None
        return parts

    async def _saved_summaries(
        self, ctx: MethodContext, store: WorkStore
    ) -> list[TableSummaryLine]:
        raw = await store.read(WORK, table_summaries_path(ctx.work_prefix))
        return await asyncio.to_thread(decode_table_summaries, raw)

    async def enrich(self, ctx: MethodContext, params: MethodParameters) -> EnrichResult:
        """The table summaries (section 6.4); done at once when `summarizeTables` is false."""
        if not _parameters(params).summarize_tables:
            return EnrichResult(done=True)
        ports = self._ports(ctx)
        parts = await self._parts(ctx, ports.store)
        jobs = table_jobs(parts)
        saved = await self._saved_summaries(ctx, ports.store)
        tokenizer = await asyncio.to_thread(ports.tokenizers, ports.summary_encoding)
        path = table_summaries_path(ctx.work_prefix)

        async def save(lines: Sequence[TableSummaryLine]) -> None:
            data = await asyncio.to_thread(encode_table_summaries, lines)
            await ports.store.write(WORK, path, data)

        summariser = TableSummariser(ports.summaries, tokenizer, ctx.settings, ports.clock, save)
        return await summariser.run(jobs, saved)

    async def chunk(self, ctx: MethodContext, params: MethodParameters) -> ChunkResult:
        """One chunk per page into `chunks/{chunkSetId}.jsonl` (section 6.5)."""
        parameters = _parameters(params)
        ports = self._ports(ctx)
        parts = await self._parts(ctx, ports.store)
        summaries: dict[str, str] = {}
        if parameters.summarize_tables:
            summaries = summary_texts(await self._saved_summaries(ctx, ports.store))

        def build() -> tuple[bytes, int]:
            tokenizer = ports.tokenizers(ctx.encoding)
            chunks = chunk_pages(parts, summaries, tokenizer)
            check_chunk_count(len(chunks))
            return encode_chunks_file(ctx, chunk_lines(chunks, tokenizer)), len(chunks)

        data, count = await asyncio.to_thread(build)
        await ports.store.write(WORK, chunks_path(ctx.work_prefix, ctx.chunk_set_id), data)
        return ChunkResult(chunk_count=count)


# --- The worker process's instance ----------------------------------------------------------------

_BOUND_PARTITIONER: Final[ContextVar[Partitioner | None]] = ContextVar(
    "docchat_pagewise_partitioner", default=None
)


class UnboundPartitionerError(RuntimeError):
    """A page-wise Extract ran outside `bound_partitioner`: a programming error."""

    def __init__(self) -> None:
        super().__init__("no partition port is bound for this Extract call")


@contextmanager
def bound_partitioner(partitioner: Partitioner) -> Iterator[None]:
    """Bind the Extract call's partition port (its parse slot, a fresh scratch directory holding
    the bytes of the PDF it inspected, its parse budget) for the method calls inside the block.
    The binding is a context variable, so concurrent activities of one process each see their
    own."""
    token = _BOUND_PARTITIONER.set(partitioner)
    try:
        yield
    finally:
        _BOUND_PARTITIONER.reset(token)


class BoundPartitioner:
    """The partition port bound for the current call (`bound_partitioner`), looked up when
    Extract partitions, so Enrich and Chunk need none."""

    async def partition(
        self, ctx: MethodContext, part: PageRange, parameters: Mapping[str, JsonValue]
    ) -> bytes:
        partitioner = _BOUND_PARTITIONER.get()
        if partitioner is None:
            raise UnboundPartitionerError
        return await partitioner.partition(ctx, part, parameters)


def process_ports(ctx: MethodContext) -> PagewisePorts:
    """The ports of the worker process: the open registry's blob containers and `AsyncOpenAI`
    client, the table-summary deployment and encoding from the app settings, and the bound
    partition port."""
    del ctx
    settings = app_settings()
    return PagewisePorts(
        store=process_store(),
        partitioner=BoundPartitioner(),
        summaries=OpenAISummaryModel(
            ClientRegistry.current().openai, settings.openai_table_summary_deployment
        ),
        summary_encoding=settings.openai_table_summary_encoding,
    )


IMPLEMENTATION: Final = PagewiseMethod(process_ports)
"""`pagewise` version 1 as the worker process runs it."""
