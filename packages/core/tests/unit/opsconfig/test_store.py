"""The `OpsConfigStore` unit tests (T-120; `81-config.md` section 3.1 items 3 to 6 and acceptance
criterion 8), with a fake clock and a fake SQL reader in place of the data-access layer."""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

import pytest
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from sqlalchemy.dialects import mssql
from sqlalchemy.sql.base import Executable

from docchat_core.opsconfig.root import API_AREAS, INGESTION_AREAS, AreaUnavailableError
from docchat_core.opsconfig.store import (
    READ_ALL,
    READ_STATEMENT_NAME,
    OpsConfigInvalidError,
    OpsConfigStore,
    RowReader,
)
from docchat_core.opsconfig.values import encode_value
from docchat_core.settings import StartupCheckError
from docchat_core.sql.engine import Retry, SqlEngine, SqlUnavailable


class Captured(Protocol):
    """The `captured` fixture of this directory's `conftest.py`."""

    spans: InMemorySpanExporter

    def finished_spans(self, name: str) -> list[ReadableSpan]: ...

    def counts(self, metric_name: str) -> dict[tuple[tuple[str, object], ...], int]: ...

    def records(self, message: str) -> list[logging.LogRecord]: ...


EFFORTS = ("none", "low", "medium", "high")
REFRESH_SPAN = "docchat.opsconfig.refresh"
EVENT_ATTRIBUTE = "microsoft.custom_event.name"


def seeded_rows() -> dict[str, str]:
    """`app.ops_config` right after the first revision: the 73 rows of `80-data-model.md`
    section 2.24, `config_key` to `value_json`."""
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "docs" / "spec" / "80-data-model.md"
        if candidate.is_file():
            text = candidate.read_text(encoding="utf-8")
            return dict(re.findall(r"^\| `([a-z_.]+)` \| `(\{.*?\})` \| `\w+` \|$", text, re.M))
    raise FileNotFoundError("80-data-model.md")


class FakeClock:
    def __init__(self) -> None:
        self._now = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)
        self._monotonic = 5_000.0

    def now(self) -> datetime:
        return self._now

    def monotonic(self) -> float:
        return self._monotonic

    def advance(self, seconds: float) -> None:
        self._now += timedelta(seconds=seconds)
        self._monotonic += seconds


class FakeReader:
    """The SQL reader: serves the current rows, or fails as an unreachable database does."""

    def __init__(self, rows: Mapping[str, str]) -> None:
        self.rows = dict(rows)
        self.unreachable = False
        self.calls: list[tuple[Executable, str, Retry | None]] = []
        self.gate: asyncio.Event | None = None

    @property
    def reads(self) -> int:
        return len(self.calls)

    async def fetch_all(
        self,
        statement: Executable,
        parameters: Mapping[str, Any] | None = None,
        *,
        name: str,
        retry: Retry | None = None,
    ) -> Sequence[Sequence[Any]]:
        assert parameters is None
        self.calls.append((statement, name, retry))
        if self.gate is not None:
            await self.gate.wait()
        if self.unreachable:
            raise SqlUnavailable("connection", 10054, "08S01")
        return [(key, value) for key, value in self.rows.items()]

    def set(self, key: str, value: Any) -> None:
        self.rows[key] = encode_value(value)


def _engine_is_a_reader(engine: SqlEngine) -> RowReader:
    # A type check only: the store takes the data-access layer as it is.
    return engine


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def reader() -> FakeReader:
    return FakeReader(seeded_rows())


def api_store(reader: FakeReader, clock: FakeClock, **options: Any) -> OpsConfigStore:
    return OpsConfigStore(reader, API_AREAS, chat_small_efforts=EFFORTS, clock=clock, **options)


def span_outcomes(captured: Captured) -> list[tuple[object, object]]:
    return [
        (
            (span.attributes or {}).get("docchat.opsconfig.outcome"),
            (span.attributes or {}).get("docchat.opsconfig.invalid_areas"),
        )
        for span in captured.finished_spans(REFRESH_SPAN)
    ]


async def test_t120_start_reads_every_row_once_and_holds_the_registered_areas(
    reader: FakeReader, clock: FakeClock, captured: Captured
) -> None:
    store = api_store(reader, clock)
    snapshot = await store.start()
    assert reader.calls == [(READ_ALL, READ_STATEMENT_NAME, None)]
    assert snapshot.chat.context_token_budget == 40_000
    assert snapshot.retrieval.reranker_threshold == 2.0
    assert snapshot.pricing.currency == "USD"
    assert snapshot.memory.enabled is True
    assert snapshot.ingestion_area is None
    with pytest.raises(AreaUnavailableError):
        _ = snapshot.ingestion
    assert store.areas == API_AREAS
    assert await store.snapshot() is snapshot
    assert reader.reads == 1
    assert span_outcomes(captured) == [("ok", 0)]


def test_the_read_is_the_one_full_statement() -> None:
    sql = str(READ_ALL.compile(dialect=mssql.dialect()))  # type: ignore[no-untyped-call]
    assert " ".join(sql.split()) == (
        "SELECT app.ops_config.config_key, app.ops_config.value_json FROM app.ops_config"
    )
    assert READ_STATEMENT_NAME == "read_ops_config"


async def test_t120_rows_of_unregistered_areas_and_undeclared_keys_are_ignored(
    reader: FakeReader, clock: FakeClock
) -> None:
    reader.rows["future.setting"] = '{"value":1}'
    reader.rows["chat.added_by_a_later_release"] = "not json"
    reader.set("ingestion.max_parallel_items", 0)
    snapshot = await api_store(reader, clock).start()
    assert snapshot.chat.max_turn_seconds == 180
    ingestion = OpsConfigStore(reader, INGESTION_AREAS, clock=clock)
    reader.set("ingestion.max_parallel_items", 4)
    reader.set("chat.max_turn_seconds", 0)
    assert (await ingestion.start()).ingestion.max_parallel_items == 4


async def test_t121_start_fails_when_a_registered_area_misses_a_seeded_key(
    reader: FakeReader, clock: FakeClock, captured: Captured
) -> None:
    del reader.rows["chat.max_turn_seconds"]
    with pytest.raises(OpsConfigInvalidError) as raised:
        await api_store(reader, clock).start()
    assert raised.value.failures == {"chat": ("chat.max_turn_seconds",)}
    assert isinstance(raised.value, StartupCheckError)
    assert raised.value.record() == (
        '{"level": "error", "name": "settings_validation", '
        '"message": "Ops config validation failed.", "settings": ["chat.max_turn_seconds"]}'
    )
    (record,) = captured.records("ops_config_invalid")
    assert record.levelno == logging.ERROR
    assert record.__dict__["docchat.opsconfig.area"] == "chat"
    assert record.__dict__["docchat.opsconfig.key"] == "chat.max_turn_seconds"
    # The process exits before it serves: no running-process signals.
    assert captured.counts("docchat.config.invalid") == {}
    assert span_outcomes(captured) == [("partial", 1)]


async def test_t120_start_fails_on_an_invalid_value_and_names_only_the_key(
    reader: FakeReader, clock: FakeClock, captured: Captured
) -> None:
    reader.rows["chat.rewrite_history_turns"] = '{"value":"4"}'
    reader.rows["memory.max_facts"] = "{broken"
    reader.set("ingestion.max_parallel_items", 99)
    with pytest.raises(OpsConfigInvalidError) as raised:
        await api_store(reader, clock).start()
    assert raised.value.failures == {
        "chat": ("chat.rewrite_history_turns",),
        "memory": ("memory.max_facts",),
    }
    assert raised.value.settings == ("chat.rewrite_history_turns", "memory.max_facts")
    for record in captured.records("ops_config_invalid"):
        assert set(record.__dict__) >= {"docchat.opsconfig.area", "docchat.opsconfig.key"}
        assert "broken" not in str(record.__dict__)
        assert record.getMessage() == "ops_config_invalid"
    assert len(captured.records("ops_config_invalid")) == 2


async def test_start_raises_the_data_layer_failure_when_sql_is_unreachable(
    reader: FakeReader, clock: FakeClock, captured: Captured
) -> None:
    reader.unreachable = True
    with pytest.raises(SqlUnavailable):
        await api_store(reader, clock).start()
    assert span_outcomes(captured) == [("unreachable", 0)]
    assert not captured.spans.get_finished_spans()[0].events


async def test_t120_a_value_changed_in_sql_is_used_within_60_s_plus_one_request(
    reader: FakeReader, clock: FakeClock
) -> None:
    store = api_store(reader, clock)
    first = await store.start()
    reader.set("ratelimit.chat_requests_per_minute", 5)
    clock.advance(59.999)
    assert (await store.snapshot()) is first
    assert reader.reads == 1
    clock.advance(0.001)
    refreshed = await store.snapshot()
    assert refreshed.ratelimit.chat_requests_per_minute == 5
    assert first.ratelimit.chat_requests_per_minute == 3
    assert reader.reads == 2
    clock.advance(30)
    assert (await store.snapshot()) is refreshed
    assert reader.reads == 2


async def test_t120_concurrent_callers_wait_for_one_refresh(
    reader: FakeReader, clock: FakeClock
) -> None:
    store = api_store(reader, clock)
    await store.start()
    reader.set("api.write_requests_per_minute", 200)
    clock.advance(60)
    reader.gate = asyncio.Event()
    waiting = [asyncio.create_task(store.snapshot()) for _ in range(5)]
    for _ in range(5):
        await asyncio.sleep(0)
    assert reader.reads == 2
    reader.gate.set()
    snapshots = await asyncio.gather(*waiting)
    assert all(snapshot is snapshots[0] for snapshot in snapshots)
    assert snapshots[0].api.write_requests_per_minute == 200
    assert reader.reads == 2


async def test_t120_an_invalid_stored_value_leaves_the_last_valid_area_in_use(
    reader: FakeReader, clock: FakeClock, captured: Captured
) -> None:
    store = api_store(reader, clock)
    await store.start()
    reader.set("chat.max_turn_seconds", 999)
    reader.set("api.write_requests_per_minute", 200)
    clock.advance(60)
    snapshot = await store.snapshot()
    assert snapshot.chat.max_turn_seconds == 180
    assert snapshot.api.write_requests_per_minute == 200
    assert store.invalid_keys("chat") == ("chat.max_turn_seconds",)
    assert store.invalid_keys("api") == ()
    assert captured.counts("docchat.config.invalid") == {
        (("area", "chat"), ("key", "chat.max_turn_seconds")): 1
    }
    event, record = captured.records("ops_config_invalid")
    assert event.__dict__[EVENT_ATTRIBUTE] == "ops_config_invalid"
    assert (event.__dict__["area"], event.__dict__["key"]) == ("chat", "chat.max_turn_seconds")
    assert record.levelno == logging.ERROR
    assert record.__dict__["docchat.opsconfig.key"] == "chat.max_turn_seconds"
    # The record's own fields (thread ID, timestamps) can hold "999" by chance; the message and
    # the attributes the code adds must not hold the stored value.
    standard = set(vars(logging.makeLogRecord({})))
    written = [
        str(value)
        for entry in (event, record)
        for name, value in vars(entry).items()
        if name not in standard
    ]
    assert not any("999" in text for text in [event.getMessage(), record.getMessage(), *written])
    assert span_outcomes(captured) == [("ok", 0), ("partial", 1)]
    # Corrected, the area is replaced at the next refresh.
    reader.set("chat.max_turn_seconds", 200)
    clock.advance(60)
    assert (await store.snapshot()).chat.max_turn_seconds == 200
    assert store.invalid_keys("chat") == ()


async def test_a_broken_rule_keeps_the_area_and_names_the_keys_of_the_rule(
    reader: FakeReader, clock: FakeClock, captured: Captured
) -> None:
    store = api_store(reader, clock)
    await store.start()
    reader.set("retrieval.keep_count", 16)
    reader.set("retrieval.candidate_count", 10)
    clock.advance(60)
    snapshot = await store.snapshot()
    assert (snapshot.retrieval.keep_count, snapshot.retrieval.candidate_count) == (8, 50)
    assert store.invalid_keys("retrieval") == ("retrieval.keep_count", "retrieval.candidate_count")
    assert set(captured.counts("docchat.config.invalid")) == {
        (("area", "retrieval"), ("key", "retrieval.keep_count")),
        (("area", "retrieval"), ("key", "retrieval.candidate_count")),
    }


async def test_t120_with_sql_unreachable_the_last_valid_snapshot_stays(
    reader: FakeReader, clock: FakeClock, captured: Captured
) -> None:
    store = api_store(reader, clock)
    first = await store.start()
    reader.set("api.write_requests_per_minute", 200)
    reader.unreachable = True
    clock.advance(60)
    assert (await store.snapshot()) is first
    assert reader.reads == 2
    (warning,) = captured.records("ops_config_unreachable")
    assert warning.levelno == logging.WARNING
    assert captured.counts("docchat.opsconfig.refresh_failures") == {
        (("reason", "unreachable"),): 1
    }
    assert span_outcomes(captured) == [("ok", 0), ("unreachable", 0)]
    # The next attempt is no sooner than 15 seconds later.
    clock.advance(14.999)
    assert (await store.snapshot()) is first
    assert reader.reads == 2
    reader.unreachable = False
    clock.advance(0.001)
    assert (await store.snapshot()).api.write_requests_per_minute == 200
    assert reader.reads == 3


async def test_ingestion_reports_its_own_invalid_area(
    reader: FakeReader, clock: FakeClock, captured: Captured
) -> None:
    store = OpsConfigStore(reader, INGESTION_AREAS, report_invalid=False, clock=clock)
    await store.start()
    reader.set("ingestion.max_parallel_items", 17)
    clock.advance(60)
    snapshot = await store.snapshot()
    assert snapshot.ingestion.max_parallel_items == 1
    assert store.invalid_keys("ingestion") == ("ingestion.max_parallel_items",)
    assert captured.counts("docchat.config.invalid") == {}
    assert captured.records("ops_config_invalid") == []


async def test_a_first_snapshot_without_start_holds_only_the_areas_that_validate(
    reader: FakeReader, clock: FakeClock
) -> None:
    reader.set("ingestion.idle_wake_minutes", 61)
    store = OpsConfigStore(reader, INGESTION_AREAS, report_invalid=False, clock=clock)
    snapshot = await store.snapshot()
    assert snapshot.ingestion_area is None
    with pytest.raises(AreaUnavailableError):
        _ = snapshot.ingestion
    assert store.invalid_keys("ingestion") == ("ingestion.idle_wake_minutes",)
    reader.set("ingestion.idle_wake_minutes", 60)
    clock.advance(60)
    assert (await store.snapshot()).ingestion.idle_wake_minutes == 60


async def test_with_no_snapshot_an_unreachable_database_raises_on_every_attempt(
    reader: FakeReader, clock: FakeClock, captured: Captured
) -> None:
    reader.unreachable = True
    store = OpsConfigStore(reader, INGESTION_AREAS, report_invalid=False, clock=clock)
    with pytest.raises(SqlUnavailable):
        await store.snapshot()
    with pytest.raises(SqlUnavailable):
        await store.snapshot()
    assert reader.reads == 2
    assert len(captured.records("ops_config_unreachable")) == 2


async def test_the_snapshot_can_not_be_mutated(reader: FakeReader, clock: FakeClock) -> None:
    snapshot = await api_store(reader, clock).start()
    with pytest.raises(ValueError, match="frozen"):
        snapshot.quota.tokens_per_day = 1  # type: ignore[misc]
    with pytest.raises(ValueError, match="frozen"):
        snapshot.chat_area = None  # type: ignore[misc]


def test_a_store_registers_known_areas_and_the_efforts_they_need(reader: FakeReader) -> None:
    with pytest.raises(ValueError, match="at least one"):
        OpsConfigStore(reader, ())
    with pytest.raises(ValueError, match="unknown"):
        OpsConfigStore(reader, ("chat", "nonsense"))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="efforts"):
        OpsConfigStore(reader, API_AREAS)
    with pytest.raises(ValueError, match="efforts"):
        OpsConfigStore(reader, ("memory",))
    store = OpsConfigStore(reader, ("chat", "chat", "api"))
    assert store.areas == ("chat", "api")


async def test_the_effort_rule_follows_the_chat_small_row(
    reader: FakeReader, clock: FakeClock
) -> None:
    reader.set("summary.reasoning_effort", "none")
    store = OpsConfigStore(
        reader, API_AREAS, chat_small_efforts=("low", "medium", "high"), clock=clock
    )
    with pytest.raises(OpsConfigInvalidError) as raised:
        await store.start()
    assert raised.value.failures == {"summary": ("summary.reasoning_effort",)}
