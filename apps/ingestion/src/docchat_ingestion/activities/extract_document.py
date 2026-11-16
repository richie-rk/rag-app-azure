"""`extract_document`, the Extract step (`14-ingestion.md` sections 6.3, 9.1, 15.6, 15.7 and
15.11; ADRs 0043, 0046, 0049 and 0152).

Every call parses the version's PDF: the source of a PDF version, or the PDF converted from a
DOCX or PPTX source; no call parses an Office file for its text. A call takes a parse slot,
downloads that PDF into a fresh scratch directory, checks it with an `inspect` child, applies the
page cap and calls the method's `extract` on its part, with the method's ports bound to this
call: page-wise partitions the part through the `Partitioner` port, which runs the `partition`
child under the slot's first user in a second fresh directory whose `source.bin` the parent wrote
in the same download, so the child reads bytes no other child touched; recursive sends the PDF
of that directory to the layout service through `bound_pdf_source`, so the bytes are downloaded
once. The first call of a DOCX or PPTX version parses nothing: it converts the source with a
`convert` child, or reuses the stored converted PDF, uploads the PDF to
`ingestion-work/{workPrefix}convert/converted.pdf`, records its hash and page count, and returns
the method's first part, so the conversion and the first parse each get the whole deadline.

The parse budget belongs to the build: the wall-clock seconds of every child Extract starts
(`inspect`, `convert` and `partition`), failed attempts included, are added to
`chunk_sets.parse_seconds_used`, and a `convert` or `partition` child starts only while at least
one second of `maxParseSecondsPerDocument` is left, with a wall clock of min(its own limit, what
is left) (section 15.7). The partition port runs its child through the slot the call already
holds and takes no slot of its own, so one parse slot per process can't deadlock. The layout
call starts no child, so the recursive method's PDF source gives the slot back to the pool before
the call, and other documents' children run while the service analyses this one.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator, Mapping
from contextlib import AbstractAsyncContextManager, AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from typing import IO, Final, Literal
from uuid import UUID

from opentelemetry.trace import Span
from pydantic import Field, JsonValue
from sqlalchemy import Connection, update

from docchat_core.ingestion.methods import (
    ExtractPlan,
    ExtractSource,
    MethodContext,
    MethodFailedError,
    PageRange,
)
from docchat_core.ingestion.models import ActivityResult, ItemStage, ItemWarning
from docchat_core.sql.tables.ingestion import chunk_sets, document_versions
from docchat_ingestion import telemetry
from docchat_ingestion.activities.documents import (
    DEADLINES,
    ActivityOutput,
    BuildFacts,
    BuildPlan,
    DocumentServices,
    ItemFailure,
    PartRange,
    PlanInput,
    ServicesProvider,
    blueprint,
    document_services,
    download_into,
    fence_and_read,
    item_ids,
    method_context,
    read_blob,
    registered,
    run_document_activity,
    write_blob,
)
from docchat_ingestion.activities.support import literal
from docchat_ingestion.isolation.convert import CONVERT_TIMEOUT_SECONDS
from docchat_ingestion.isolation.results import PDF, SOURCE, InspectOk
from docchat_ingestion.isolation.results import PageRange as ChildPages
from docchat_ingestion.isolation.runner import (
    INSPECT_TIMEOUT_SECONDS,
    ChildOutcome,
    ParsePool,
    ParseSlot,
    ParseUsersUnavailableError,
    Scratch,
)
from docchat_ingestion.methods.pagewise.method import bound_partitioner
from docchat_ingestion.methods.recursive.method import bound_pdf_source

NAME: Final = "extract_document"
DEADLINE_SECONDS: Final = DEADLINES[NAME]
PARSE_TIMEOUT: Final = "parse_timeout"
PARSE_FAILED: Final = "parse_failed"
CONVERT_FAILED: Final = "convert_failed"
PAGE_LIMIT_EXCEEDED: Final = "page_limit_exceeded"
CONVERTED_PDF_MISSING: Final = "converted_pdf_missing"


class ExtractInput(PlanInput):
    """The item envelope, the plan and the part (null on the first call)."""

    part: PartRange | None = None


class ExtractResult(ActivityResult):
    """`{pageCount, nextPart, docIntelPages, warnings}` with the `ActivityResult` fields."""

    page_count: int | None = Field(default=None, ge=1)
    next_part: PartRange | None = None
    doc_intel_pages: int = Field(default=0, ge=0)
    warnings: list[ItemWarning] = Field(default_factory=list)


@dataclass(slots=True)
class Budget:
    """The build's parse budget: the seconds the chunk set has used, kept up to date in SQL."""

    services: DocumentServices
    chunk_set_id: UUID
    limit: int
    used: int

    @property
    def remaining(self) -> int:
        return self.limit - self.used

    async def add(self, outcome: ChildOutcome) -> None:
        """Add a child's wall-clock seconds, rounded up, failed attempts included."""
        seconds = outcome.seconds
        self.used += seconds

        def work(connection: Connection) -> None:
            connection.execute(
                update(chunk_sets)
                .where(chunk_sets.c.id == self.chunk_set_id)
                .values(parse_seconds_used=chunk_sets.c.parse_seconds_used + seconds)
            )

        # Not idempotent, so a lost answer isn't re-run: the budget then counts a little less.
        await self.services.sql.run(work, name="extract_parse_seconds", retry="none")

    def child_limit(self, own_limit: int) -> int:
        """A child's wall clock: min(its own limit, what is left); less than one second left is
        `parse_timeout` before the child starts. Raised as the method's failure, because the
        partition port raises it inside the method's `extract`."""
        if self.remaining < 1:
            raise MethodFailedError(PARSE_TIMEOUT)
        return min(own_limit, self.remaining)


@dataclass(slots=True)
class Call:
    """What one Extract call learns as it runs."""

    services: DocumentServices
    item: ExtractInput
    facts: BuildFacts
    budget: Budget
    warnings: list[ItemWarning] = field(default_factory=list)

    @property
    def plan(self) -> BuildPlan:
        return self.item.plan

    @property
    def max_pages(self) -> int:
        return self.item.settings.max_pages_per_document


# --- SQL ------------------------------------------------------------------------------------------


def record_pdf_sha(connection: Connection, chunk_set_id: UUID, sha256: str) -> None:
    """The PDF the build reads, recorded while the build is `building`."""
    connection.execute(
        update(chunk_sets)
        .where(chunk_sets.c.id == chunk_set_id, chunk_sets.c.state == literal("building"))
        .values(pdf_sha256=bytes.fromhex(sha256))
    )


def record_page_count(
    connection: Connection, version_id: UUID, page_count: int, *, converted: bool
) -> None:
    """A PDF version's page count is recorded once; a conversion records its own, because another
    converter version can paginate differently (section 6.3 step 5)."""
    statement = update(document_versions).where(document_versions.c.id == version_id)
    if not converted:
        statement = statement.where(document_versions.c.page_count.is_(None))
    connection.execute(statement.values(page_count=page_count))


# --- Children -------------------------------------------------------------------------------------


def _page_count(outcome: ChildOutcome) -> int:
    if not isinstance(outcome.result, InspectOk) or outcome.result.page_count is None:
        raise ItemFailure(PARSE_FAILED)
    return outcome.result.page_count


def _check_cap(page_count: int, max_pages: int) -> None:
    if page_count > max_pages:
        raise ItemFailure(PAGE_LIMIT_EXCEEDED)


async def _inspect(call: Call, slot: ParseSlot, directory: Scratch) -> ChildOutcome:
    """An `inspect` child of the PDF in `directory`, its seconds added to the budget."""
    outcome = await slot.inspect(directory, PDF, max_pages=call.max_pages)
    await call.budget.add(outcome)
    return outcome


async def _fresh(slot: ParseSlot, data: bytes) -> Scratch:
    """A fresh scratch directory whose `source.bin` the parent wrote from the bytes it read."""
    directory = slot.scratch()
    await asyncio.to_thread(directory.write, SOURCE, data)
    return directory


async def convert_source(call: Call, slot: ParseSlot) -> bytes:
    """Section 6.3 step 3.1: the source written into the `inspect` child's directory and into a
    second, fresh directory of the same user, `inspect` on it, then a `convert` child there, so
    it reads bytes the parent wrote and no child touched. Returns the converted PDF's bytes."""
    plan = call.plan
    started = call.services.clock.monotonic()
    inspected = slot.scratch()
    converting = slot.scratch()
    with (
        await asyncio.to_thread(inspected.open_new, SOURCE) as first,
        await asyncio.to_thread(converting.open_new, SOURCE) as second,
    ):
        await download_into(
            call.services.documents,
            plan.source_blob_path,
            (first, second),
            expected_sha256=plan.source_sha256,
        )
    outcome = await slot.inspect(inspected, plan.content_type, max_pages=call.max_pages)
    await call.budget.add(outcome)
    if not outcome.ok:
        raise ItemFailure(outcome.code or PARSE_FAILED)
    limit = call.budget.child_limit(CONVERT_TIMEOUT_SECONDS)
    converted, data = await slot.convert(
        converting, plan.content_type, limit_seconds=limit, max_pages=call.max_pages
    )
    await call.budget.add(converted)
    seconds = call.services.clock.monotonic() - started
    if not converted.ok or data is None:
        telemetry.record_conversion(plan.content_type, _convert_outcome(converted), seconds)
        raise ItemFailure(converted.code or CONVERT_FAILED)
    telemetry.record_conversion(plan.content_type, "converted", seconds)
    return data


def _convert_outcome(outcome: ChildOutcome) -> Literal["timeout", "memory", "failed"]:
    if outcome.exit == "timeout":
        return "timeout"
    if outcome.exit == "memory":
        return "memory"
    return "failed"


async def stored_pdf(call: Call) -> bytes | None:
    """Section 6.3 step 3.2: the stored converted PDF, checked against its recorded hash;
    `None` when it is missing."""
    conversion = call.plan.conversion
    if conversion is None or conversion.converted_sha256 is None:
        raise ValueError("only a reuse plan reads the stored converted PDF")
    data = await read_blob(call.services.documents, conversion.converted_blob_path)
    if data is None:
        return None
    if await asyncio.to_thread(_sha256, data) != conversion.converted_sha256:
        raise ItemFailure("source_changed")
    return data


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


async def conversion_call(call: Call, slot: ParseSlot) -> ExtractResult:
    """The first call of a DOCX or PPTX version (section 6.3 step 3): it parses nothing."""
    plan = call.plan
    conversion = plan.conversion
    if conversion is None:
        raise ValueError("a conversion call belongs to a DOCX or PPTX version")
    started = call.services.clock.monotonic()
    reused = False
    data: bytes | None = None
    if not conversion.convert:
        data = await stored_pdf(call)
        if data is None:
            call.warnings.append(ItemWarning(code=CONVERTED_PDF_MISSING))
        else:
            reused = True
    if data is None:
        data = await convert_source(call, slot)
    pdf_sha = await asyncio.to_thread(_sha256, data)
    await write_blob(call.services.work, plan.work_pdf_path(), data)
    await call.services.sql.run(
        lambda connection: record_pdf_sha(connection, plan.chunk_set_id, pdf_sha),
        name="extract_pdf_sha",
        retry="transient",
    )
    outcome = await _inspect(call, slot, await _fresh(slot, data))
    if not outcome.ok:
        if outcome.exit in {"timeout", "memory"}:
            raise ItemFailure(outcome.code or PARSE_TIMEOUT)
        # A converted PDF that the PDF `inspect` refuses fails the conversion (section 6.3).
        raise ItemFailure(CONVERT_FAILED)
    page_count = _page_count(outcome)
    if reused:
        telemetry.record_conversion(
            plan.content_type,
            "reused",
            call.services.clock.monotonic() - started,
            page_count,
        )
    else:
        telemetry.CONVERT_PAGES.record(page_count, content_type=plan.content_type)
    _check_cap(page_count, call.max_pages)
    await call.services.sql.run(
        lambda connection: record_page_count(
            connection, plan.version_id, page_count, converted=True
        ),
        name="extract_page_count",
        retry="transient",
    )
    parts = _extract_plan(call, page_count)
    return ExtractResult.success(
        page_count=page_count,
        next_part=_wire_part(parts.first_part()),
        warnings=call.warnings,
    )


def _extract_plan(call: Call, page_count: int) -> ExtractPlan:
    method = registered(call.services, call.plan).implementation
    return method.extract_plan(
        ExtractSource(
            page_count=page_count, pages_per_extract=call.item.settings.pagewise_pages_per_extract
        )
    )


def _wire_part(part: PageRange | None) -> PartRange | None:
    if part is None:
        return None
    return PartRange(first_page=part.first_page, last_page=part.last_page)


@dataclass(frozen=True, slots=True)
class Prepared:
    """The build's PDF as one call prepared it: its page count and SHA-256, and the fresh
    directory whose `source.bin` the parent wrote in the download and no child has been given."""

    page_count: int
    sha256: str
    parse_directory: Scratch


async def prepare_part(call: Call, slot: ParseSlot) -> Prepared:
    """Sections 6.3 steps 1, 2 and 4: download the build's PDF into the `inspect` child's
    directory and into a second fresh directory in one pass, `inspect` it, record the hash and
    page count on the first call of a PDF version and apply the page cap."""
    plan = call.plan
    if plan.is_office:
        container, path = call.services.work, plan.work_pdf_path()
        expected = call.facts.pdf_sha256
        if expected is None:
            raise ValueError("a later call of a DOCX or PPTX build follows its conversion call")
    else:
        container, path, expected = (
            call.services.documents,
            plan.source_blob_path,
            plan.source_sha256,
        )
    inspected = slot.scratch()
    parsing = slot.scratch()
    with (
        await asyncio.to_thread(inspected.open_new, SOURCE) as first,
        await asyncio.to_thread(parsing.open_new, SOURCE) as second,
    ):
        downloaded = await download_into(container, path, (first, second), expected_sha256=expected)
    first_call = call.item.part is None
    if first_call and not plan.is_office:
        await call.services.sql.run(
            lambda connection: record_pdf_sha(connection, plan.chunk_set_id, downloaded.sha256),
            name="extract_pdf_sha",
            retry="transient",
        )
    outcome = await _inspect(call, slot, inspected)
    if not outcome.ok:
        raise ItemFailure(outcome.code or PARSE_FAILED)
    page_count = _page_count(outcome)
    _check_cap(page_count, call.max_pages)
    if first_call and not plan.is_office:
        await call.services.sql.run(
            lambda connection: record_page_count(
                connection, plan.version_id, page_count, converted=False
            ),
            name="extract_page_count",
            retry="transient",
        )
    return Prepared(page_count, downloaded.sha256, parsing)


# --- The method's ports ---------------------------------------------------------------------------


class HeldSlot:
    """The call's parse slot, taken before its first child and left once, by the recursive
    method's PDF source or at the end of the call, whichever comes first."""

    def __init__(self) -> None:
        self._stack = AsyncExitStack()
        self.slot: ParseSlot | None = None

    async def take(self, pool: ParsePool) -> ParseSlot:
        """A slot and its two parse users; users still busy when the first command's wall clock
        runs out are `parse_timeout`."""
        try:
            slot = await self._stack.enter_async_context(pool.slot(INSPECT_TIMEOUT_SECONDS))
        except ParseUsersUnavailableError:
            raise ItemFailure(PARSE_TIMEOUT) from None
        self.slot = slot
        return slot

    async def release(self) -> None:
        """End every process of both users, remove the scratch directories and release the
        users; a second call does nothing."""
        self.slot = None
        await self._stack.aclose()


class PortMisuseError(RuntimeError):
    """A method called a port of the Extract call in a way the call doesn't serve: a programming
    error, which the failure boundary records."""

    def __init__(self) -> None:
        super().__init__("an Extract port was used out of turn")


class ExtractPorts:
    """The method's ports for one Extract call: the page-wise `Partitioner` and the recursive
    `PdfSource`, both over the PDF that `prepare_part` downloaded and inspected."""

    def __init__(self, call: Call, held: HeldSlot, prepared: Prepared) -> None:
        self._call = call
        self._held = held
        self._prepared = prepared
        self._used = False

    def _take(self, ctx: MethodContext) -> Scratch:
        # Each call partitions one part or opens the PDF once, from a directory no child has had.
        if self._used:
            raise PortMisuseError
        if ctx.pdf_sha256 != self._prepared.sha256 or ctx.page_count != self._prepared.page_count:
            raise ValueError("the method's context names another PDF than the call inspected")
        self._used = True
        return self._prepared.parse_directory

    async def partition(
        self, ctx: MethodContext, part: PageRange, parameters: Mapping[str, JsonValue]
    ) -> bytes:
        """The `partition` child of `part` under the slot's first user, its seconds counted
        against the parse budget; returns the `partition.json` bytes the runner read and
        validated against the version's page count and the part (section 15.6 item 5.6), which
        the method uploads. A failed child raises its item code."""
        slot = self._held.slot
        if slot is None:
            raise PortMisuseError
        directory = self._take(ctx)
        services = self._call.services
        limit = self._call.budget.child_limit(services.parse_pool.parse_timeout_seconds)
        outcome, raw, _ = await slot.partition(
            directory,
            pages=ChildPages(first=part.first_page, last=part.last_page),
            page_count=ctx.page_count,
            parameters=parameters,
            limit_seconds=limit,
            max_pages=self._call.max_pages,
        )
        await self._call.budget.add(outcome)
        if not outcome.ok or raw is None:
            raise MethodFailedError(outcome.code or PARSE_FAILED)
        return raw

    def open(self, ctx: MethodContext) -> AbstractAsyncContextManager[IO[bytes]]:
        """The inspected PDF as a stream for the layout call."""
        return self._open(ctx)

    @asynccontextmanager
    async def _open(self, ctx: MethodContext) -> AsyncIterator[IO[bytes]]:
        directory = self._take(ctx)
        stream = await asyncio.to_thread(directory.open_own, SOURCE)
        try:
            # The open descriptor keeps the bytes once the slot removes the directory.
            await self._held.release()
            yield stream
        finally:
            await asyncio.to_thread(stream.close)


async def extract_part(call: Call, held: HeldSlot, prepared: Prepared) -> ExtractResult:
    """The method's `extract` of the call's part (section 6.3 steps 7 and 8), its ports bound."""
    item = call.item
    plan = call.plan
    parts = _extract_plan(call, prepared.page_count)
    current = (
        parts.first_part()
        if item.part is None
        else PageRange(item.part.first_page, item.part.last_page)
    )
    if current not in parts.parts:
        raise ValueError("the part isn't one of the method's parts for this page count")
    facts = call.facts
    context = method_context(
        item,
        BuildFacts(
            prepared.page_count,
            prepared.sha256,
            facts.chunk_count,
            call.budget.used,
            facts.chunk_set_state,
        ),
    )
    ports = ExtractPorts(call, held, prepared)
    implementation = registered(call.services, plan).implementation
    with bound_partitioner(ports), bound_pdf_source(ports):
        result = await implementation.extract(context, plan.method.validated_parameters(), current)
    return ExtractResult.success(
        page_count=prepared.page_count,
        next_part=_wire_part(parts.next_part(current)),
        doc_intel_pages=result.doc_intel_pages,
        warnings=[*call.warnings, *result.warnings],
    )


async def extract(services: DocumentServices, item: ExtractInput, span: Span) -> ExtractResult:
    del span
    plan = item.plan
    facts = await fence_and_read(services, item, ItemStage.EXTRACT, DEADLINE_SECONDS)
    budget = Budget(
        services,
        plan.chunk_set_id,
        item.settings.max_parse_seconds_per_document,
        facts.parse_seconds_used,
    )
    call = Call(services, item, facts, budget)
    held = HeldSlot()
    try:
        slot = await held.take(services.parse_pool)
        if plan.is_office and item.part is None:
            return await conversion_call(call, slot)
        prepared = await prepare_part(call, slot)
        return await extract_part(call, held, prepared)
    finally:
        await held.release()


def _failure(code: str, retry_after: int | None) -> ExtractResult:
    return ExtractResult.failure(code, retry_after_seconds=retry_after)


async def run(payload: object, provider: ServicesProvider = document_services) -> ActivityOutput:
    return await run_document_activity(
        NAME,
        payload,
        ExtractInput,
        extract,
        stage=ItemStage.EXTRACT,
        provider=provider,
        ids=item_ids,
        failure=_failure,
    )


async def extract_document(payload: object) -> ActivityOutput:
    return await run(payload)


bp: Final = blueprint(extract_document)
