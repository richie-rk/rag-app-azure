"""Daily maintenance and the scanner canaries against SQL Server and Azurite: T-090, the retention
integration test, and T-159, the scanner canary integration test (`14-ingestion.md` sections 5.5
and 7.2; `80-data-model.md` sections 2.19, 2.25 and acceptance criterion 21).

The worker process is the one `conftest.py` builds over the compose services; the Durable task
hub isn't a compose service, so the history purge reaches a recorder of the client's call.
"""

from __future__ import annotations

import asyncio
import dataclasses
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID

import pytest
from azure.storage.blob.aio import ContainerClient
from sqlalchemy import Connection, Engine, Table, insert, select

from docchat_core.ingestion.models import CanaryResult
from docchat_core.ingestion.names import scanner_canary_path
from docchat_core.sql.engine import Retry
from docchat_core.sql.tables.access import projects
from docchat_core.sql.tables.ingestion import (
    ingestion_run_items,
    ingestion_runs,
    physical_indexes,
    scan_canaries,
    search_indexes,
)
from docchat_ingestion import starters
from docchat_ingestion.activities import scan_results
from docchat_ingestion.activities.runtime import ContainerPort, IngestionProcess, SqlRunner
from docchat_testing.builders import project_row
from docchat_testing.builders.ingestion import (
    ingestion_run_item_row,
    ingestion_run_row,
    physical_index_row,
    scan_canary_row,
    search_index_row,
)
from docchat_testing.builders.rows import sql_now
from docchat_testing.clock import FakeClock
from docchat_testing.fixtures.sql import SqlTestDatabase
from docchat_testing.fixtures.storage import StorageClients
from docchat_testing.telemetry_capture import TelemetryCapture

pytestmark = pytest.mark.integration

type ProcessFactory = Callable[..., IngestionProcess]
"""`conftest.make_process`."""

SCAN_RESULT_TAG = "Malware scanning scan result"
SCAN_TIME_TAG = "Malware scanning scan time"


@pytest.fixture
def admin(sql_database: SqlTestDatabase) -> Iterator[Engine]:
    engine = sql_database.engine()
    yield engine
    engine.dispose()


def put(engine: Engine, table: Table, *rows: dict[str, Any]) -> None:
    with engine.begin() as connection:
        connection.execute(insert(table), list(rows))


def ids_of(engine: Engine, table: Table) -> set[UUID]:
    with engine.connect() as connection:
        return {row.id for row in connection.execute(select(table.c.id))}


@dataclass
class CountingSql:
    """The data-access layer, recording what each unit of work returned."""

    inner: SqlRunner
    results: list[tuple[str, Any]] = field(default_factory=list)

    async def run[T](self, work: Callable[[Connection], T], *, name: str, retry: Retry) -> T:
        result = await self.inner.run(work, name=name, retry=retry)
        self.results.append((name, result))
        return result

    def returned(self, name: str) -> list[Any]:
        return [result for unit, result in self.results if unit == name]


@dataclass
class OrderedDocuments:
    """The `documents` container, recording at each blob delete whether the canary row of that
    blob still existed."""

    inner: ContainerClient
    admin: Engine
    row_existed: dict[str, bool] = field(default_factory=dict)

    def _row_exists(self, blob: str) -> bool:
        with self.admin.connect() as connection:
            found = connection.execute(
                select(scan_canaries.c.id).where(scan_canaries.c.blob_path == blob)
            ).first()
        return found is not None

    async def delete_blob(self, blob: str, **kwargs: Any) -> None:
        self.row_existed[blob] = await asyncio.to_thread(self._row_exists, blob)
        await self.inner.delete_blob(blob, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)


class Purger:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def purge_instance_history_by(self, **kwargs: Any) -> None:
        self.calls.append(kwargs)


def _provide(process: IngestionProcess) -> Callable[[], Any]:
    async def provide() -> IngestionProcess:
        return process

    return provide


async def _exists(container: ContainerClient, blob: str) -> bool:
    return bool(await container.get_blob_client(blob).exists())


# T-090


async def test_t090_retention_deletes_old_rows_in_batches_and_keeps_the_rest(
    make_process: ProcessFactory, admin: Engine, storage: StorageClients
) -> None:
    project, index = project_row(), search_index_row(status="ready")
    old = sql_now() - timedelta(days=181)
    old_run = ingestion_run_row(index["id"], state="completed", completed_at=old)
    young_run = ingestion_run_row(index["id"], state="completed", completed_at=sql_now())
    named_run = ingestion_run_row(
        index["id"], kind="index_rebuild", priority=1, state="completed", completed_at=old
    )
    building = physical_index_row(
        index["id"], generation=2, state="building", build_run_id=named_run["id"]
    )
    old_items = [ingestion_run_item_row(old_run["id"], index["id"]) for _ in range(501)]
    young_items = [ingestion_run_item_row(young_run["id"], index["id"]) for _ in range(2)]
    named_items = [ingestion_run_item_row(named_run["id"], index["id"])]
    documents = storage.container("documents")
    old_open = scan_canary_row(written_at=sql_now() - timedelta(days=91))
    old_closed = scan_canary_row(
        written_at=sql_now() - timedelta(days=91), result="clean", minutes_to_result=3
    )
    young = scan_canary_row(written_at=sql_now() - timedelta(days=89))

    def arrange() -> None:
        put(admin, projects, project)
        put(admin, search_indexes, index)
        put(admin, ingestion_runs, old_run, young_run, named_run)
        put(admin, physical_indexes, building)
        put(admin, ingestion_run_items, *old_items, *young_items, *named_items)
        put(admin, scan_canaries, old_open, old_closed, young)

    await asyncio.to_thread(arrange)
    for canary in (old_open, young):
        await documents.upload_blob(canary["blob_path"], b"%PDF-1.7", overwrite=True)
    clock = FakeClock(datetime.now(UTC))
    process = make_process(clock=clock)
    counting = CountingSql(process.sql)
    ordered = OrderedDocuments(documents, admin)
    process = dataclasses.replace(process, sql=counting, documents=cast(ContainerPort, ordered))
    purger = Purger()

    await starters.run_maintenance(cast(starters.HistoryPurger, purger), _provide(process))

    snapshot = (await process.ops.snapshot()).ingestion
    assert snapshot.run_retention_days == 180
    (purge,) = purger.calls
    assert purge["created_time_to"] == clock.now() - timedelta(
        days=snapshot.durable_history_retention_days
    )
    assert snapshot.durable_history_retention_days == 30
    assert counting.returned("maintenance_run_items")[:2] == [500, 2]
    for unit in ("maintenance_run_items", "maintenance_runs", "maintenance_canaries"):
        assert all(0 <= count <= starters.RETENTION_BATCH for count in counting.returned(unit))
    items = await asyncio.to_thread(ids_of, admin, ingestion_run_items)
    runs = await asyncio.to_thread(ids_of, admin, ingestion_runs)
    canaries = await asyncio.to_thread(ids_of, admin, scan_canaries)
    assert items == {item["id"] for item in young_items}
    assert runs == {young_run["id"], named_run["id"]}
    assert canaries == {young["id"]}
    assert ordered.row_existed == {old_open["blob_path"]: True}
    assert not await _exists(documents, old_open["blob_path"])
    assert await _exists(documents, young["blob_path"])


# T-159


def _is_canary_result(record: Any) -> bool:
    attributes = dict(record.log_record.attributes or {})
    return record.log_record.body == "scanner_canary_result" or any(
        value == "scanner_canary_result" for value in attributes.values()
    )


async def test_t159_maintenance_writes_the_row_then_the_blob_and_the_sweep_resolves_it_once(
    make_process: ProcessFactory,
    admin: Engine,
    storage: StorageClients,
    telemetry_capture: TelemetryCapture,
) -> None:
    documents = storage.container("documents")
    process = make_process(scanning_enabled=True)
    counting = CountingSql(process.sql)
    process = dataclasses.replace(process, sql=counting)
    events_before = sum(1 for r in telemetry_capture.log_records() if _is_canary_result(r))

    await starters.write_daily_canary(process)

    (canary_id,) = await asyncio.to_thread(ids_of, admin, scan_canaries)
    path = scanner_canary_path(canary_id)
    assert path == f"_scanner-canary/{canary_id}.pdf"
    assert [name for name, _ in counting.results] == ["maintenance_canary_write"]
    assert await _exists(documents, path)
    # The scan runs after the write: a whole-second tag of the current second can fall before
    # the millisecond `written_at`, which section 5.5 reads as no usable result.
    scanned = (datetime.now(UTC) + timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    await documents.get_blob_client(path).set_blob_tags(
        {SCAN_RESULT_TAG: "No threats found", SCAN_TIME_TAG: scanned}
    )

    await scan_results.resolve_canaries(process, scan_wait_minutes=240)
    await scan_results.resolve_canaries(process, scan_wait_minutes=240)

    def stored() -> Any:
        with admin.connect() as connection:
            return connection.execute(
                select(scan_canaries).where(scan_canaries.c.id == canary_id)
            ).one()

    row = await asyncio.to_thread(stored)
    assert row.result == CanaryResult.CLEAN.value
    assert row.scanned_at is not None
    assert not await _exists(documents, path)
    events_after = sum(1 for r in telemetry_capture.log_records() if _is_canary_result(r))
    assert events_after - events_before == 1


async def test_t159_with_no_row_at_all_the_sweep_writes_the_first_canary(
    make_process: ProcessFactory, admin: Engine, storage: StorageClients
) -> None:
    assert await asyncio.to_thread(ids_of, admin, scan_canaries) == set()
    process = make_process(scanning_enabled=True)

    await scan_results.resolve_canaries(process, scan_wait_minutes=240)

    (canary_id,) = await asyncio.to_thread(ids_of, admin, scan_canaries)
    assert await _exists(storage.container("documents"), scanner_canary_path(canary_id))
