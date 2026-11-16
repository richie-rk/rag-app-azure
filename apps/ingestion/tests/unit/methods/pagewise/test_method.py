"""Unit tests of `PagewiseMethod` (T-182; `14-ingestion.md` sections 6.3 to 6.5, 10 and 12;
ADR 0047).

The T-182 fixture tests run the method on the four fixture documents and compare the chunks with
`tests/fixtures/documents/<file>.expected.json`. They need what this machine lacks: the expected
files (produced by a reviewed regeneration in the ingestion image), `unstructured` with its pinned
models, the pinned LibreOffice for the two Office fixtures and the `cl100k_base` encoding; each
missing prerequisite skips with its reason. An expected file holds, under the key `pagewise`, the
chunk lines of the chunks file (`ordinal`, `chunkText`, `headingPath`, `pageNumber`,
`tokenCount`), with each table's summary the stub summary `Summary of the table on page <n>.`.
"""

from __future__ import annotations

import importlib
import importlib.util
import json
from collections.abc import Callable, Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pikepdf
import pytest
from pydantic import JsonValue

from docchat_core.clients import ClientRegistry
from docchat_core.ingestion.methods import (
    PAGEWISE_V1,
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
from docchat_core.ingestion.models import ItemSettings
from docchat_core.ingestion.names import (
    BlobContainer,
    chunks_path,
    extract_part_path,
    table_summaries_path,
)
from docchat_core.ingestion.settings import TokenEncoding
from docchat_ingestion.isolation import convert
from docchat_ingestion.isolation.results import (
    DOCX,
    PPTX,
    ExtractElement,
    ExtractPage,
    ExtractPartV1,
)
from docchat_ingestion.methods.pagewise import method as pagewise_method
from docchat_ingestion.methods.pagewise.method import PagewiseMethod, PagewisePorts
from docchat_ingestion.methods.pagewise.partition import partition_part
from docchat_ingestion.methods.pagewise.summaries import (
    OpenAISummaryModel,
    SummaryReply,
)
from docchat_ingestion.methods.store import WorkStore
from docchat_ingestion.methods.tables import table_sha256
from docchat_ingestion.methods.text import Tokenizer
from docchat_ingestion.selfcheck import LIBREOFFICE_PACKAGE, installed_package_version
from docchat_ingestion.workfiles import WorkFileError, decode_chunks, decode_table_summaries
from docchat_testing.clock import FakeClock

type ContextFactory = Callable[..., MethodContext]
WORK = BlobContainer.INGESTION_WORK
FIXTURES = Path(__file__).resolve().parents[6] / "tests" / "fixtures" / "documents"
TABLE = "<table><tbody><tr><td>Tea</td><td>2</td></tr></tbody></table>"


def element(kind: str, text: str = "", html: str | None = None) -> ExtractElement:
    return ExtractElement(kind=kind, text=text, table_html=html, heading_level=None)  # type: ignore[arg-type]


def part_bytes(first: int, last: int, count: int, pages: dict[int, list[ExtractElement]]) -> bytes:
    part = ExtractPartV1(
        format_version=1,
        method="pagewise",
        page_count=count,
        first_page=first,
        last_page=last,
        pages=[ExtractPage(page_number=page, elements=items) for page, items in pages.items()],
    )
    return part.model_dump_json(by_alias=True).encode("utf-8")


class _Partitioner:
    def __init__(self, data: bytes | Exception) -> None:
        self.data = data
        self.calls: list[tuple[MethodContext, PageRange, dict[str, JsonValue]]] = []

    async def partition(
        self, ctx: MethodContext, part: PageRange, parameters: Mapping[str, JsonValue]
    ) -> bytes:
        self.calls.append((ctx, part, dict(parameters)))
        if isinstance(self.data, Exception):
            raise self.data
        return self.data


class _Model:
    deployment = "ingest-summary"

    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, system: str, user: str) -> SummaryReply:
        del system
        self.calls += 1
        page = user.split("page ", 1)[1].split(" ", 1)[0]
        return SummaryReply(f"Summary of the table on page {page}.", "stop", None, 10, 5)


def method_for(
    store: WorkStore,
    tokenizer: Tokenizer,
    partitioner: _Partitioner | None = None,
    model: _Model | None = None,
) -> PagewiseMethod:
    ports = PagewisePorts(
        store=store,
        partitioner=partitioner or _Partitioner(b""),
        summaries=model or _Model(),
        summary_encoding=TokenEncoding.O200K_BASE,
        tokenizers=lambda encoding: tokenizer,
        clock=FakeClock(),
    )
    return PagewiseMethod(lambda ctx: ports)


def test_the_plan_is_parts_of_the_item_size(store: WorkStore, tokenizer: Tokenizer) -> None:
    plan = method_for(store, tokenizer).extract_plan(
        ExtractSource(page_count=85, pages_per_extract=40)
    )

    assert plan == ExtractPlan(parts=(PageRange(1, 40), PageRange(41, 80), PageRange(81, 85)))


async def test_extract_writes_the_validated_part(
    store: WorkStore, tokenizer: Tokenizer, make_context: ContextFactory
) -> None:
    ctx = make_context(page_count=3)
    data = part_bytes(1, 3, 3, {1: [element("text", "a")]})
    partitioner = _Partitioner(data)
    parameters = PagewiseParametersV1(ocr_languages=["deu"])

    result = await method_for(store, tokenizer, partitioner).extract(
        ctx, parameters, PageRange(1, 3)
    )

    assert (result.doc_intel_pages, result.warnings) == (0, ())
    assert partitioner.calls == [
        (ctx, PageRange(1, 3), {"summarizeTables": True, "ocrLanguages": ["deu"]})
    ]
    assert await store.read(WORK, extract_part_path(ctx.work_prefix, 1, 3)) == data


async def test_extract_refuses_what_it_cant_parse(
    store: WorkStore, tokenizer: Tokenizer, make_context: ContextFactory
) -> None:
    method = method_for(store, tokenizer, _Partitioner(MethodFailedError("parse_timeout")))
    parameters = PagewiseParametersV1()

    with pytest.raises(MethodFailedError) as capped:
        await method.extract(make_context(page_count=2001), parameters, PageRange(1, 40))
    assert capped.value.code == "page_limit_exceeded"
    with pytest.raises(ValueError, match="plan"):
        await method.extract(make_context(page_count=3), parameters, PageRange(1, 2))
    with pytest.raises(TypeError):
        await method.extract(make_context(page_count=3), RecursiveParametersV1(), PageRange(1, 3))
    with pytest.raises(MethodFailedError) as timed_out:
        await method.extract(make_context(page_count=3), parameters, PageRange(1, 3))
    assert timed_out.value.code == "parse_timeout"


async def _seed_parts(store: WorkStore, ctx: MethodContext) -> None:
    await store.write(
        WORK,
        extract_part_path(ctx.work_prefix, 1, 2),
        part_bytes(1, 2, 3, {1: [element("title", "Prices"), element("table", "t", TABLE)], 2: []}),
    )
    await store.write(
        WORK,
        extract_part_path(ctx.work_prefix, 3, 3),
        part_bytes(3, 3, 3, {3: [element("text", "Closing words."), element("table", "t", TABLE)]}),
    )


async def test_enrich_summarises_each_distinct_table_once(
    store: WorkStore,
    tokenizer: Tokenizer,
    make_context: ContextFactory,
    make_settings: Callable[..., ItemSettings],
) -> None:
    ctx = make_context(page_count=3, settings=make_settings(pagewise_pages_per_extract=2))
    await _seed_parts(store, ctx)
    model = _Model()
    method = method_for(store, tokenizer, model=model)

    result = await method.enrich(ctx, PagewiseParametersV1())

    assert result.done
    assert (result.tables, result.summarised) == (1, 1)
    assert model.calls == 1
    lines = decode_table_summaries(await store.read(WORK, table_summaries_path(ctx.work_prefix)))
    assert [(line.table_sha256, line.page_number) for line in lines] == [(table_sha256(TABLE), 1)]
    again = await method.enrich(ctx, PagewiseParametersV1())
    assert (again.done, again.tables, model.calls) == (True, 0, 1)
    off = await method.enrich(ctx, PagewiseParametersV1(summarize_tables=False))
    assert off.done
    assert off.tables == 0


async def test_chunk_writes_one_chunk_per_page_with_the_summaries(
    store: WorkStore,
    tokenizer: Tokenizer,
    make_context: ContextFactory,
    make_settings: Callable[..., ItemSettings],
) -> None:
    ctx = make_context(page_count=3, settings=make_settings(pagewise_pages_per_extract=2))
    await _seed_parts(store, ctx)
    method = method_for(store, tokenizer)
    await method.enrich(ctx, PagewiseParametersV1())

    result = await method.chunk(ctx, PagewiseParametersV1())

    assert result.chunk_count == 2
    raw = await store.read(WORK, chunks_path(ctx.work_prefix, ctx.chunk_set_id))
    assert raw is not None
    decoded = decode_chunks(raw)
    header, lines = decoded.header, decoded.chunks
    assert header.title == ctx.title
    assert [(line.page_number, line.heading_path) for line in lines] == [(1, ["Prices"]), (3, [])]
    assert lines[0].chunk_text == f"Prices\n\nSummary of the table on page 1.\n\n{TABLE}"
    assert lines[1].chunk_text == f"Closing words.\n\nSummary of the table on page 1.\n\n{TABLE}"

    plain = await method.chunk(ctx, PagewiseParametersV1(summarize_tables=False))
    assert plain.chunk_count == 2
    raw = await store.read(WORK, chunks_path(ctx.work_prefix, ctx.chunk_set_id))
    assert raw is not None
    assert decode_chunks(raw).chunks[0].chunk_text == f"Prices\n\n{TABLE}"


async def test_chunk_needs_every_part_of_the_plan(
    store: WorkStore, tokenizer: Tokenizer, make_context: ContextFactory
) -> None:
    ctx = make_context(page_count=3)
    method = method_for(store, tokenizer)

    with pytest.raises(WorkFileError):
        await method.chunk(ctx, PagewiseParametersV1())
    # A part an earlier claim wrote with other ranges is ignored, and one that doesn't match the
    # version is refused.
    await store.write(WORK, extract_part_path(ctx.work_prefix, 1, 2), part_bytes(1, 2, 3, {}))
    with pytest.raises(WorkFileError):
        await method.chunk(ctx, PagewiseParametersV1())
    await store.write(WORK, extract_part_path(ctx.work_prefix, 1, 3), part_bytes(1, 3, 4, {}))
    with pytest.raises(WorkFileError):
        await method.chunk(ctx, PagewiseParametersV1())


async def test_t189_a_document_without_text_fails_with_no_extractable_text(
    store: WorkStore, tokenizer: Tokenizer, make_context: ContextFactory
) -> None:
    ctx = make_context(page_count=3)
    await store.write(
        WORK, extract_part_path(ctx.work_prefix, 1, 3), part_bytes(1, 3, 3, {1: [], 2: [], 3: []})
    )

    with pytest.raises(MethodFailedError) as failed:
        await method_for(store, tokenizer).chunk(ctx, PagewiseParametersV1())

    assert failed.value.code == "no_extractable_text"
    assert await store.read(WORK, chunks_path(ctx.work_prefix, ctx.chunk_set_id)) is None


# --- T-182 on the four fixture documents ----------------------------------------------------------

FIXTURE_DOCUMENTS = ("tables.pdf", "scanned.pdf", "headings.docx", "slides.pptx")
OFFICE_TYPES = {".docx": DOCX, ".pptx": PPTX}


def _expected(name: str) -> list[dict[str, Any]]:
    path = FIXTURES / f"{name}.expected.json"
    if not path.is_file():
        pytest.skip(
            f"{path.name} isn't in tests/fixtures/documents: the expected chunks are produced by a "
            "reviewed regeneration in the ingestion image"
        )
    expected = json.loads(path.read_text(encoding="utf-8"))
    if "pagewise" not in expected:
        pytest.skip(f"{path.name} holds no page-wise expectation")
    chunks: list[dict[str, Any]] = expected["pagewise"]
    return chunks


def _real_tokenizer() -> Tokenizer:
    from docchat_ingestion.methods.text import TiktokenTokenizer  # noqa: PLC0415

    try:
        return TiktokenTokenizer(TokenEncoding.CL100K_BASE)
    except Exception as failure:  # noqa: BLE001 - any loading failure means no cached encoding
        pytest.skip(f"the cl100k_base encoding isn't available here: {type(failure).__name__}")


def _version_pdf(name: str, tmp_path: Path) -> Path:
    source = FIXTURES / name
    suffix = source.suffix
    if suffix == ".pdf":
        return source
    if installed_package_version(LIBREOFFICE_PACKAGE) != convert.LIBREOFFICE_PACKAGE_VERSION:
        pytest.skip("the expected pages of an Office fixture need the pinned LibreOffice 7.4.7")
    (tmp_path / "source.bin").write_bytes(source.read_bytes())
    (tmp_path / convert.PROFILE_USER_DIRECTORY).mkdir(parents=True)
    (tmp_path / convert.OUTPUT_DIRECTORY).mkdir()
    (tmp_path / convert.PROFILE_FILE).write_bytes(convert.PROFILE)
    convert.convert_in_child(tmp_path, OFFICE_TYPES[suffix])
    return tmp_path / "converted.pdf"


@pytest.mark.parametrize("name", FIXTURE_DOCUMENTS)
async def test_t182_the_fixture_documents_match_their_expectations(
    name: str, store: WorkStore, make_context: ContextFactory, tmp_path: Path
) -> None:
    expected = _expected(name)
    if importlib.util.find_spec("unstructured") is None:
        pytest.skip("unstructured isn't installed here (it needs torch, whose index is blocked)")
    tokenizer = _real_tokenizer()
    pdf = _version_pdf(name, tmp_path)
    with pikepdf.open(pdf) as document:
        page_count = len(document.pages)
    ctx = make_context(page_count=page_count)
    parameters = PagewiseParametersV1()
    method = method_for(store, tokenizer)
    for part in method.extract_plan(ExtractSource(page_count, 40)).parts:
        extracted = partition_part(
            pdf,
            first=part.first_page,
            last=part.last_page,
            page_count=page_count,
            parameters=dict(parameters.dumped()),
        )
        raw = extracted.model_dump_json(by_alias=True).encode("utf-8")
        await store.write(
            WORK, extract_part_path(ctx.work_prefix, part.first_page, part.last_page), raw
        )
    await method.enrich(ctx, parameters)
    await method.chunk(ctx, parameters)
    chunks_file = await store.read(WORK, chunks_path(ctx.work_prefix, ctx.chunk_set_id))
    assert chunks_file is not None
    lines = decode_chunks(chunks_file).chunks

    assert [line.model_dump(mode="json") for line in lines] == expected
    assert [line.page_number for line in lines] == list(range(1, page_count + 1))


# --- The worker process's instance ----------------------------------------------------------------


def test_the_package_exposes_the_instance_the_function_app_registers() -> None:
    package = importlib.import_module("docchat_ingestion.methods.pagewise")
    registry = MethodRegistry()

    registry.register(PAGEWISE_V1, package.IMPLEMENTATION)

    assert registry.get("pagewise", 1).implementation is pagewise_method.IMPLEMENTATION
    assert isinstance(package.IMPLEMENTATION, IngestionMethod)
    with pytest.raises(AttributeError):
        _ = package.OTHER


@pytest.fixture
def worker(monkeypatch: pytest.MonkeyPatch, store: WorkStore) -> None:
    """The open registry and the app settings of a worker process, as fakes."""
    registry = SimpleNamespace(openai=object(), blob_container=lambda name: None)
    monkeypatch.setattr(ClientRegistry, "_open", registry)
    settings = SimpleNamespace(
        openai_table_summary_deployment="ingest-summary",
        openai_table_summary_encoding=TokenEncoding.O200K_BASE,
    )
    monkeypatch.setattr(pagewise_method, "app_settings", lambda: settings)
    monkeypatch.setattr(pagewise_method, "process_store", lambda: store)


@pytest.mark.usefixtures("worker")
def test_the_process_ports_come_from_the_worker(
    store: WorkStore, make_context: ContextFactory
) -> None:
    ports = pagewise_method.process_ports(make_context())

    assert ports.store is store
    assert isinstance(ports.summaries, OpenAISummaryModel)
    assert ports.summaries.deployment == "ingest-summary"
    assert ports.summary_encoding is TokenEncoding.O200K_BASE


@pytest.mark.usefixtures("worker")
async def test_extract_partitions_through_the_bound_port(
    store: WorkStore, make_context: ContextFactory
) -> None:
    ctx = make_context(page_count=1)
    data = part_bytes(1, 1, 1, {1: [element("text", "a")]})
    partitioner = _Partitioner(data)
    implementation = pagewise_method.IMPLEMENTATION

    with pytest.raises(pagewise_method.UnboundPartitionerError):
        await implementation.extract(ctx, PagewiseParametersV1(), PageRange(1, 1))
    with pagewise_method.bound_partitioner(partitioner):
        await implementation.extract(ctx, PagewiseParametersV1(), PageRange(1, 1))

    assert [part for _, part, _ in partitioner.calls] == [PageRange(1, 1)]
    assert await store.read(WORK, extract_part_path(ctx.work_prefix, 1, 1)) == data
