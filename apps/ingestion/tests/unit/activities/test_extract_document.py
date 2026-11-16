"""Unit tests of `extract_document` (`14-ingestion.md` sections 6.3, 15.6, 15.7 and 15.11; T-189's
order of refusals, T-191's budget, T-615 and T-616's Office path at unit level).

Extract runs the real methods: page-wise partitions its part through the port bound over the
call's parse slot (the partition child's work in process, real `pikepdf`, a fake
`unstructured`), and recursive sends the PDF the call downloaded to the real
`DocumentIntelligenceClient` over `ReplayTransport`."""

from __future__ import annotations

import asyncio
import selectors
from types import SimpleNamespace
from typing import Any, override

import pytest
from pydantic import JsonValue

from docchat_core.ingestion.methods import MethodContext, MethodFailedError, PageRange
from docchat_core.ingestion.models import ContentType
from docchat_core.ingestion.names import extract_document_path, extract_part_path
from docchat_core.telemetry import TestExporters
from docchat_ingestion.activities import extract_document
from docchat_ingestion.activities.documents import BuildFacts, PlanInput, method_context
from docchat_ingestion.isolation.results import Command, parse_partition
from docchat_ingestion.workfiles import decode_extract_document
from docchat_testing.clock import FakeClock

DOCX = ContentType.DOCX


def pdf_world(
    kit: SimpleNamespace, world: Any, ids: Any, method: str = "pagewise", **facts: Any
) -> Any:
    plan = kit.make_plan(ids, method=method)
    world.documents.blobs[plan.source_blob_path] = kit.PDF_BYTES
    world.sql.script(kit.fence_ok(), kit.build_facts(**facts))
    return plan


def parse_seconds(world: Any) -> list[int]:
    return [
        entry.values["parse_seconds_used_1"]
        for entry in world.sql.connection.statements("UPDATE app.chunk_sets SET parse_seconds_used")
    ]


# --- PDF versions, page-wise ----------------------------------------------------------------------


async def test_the_first_call_of_a_pdf_records_its_pdf_and_partitions_its_part(
    kit: SimpleNamespace, world: Any, library: Any
) -> None:
    ids = kit.Ids()
    plan = pdf_world(kit, world, ids)
    world.sql.script(kit.Result(), kit.Result(), kit.Result(), kit.Result())
    output = await extract_document.run(kit.plan_payload(ids, plan, part=None), world.provider)
    assert output["ok"] is True, output
    assert output["pageCount"] == 3
    assert output["nextPart"] == {"firstPage": 3, "lastPage": 3}
    assert output["docIntelPages"] == 0
    # The inspected download and a second fresh directory, both written in one download, the
    # partition child reading the second.
    assert world.pool.commands() == ["inspect", "partition"]
    inspected, parsing = world.pool.scratches
    assert inspected is not parsing
    assert world.pool.calls[0][2] == {"source.bin": kit.PDF_BYTES}
    assert world.pool.calls[1][1:] == ("1-2", {"source.bin": kit.PDF_BYTES})
    assert [entry for entry in world.log if entry[1] == "download"] == [
        ("documents", "download", plan.source_blob_path)
    ]
    assert world.pool.partition_limits == [1200]
    (call,) = library.calls
    assert (call["strategy"], call["infer_table_structure"], call["languages"]) == (
        "hi_res",
        True,
        ["eng"],
    )
    raw = world.work.blobs[extract_part_path(plan.work_prefix, 1, 2)]
    part = parse_partition(raw, page_count=3, first=1, last=2)
    assert [page.page_number for page in part.pages] == [1, 2]
    assert [element.text for element in part.pages[1].elements] == [
        "Section 2",
        "Text of page 2.",
    ]
    assert part.pages[0].elements[-1].table_html == kit.TABLE_HTML
    executed = world.sql.connection.executed
    pdf_sha = executed[2]
    assert pdf_sha.sql.startswith("UPDATE app.chunk_sets SET pdf_sha256")
    assert "state = N'building'" in pdf_sha.sql
    assert "page_count IS NULL" in executed[4].sql
    # Both children count against the build's parse budget, the inspect's first.
    assert parse_seconds(world) == [2, 3]
    assert (world.pool.taken, world.pool.held) == (1, 0)
    world.sql.connection.finished()


async def test_a_later_part_records_nothing_more(kit: SimpleNamespace, world: Any) -> None:
    ids = kit.Ids()
    plan = pdf_world(kit, world, ids)
    world.sql.script(kit.Result(), kit.Result())
    output = await extract_document.run(
        kit.plan_payload(ids, plan, part={"firstPage": 3, "lastPage": 3}), world.provider
    )
    assert output["nextPart"] is None
    assert world.pool.calls[-1][1] == "3-3"
    raw = world.work.blobs[extract_part_path(plan.work_prefix, 3, 3)]
    assert parse_partition(raw, page_count=3, first=3, last=3).pages[0].page_number == 3
    world.sql.connection.finished()


async def test_a_part_that_isnt_the_methods_is_internal_error(
    kit: SimpleNamespace, world: Any
) -> None:
    ids = kit.Ids()
    plan = pdf_world(kit, world, ids)
    world.sql.script(kit.Result())
    output = await extract_document.run(
        kit.plan_payload(ids, plan, part={"firstPage": 2, "lastPage": 3}), world.provider
    )
    assert (output["errorCode"], output["retryable"]) == ("internal_error", True)
    assert "partition" not in world.pool.commands()


@pytest.mark.parametrize(
    ("stored", "code"), [(None, "source_missing"), (b"other bytes", "source_changed")]
)
async def test_the_source_must_be_the_versions(
    kit: SimpleNamespace, world: Any, stored: bytes | None, code: str
) -> None:
    ids = kit.Ids()
    plan = pdf_world(kit, world, ids)
    if stored is None:
        world.documents.blobs.clear()
    else:
        world.documents.blobs[plan.source_blob_path] = stored
    output = await extract_document.run(kit.plan_payload(ids, plan), world.provider)
    assert output["errorCode"] == code
    assert output["retryable"] is False
    assert world.pool.commands() == []


async def test_a_storage_failure_is_retryable(kit: SimpleNamespace, world: Any) -> None:
    ids = kit.Ids()
    plan = pdf_world(kit, world, ids)
    world.documents.fail.add("download")
    output = await extract_document.run(kit.plan_payload(ids, plan), world.provider)
    assert output["errorCode"] == "storage_unavailable"
    assert output["retryable"] is True


# --- The activity deadline (T-146) ---------------------------------------------------------------

_REAL_WAIT_SECONDS = 0.05


class _VirtualTimeSelector(selectors.DefaultSelector):
    """Waits only for real events: worker threads wake the loop through its self-pipe within a
    short real wait, and when nothing does, the loop holds only timers, so time jumps to the
    next one."""

    def __init__(self, clock: FakeClock) -> None:
        super().__init__()
        self.clock = clock

    @override
    def select(self, timeout: float | None = None) -> list[tuple[selectors.SelectorKey, int]]:
        if timeout is None or timeout <= 0:
            return super().select(timeout)
        events: list[tuple[selectors.SelectorKey, int]] = super().select(
            min(timeout, _REAL_WAIT_SECONDS)
        )
        if not events:
            self.clock.advance(timeout)
        return events


class _VirtualTimeLoop(asyncio.SelectorEventLoop):
    """An event loop whose clock is a `FakeClock`, so `asyncio.timeout` deadlines pass in
    virtual time."""

    def __init__(self, clock: FakeClock) -> None:
        super().__init__(_VirtualTimeSelector(clock))
        self._virtual_clock = clock

    @override
    def time(self) -> float:
        return self._virtual_clock.monotonic()


def test_t146_a_download_that_never_returns_ends_at_the_deadline_and_frees_the_slot(
    kit: SimpleNamespace, world: Any
) -> None:
    ids = kit.Ids()
    plan = pdf_world(kit, world, ids)
    downloads: list[str] = []

    async def never_returns(blob: str, **kwargs: Any) -> Any:
        downloads.append(blob)
        await asyncio.Event().wait()

    world.documents.download_blob = never_returns
    clock = FakeClock()
    started = clock.monotonic()

    # A private loop on virtual time, so the 2,700-second deadline passes without a wait.
    with asyncio.Runner(debug=False, loop_factory=lambda: _VirtualTimeLoop(clock)) as runner:
        output = runner.run(extract_document.run(kit.plan_payload(ids, plan), world.provider))

    assert downloads == [plan.source_blob_path]
    assert output["ok"] is False
    assert output["errorCode"] == "activity_timeout"
    assert output["retryable"] is True
    assert clock.monotonic() - started == extract_document.DEADLINE_SECONDS == 2700
    assert world.pool.taken == 1
    assert world.pool.held == 0
    assert world.pool.commands() == []


async def test_an_inspect_refusal_ends_the_item_before_any_parse(
    kit: SimpleNamespace, world: Any
) -> None:
    ids = kit.Ids()
    plan = pdf_world(kit, world, ids)
    world.sql.script(kit.Result(), kit.Result())
    world.pool.inspections.append(kit.outcome(Command.INSPECT, "encrypted_document"))
    output = await extract_document.run(kit.plan_payload(ids, plan), world.provider)
    assert output["errorCode"] == "encrypted_document"
    assert world.pool.commands() == ["inspect"]
    assert not world.work.blobs


async def test_the_page_cap(kit: SimpleNamespace, world: Any) -> None:
    ids = kit.Ids()
    plan = pdf_world(kit, world, ids)
    world.sql.script(kit.Result(), kit.Result())
    payload = kit.plan_payload(ids, plan)
    payload["settings"]["maxPagesPerDocument"] = 2
    output = await extract_document.run(payload, world.provider)
    assert output["errorCode"] == "page_limit_exceeded"
    assert world.pool.commands() == ["inspect"]


async def test_busy_parse_users_are_parse_timeout(kit: SimpleNamespace, world: Any) -> None:
    ids = kit.Ids()
    plan = pdf_world(kit, world, ids)
    world.pool.unavailable = True
    output = await extract_document.run(kit.plan_payload(ids, plan), world.provider)
    assert output["errorCode"] == "parse_timeout"


async def test_t191_a_spent_budget_starts_no_partition(kit: SimpleNamespace, world: Any) -> None:
    ids = kit.Ids()
    plan = pdf_world(kit, world, ids, parse_seconds_used=7198)
    world.sql.script(kit.Result(), kit.Result(), kit.Result())
    output = await extract_document.run(kit.plan_payload(ids, plan), world.provider)
    assert (output["errorCode"], output["retryable"]) == ("parse_timeout", False)
    assert world.pool.commands() == ["inspect"]
    world.sql.connection.finished()


async def test_the_partition_gets_what_is_left_of_the_budget(
    kit: SimpleNamespace, world: Any
) -> None:
    ids = kit.Ids()
    plan = pdf_world(kit, world, ids, parse_seconds_used=7000)
    world.sql.script(kit.Result(), kit.Result(), kit.Result(), kit.Result())
    output = await extract_document.run(kit.plan_payload(ids, plan), world.provider)
    assert output["ok"] is True, output
    assert world.pool.partition_limits == [198]


async def test_a_failed_partition_keeps_its_code_and_counts_its_seconds(
    kit: SimpleNamespace, world: Any
) -> None:
    ids = kit.Ids()
    plan = pdf_world(kit, world, ids)
    world.sql.script(kit.Result(), kit.Result(), kit.Result(), kit.Result())
    world.pool.partitions.append(
        kit.outcome(Command.PARTITION, "parse_memory_exceeded", exit_kind="memory", seconds=40)
    )
    output = await extract_document.run(kit.plan_payload(ids, plan), world.provider)
    assert (output["errorCode"], output["retryable"]) == ("parse_memory_exceeded", False)
    assert parse_seconds(world) == [2, 40]
    assert not world.work.blobs


async def test_a_pdf_pikepdf_cant_copy_is_corrupt_document(
    kit: SimpleNamespace, world: Any
) -> None:
    ids = kit.Ids()
    broken = b"%PDF-1.7 no objects"
    plan = kit.make_plan(ids).model_copy(update={"source_sha256": kit.sha(broken)})
    world.documents.blobs[plan.source_blob_path] = broken
    world.sql.script(kit.fence_ok(), kit.build_facts())
    world.sql.script(kit.Result(), kit.Result(), kit.Result(), kit.Result())
    output = await extract_document.run(kit.plan_payload(ids, plan), world.provider)
    assert (output["errorCode"], output["retryable"]) == ("corrupt_document", False)
    assert world.pool.commands() == ["inspect", "partition"]


# --- PDF versions, recursive ----------------------------------------------------------------------


async def test_the_recursive_method_analyses_the_downloaded_pdf_without_the_slot(
    kit: SimpleNamespace, world: Any, telemetry: TestExporters
) -> None:
    ids = kit.Ids()
    plan = pdf_world(kit, world, ids, method="recursive")
    world.sql.script(kit.Result(), kit.Result(), kit.Result())
    output = await extract_document.run(kit.plan_payload(ids, plan), world.provider)
    assert output["ok"] is True, output
    assert output["nextPart"] is None
    assert output["docIntelPages"] == 3
    assert world.pool.commands() == ["inspect"]
    # One download serves the inspect child and the layout call, which runs after the slot
    # went back to the pool.
    assert [entry for entry in world.log if entry[1] == "download"] == [
        ("documents", "download", plan.source_blob_path)
    ]
    assert world.layout.slot_held_at_analyze == [False]
    assert (world.pool.taken, world.pool.held) == (1, 0)
    document = decode_extract_document(world.work.blobs[extract_document_path(plan.work_prefix)], 3)
    assert [offset.page_number for offset in document.page_offsets] == [1, 2, 3]
    assert document.doc_intel_pages == 3
    points = kit.metric_points(telemetry, "docchat.ingestion.docintel.pages")
    assert sum(point.value for point in points) == 3
    assert parse_seconds(world) == [2]
    world.sql.connection.finished()


async def test_a_throttled_layout_service_is_retryable_with_its_wait(
    kit: SimpleNamespace, world: Any, telemetry: TestExporters
) -> None:
    ids = kit.Ids()
    plan = pdf_world(kit, world, ids, method="recursive")
    world.sql.script(kit.Result(), kit.Result(), kit.Result())
    world.layout.refusals.append(
        kit.docintel_exchange(
            "POST",
            kit.ANALYZE_PATH,
            429,
            {"error": {"code": "TooManyRequests", "message": "slow down"}},
            **{"Retry-After": "30"},
        )
    )
    output = await extract_document.run(kit.plan_payload(ids, plan), world.provider)
    assert output["errorCode"] == "docintel_throttled"
    assert output["retryAfterSeconds"] == 30
    assert output["retryable"] is True
    throttled = kit.metric_points(telemetry, "docchat.ingestion.throttled")
    assert [dict(point.attributes) for point in throttled] == [{"service": "docintel"}]
    assert extract_document_path(plan.work_prefix) not in world.work.blobs


async def test_an_activity_rule_holds_whatever_the_method(kit: SimpleNamespace, world: Any) -> None:
    pagewise, _ = world.fake_methods()
    ids = kit.Ids()
    plan = pdf_world(kit, world, ids)
    world.sql.script(kit.Result(), kit.Result(), kit.Result())
    pagewise.failure = MethodFailedError("docintel_unavailable", retry_after_seconds=30)
    output = await extract_document.run(kit.plan_payload(ids, plan), world.provider)
    assert (output["errorCode"], output["retryAfterSeconds"]) == ("docintel_unavailable", 30)
    assert output["retryable"] is True


# --- The ports themselves -------------------------------------------------------------------------


class _Call:
    def __init__(self, kit: SimpleNamespace, world: Any) -> None:
        self.services = world.services
        self.max_pages = 2000
        self.budget = extract_document.Budget(world.services, kit.Ids().chunk_set, 7200, 0)


def _context(kit: SimpleNamespace, page_count: int = 3) -> MethodContext:
    """A PDF version's context: its PDF is the plan's source, the bytes `PDF_BYTES`."""
    ids = kit.Ids()
    item = PlanInput.model_validate(kit.plan_payload(ids, kit.make_plan(ids)))
    return method_context(item, BuildFacts(page_count, None, None, 0, "building"))


async def _prepared(kit: SimpleNamespace, world: Any) -> Any:
    held = extract_document.HeldSlot()
    slot = await held.take(world.services.parse_pool)
    directory = slot.scratch()
    directory.write("source.bin", kit.PDF_BYTES)
    return held, extract_document.Prepared(3, kit.sha(kit.PDF_BYTES), directory)


async def test_a_port_serves_one_use_of_the_pdf_it_prepared(
    kit: SimpleNamespace, world: Any
) -> None:
    held, prepared = await _prepared(kit, world)
    ports = extract_document.ExtractPorts(_Call(kit, world), held, prepared)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="another PDF"):
        await ports.partition(_context(kit, page_count=4), PageRange(1, 2), {})
    ctx = _context(kit)
    async with ports.open(ctx) as stream:
        # The layout call starts no child, so the slot is back in the pool.
        assert world.pool.held == 0
        assert stream.read() == kit.PDF_BYTES
    with pytest.raises(extract_document.PortMisuseError):
        await ports.partition(ctx, PageRange(1, 2), {})
    await held.release()


async def test_a_port_partitions_once(kit: SimpleNamespace, world: Any) -> None:
    held, prepared = await _prepared(kit, world)
    call = _Call(kit, world)
    world.sql.script(kit.Result())
    ports = extract_document.ExtractPorts(call, held, prepared)  # type: ignore[arg-type]
    ctx = _context(kit)
    parameters: dict[str, JsonValue] = {"summarizeTables": True, "ocrLanguages": ["eng"]}
    raw = await ports.partition(ctx, PageRange(1, 2), parameters)
    assert parse_partition(raw, page_count=3, first=1, last=2).first_page == 1
    assert call.budget.used == 3
    with pytest.raises(extract_document.PortMisuseError):
        await ports.partition(ctx, PageRange(1, 2), parameters)
    with pytest.raises(extract_document.PortMisuseError):
        async with ports.open(ctx):
            pass
    await held.release()
    again = extract_document.ExtractPorts(call, held, prepared)  # type: ignore[arg-type]
    with pytest.raises(extract_document.PortMisuseError):
        await again.partition(ctx, PageRange(1, 2), parameters)


# --- DOCX and PPTX --------------------------------------------------------------------------------


def office_world(kit: SimpleNamespace, world: Any, ids: Any, **plan: Any) -> Any:
    built = kit.make_plan(ids, content_type=DOCX, **plan)
    world.documents.blobs[built.source_blob_path] = kit.PDF_BYTES
    world.sql.script(kit.fence_ok(), kit.build_facts(page_count=None))
    return built


async def test_the_conversion_call_converts_and_parses_nothing(
    kit: SimpleNamespace, world: Any
) -> None:
    ids = kit.Ids()
    plan = office_world(kit, world, ids)
    world.sql.script(*(kit.Result() for _ in range(5)))
    output = await extract_document.run(kit.plan_payload(ids, plan), world.provider)
    assert output["ok"] is True, output
    assert output["nextPart"] == {"firstPage": 1, "lastPage": 2}
    assert world.pool.commands() == ["inspect", "convert", "inspect"]
    office_inspect, convert, pdf_inspect = world.pool.calls
    assert office_inspect[1] == DOCX
    assert office_inspect[2] == {"source.bin": kit.PDF_BYTES}
    assert convert[2] == {"source.bin": kit.PDF_BYTES}
    assert pdf_inspect[1] == "application/pdf"
    assert pdf_inspect[2] == {"source.bin": kit.CONVERTED_BYTES}
    assert list(world.work.blobs) == [plan.work_pdf_path()]
    assert world.work.blobs[plan.work_pdf_path()] == kit.CONVERTED_BYTES
    statements = [entry.sql for entry in world.sql.connection.executed]
    assert (
        sum(sql.startswith("UPDATE app.chunk_sets SET parse_seconds_used") for sql in statements)
        == 3
    )
    (page_count,) = [sql for sql in statements if "SET page_count" in sql]
    assert "IS NULL" not in page_count
    (pdf_sha,) = world.sql.connection.statements("UPDATE app.chunk_sets SET pdf_sha256")
    assert pdf_sha.values["pdf_sha256"] == bytes.fromhex(kit.sha(kit.CONVERTED_BYTES))
    assert world.pool.convert_limits == [900]
    world.sql.connection.finished()


async def test_a_stored_converted_pdf_is_reused(kit: SimpleNamespace, world: Any) -> None:
    ids = kit.Ids()
    plan = office_world(kit, world, ids, convert=False)
    world.documents.blobs[plan.conversion.converted_blob_path] = kit.CONVERTED_BYTES
    world.sql.script(kit.Result(), kit.Result(), kit.Result())
    output = await extract_document.run(kit.plan_payload(ids, plan), world.provider)
    assert output["ok"] is True, output
    assert world.pool.commands() == ["inspect"]
    assert output["warnings"] == []
    assert world.work.blobs[plan.work_pdf_path()] == kit.CONVERTED_BYTES


async def test_a_missing_stored_pdf_converts_with_a_warning(
    kit: SimpleNamespace, world: Any
) -> None:
    ids = kit.Ids()
    plan = office_world(kit, world, ids, convert=False)
    world.sql.script(*(kit.Result() for _ in range(5)))
    output = await extract_document.run(kit.plan_payload(ids, plan), world.provider)
    assert output["warnings"] == [{"code": "converted_pdf_missing", "page": None}]
    assert world.pool.commands() == ["inspect", "convert", "inspect"]


async def test_a_changed_stored_pdf_is_source_changed(kit: SimpleNamespace, world: Any) -> None:
    ids = kit.Ids()
    plan = office_world(kit, world, ids, convert=False)
    world.documents.blobs[plan.conversion.converted_blob_path] = b"changed"
    output = await extract_document.run(kit.plan_payload(ids, plan), world.provider)
    assert output["errorCode"] == "source_changed"


async def test_a_converted_pdf_that_inspect_refuses_is_convert_failed(
    kit: SimpleNamespace, world: Any
) -> None:
    ids = kit.Ids()
    plan = office_world(kit, world, ids)
    world.sql.script(*(kit.Result() for _ in range(4)))
    world.pool.inspections.extend(
        [
            kit.outcome(Command.INSPECT, pages=None),
            kit.outcome(Command.INSPECT, "encrypted_document"),
        ]
    )
    output = await extract_document.run(kit.plan_payload(ids, plan), world.provider)
    assert output["errorCode"] == "convert_failed"


async def test_an_office_source_refusal_comes_before_any_conversion(
    kit: SimpleNamespace, world: Any
) -> None:
    ids = kit.Ids()
    plan = office_world(kit, world, ids)
    world.sql.script(kit.Result())
    world.pool.inspections.append(kit.outcome(Command.INSPECT, "macro_content_declared"))
    output = await extract_document.run(kit.plan_payload(ids, plan), world.provider)
    assert output["errorCode"] == "macro_content_declared"
    assert world.pool.commands() == ["inspect"]


async def test_the_converter_timeout(kit: SimpleNamespace, world: Any) -> None:
    ids = kit.Ids()
    plan = office_world(kit, world, ids)
    world.sql.script(kit.Result(), kit.Result())
    world.pool.inspections.append(kit.outcome(Command.INSPECT, pages=None))
    world.pool.conversion = (
        kit.outcome(Command.CONVERT, "convert_timeout", exit_kind="timeout", seconds=900),
        None,
    )
    output = await extract_document.run(kit.plan_payload(ids, plan), world.provider)
    assert output["errorCode"] == "convert_timeout"


async def test_t191_a_spent_budget_starts_no_converter(kit: SimpleNamespace, world: Any) -> None:
    ids = kit.Ids()
    plan = kit.make_plan(ids, content_type=DOCX)
    world.documents.blobs[plan.source_blob_path] = kit.PDF_BYTES
    world.sql.script(kit.fence_ok(), kit.build_facts(page_count=None, parse_seconds_used=7199))
    world.sql.script(kit.Result())
    world.pool.inspections.append(kit.outcome(Command.INSPECT, pages=None, seconds=1))
    output = await extract_document.run(kit.plan_payload(ids, plan), world.provider)
    assert output["errorCode"] == "parse_timeout"
    assert "convert" not in world.pool.commands()


async def test_the_converter_gets_what_is_left_of_the_budget(
    kit: SimpleNamespace, world: Any
) -> None:
    ids = kit.Ids()
    plan = kit.make_plan(ids, content_type=DOCX)
    world.documents.blobs[plan.source_blob_path] = kit.PDF_BYTES
    world.sql.script(kit.fence_ok(), kit.build_facts(page_count=None, parse_seconds_used=7000))
    world.sql.script(*(kit.Result() for _ in range(5)))
    output = await extract_document.run(kit.plan_payload(ids, plan), world.provider)
    assert output["ok"] is True
    assert world.pool.convert_limits == [198]


async def test_t616_a_later_call_partitions_the_work_copy(kit: SimpleNamespace, world: Any) -> None:
    ids = kit.Ids()
    plan = kit.make_plan(ids, content_type=DOCX)
    world.work.blobs[plan.work_pdf_path()] = kit.CONVERTED_BYTES
    world.sql.script(
        kit.fence_ok(),
        kit.build_facts(page_count=3, pdf_sha256=bytes.fromhex(kit.sha(kit.CONVERTED_BYTES))),
        kit.Result(),
        kit.Result(),
    )
    output = await extract_document.run(
        kit.plan_payload(ids, plan, part={"firstPage": 1, "lastPage": 2}), world.provider
    )
    assert output["ok"] is True, output
    assert ("documents", "download", plan.source_blob_path) not in world.log
    assert world.pool.calls[-1][1:] == ("1-2", {"source.bin": kit.CONVERTED_BYTES})
    assert extract_part_path(plan.work_prefix, 1, 2) in world.work.blobs
    world.sql.connection.finished()
