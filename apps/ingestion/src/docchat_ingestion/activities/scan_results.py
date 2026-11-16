"""The scan gate's result mapping and the scanner canaries (`14-ingestion.md` sections 5.3 duties 3
and 11, 5.4 and 5.5; ADRs 0038 and 0039).

Defender for Storage writes its result into two blob index tags; neither app identity can write a
tag, so only the scanner sets them. `map_scan_tags` is the one mapping of section 5.4, applied by
sweep duty 3 to versions (against `blob_last_modified`) and by duty 11 to canaries (against
`written_at`): only `No threats found` with a scan time at or after that instant is clean; every
other state fails closed. The tag keys are the constants the stage-0 scan probe (SP-3) reads; their
casing is matched without case, as the probe matched it, until SP-3's recorded outcome confirms it.

Each version's outcome commits in its own transaction, guarded so a version that left `pending`
or `awaiting` meanwhile changes nothing. A malicious source is deleted after the commit; a failed
delete is left to sweep duty 6, which purges failed versions.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import logging
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from functools import partial
from typing import Any, Final
from uuid import UUID, uuid4

import pikepdf
from azure.core.exceptions import ResourceNotFoundError
from azure.storage.blob import ContentSettings
from sqlalchemy import Connection, insert, select, update

from docchat_core.audit import AuditEvent, write_audit_event
from docchat_core.audit.models import DocumentMalwareDetectedMetadata
from docchat_core.ingestion.models import CanaryResult
from docchat_core.ingestion.names import scanner_canary_path
from docchat_core.sql.tables.ingestion import document_versions, ingestion_run_items, scan_canaries
from docchat_core.sql.tables.schema import BIT_TRUE, SYSUTCDATETIME
from docchat_core.telemetry import LOGGER_NAME, emit_event, log_event
from docchat_core.telemetry.instruments import counter
from docchat_ingestion.activities.runtime import ContainerPort, IngestionProcess
from docchat_ingestion.activities.support import catalogue_message, literal

SCAN_RESULT_KEY: Final = "Malware scanning scan result"
SCAN_TIME_KEY: Final = "Malware scanning scan time"
NO_THREATS: Final = "No threats found"
MALICIOUS: Final = "Malicious"
WAIT_CODES: Final = ("SAM259213", "SAM259215")
"""Throttled and delayed by the service: no result yet."""
RAW_RESULT_MAX: Final = 256

SCAN_BATCH: Final = 200
TAG_READS_AT_ONCE: Final = 8
OPEN_CANARIES: Final = 10
CANARY_PAGE_SIZE: Final = (612, 792)

FAILURE_CODES: Final[Mapping[CanaryResult, str]] = {
    CanaryResult.MALICIOUS: "malware_detected",
    CanaryResult.UNAVAILABLE: "scan_unavailable",
    CanaryResult.FAILED: "scan_failed",
}

SCAN_RESULTS: Final = counter("docchat.ingestion.scan.results")

logger: Final = logging.getLogger(LOGGER_NAME)


class Decision(StrEnum):
    """A reading's outcome: a result of section 5.4's table, or keep waiting."""

    CLEAN = "clean"
    MALICIOUS = "malicious"
    FAILED = "failed"
    UNAVAILABLE = "unavailable"
    WAITING = "waiting"


@dataclass(frozen=True, slots=True)
class ScanReading:
    decision: Decision
    scan_time: datetime | None = None
    raw_result: str | None = None
    time_unusable: bool = False
    """`No threats found` with a missing, unparseable or earlier scan time."""

    @property
    def result(self) -> CanaryResult | None:
        return None if self.decision is Decision.WAITING else CanaryResult(self.decision.value)


def _tag(tags: Mapping[str, str], key: str) -> str | None:
    folded = key.casefold()
    return next((value for name, value in tags.items() if name.casefold() == folded), None)


def parse_scan_time(value: str | None) -> datetime | None:
    """An ISO 8601 scan time as an aware UTC instant; `None` when missing or unparseable. A time
    without an offset is taken as UTC, the scanner's clock."""
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def map_scan_tags(
    tags: Mapping[str, str], *, not_before: datetime, deadline: datetime, now: datetime
) -> ScanReading:
    """Section 5.4's mapping of one blob's tags. `not_before` is the blob's last modification
    (or a canary's `written_at`), `deadline` the time after which no result is `unavailable`."""
    result = _tag(tags, SCAN_RESULT_KEY)
    scan_time = parse_scan_time(_tag(tags, SCAN_TIME_KEY))
    expired = now >= deadline
    if result == NO_THREATS:
        if scan_time is not None and scan_time >= not_before:
            return ScanReading(Decision.CLEAN, scan_time=scan_time)
        if expired:
            return ScanReading(Decision.UNAVAILABLE, time_unusable=True)
        return ScanReading(Decision.WAITING, time_unusable=True)
    if result == MALICIOUS:
        return ScanReading(Decision.MALICIOUS, scan_time=scan_time)
    if result is None or any(code in result for code in WAIT_CODES):
        return ScanReading(Decision.UNAVAILABLE if expired else Decision.WAITING)
    return ScanReading(Decision.FAILED, scan_time=scan_time, raw_result=result[:RAW_RESULT_MAX])


def stored_utc(value: datetime) -> datetime:
    """A `DATETIME2(3)` value read back from SQL (naive UTC) as an aware instant."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


async def read_tags(
    container: ContainerPort, blob_paths: Sequence[str]
) -> list[dict[str, str] | None]:
    """Each blob's index tags, 8 reads at a time; a failed read is `None`, no result for this
    wake (a network error, a `403`)."""
    gate = asyncio.Semaphore(TAG_READS_AT_ONCE)

    async def one(path: str) -> dict[str, str] | None:
        async with gate:
            try:
                return dict(await container.get_blob_client(path).get_blob_tags())
            except Exception:  # noqa: BLE001 - any failed read is "no result for this wake"
                return None

    return list(await asyncio.gather(*(one(path) for path in blob_paths)))


# --- Versions (sweep duty 3) -------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AwaitingVersion:
    id: UUID
    document_id: UUID
    search_index_id: UUID
    version_number: int
    blob_path: str
    blob_last_modified: datetime | None
    committed_at: datetime
    uploaded_by: UUID
    uploaded_via_project_id: UUID

    @property
    def not_before(self) -> datetime:
        modified = self.blob_last_modified or self.committed_at
        return stored_utc(modified)


def awaiting_versions(connection: Connection) -> list[AwaitingVersion]:
    rows = connection.execute(
        select(
            document_versions.c.id,
            document_versions.c.document_id,
            document_versions.c.search_index_id,
            document_versions.c.version_number,
            document_versions.c.blob_path,
            document_versions.c.blob_last_modified,
            document_versions.c.committed_at,
            document_versions.c.uploaded_by,
            document_versions.c.uploaded_via_project_id,
        )
        .where(
            document_versions.c.state == literal("pending"),
            document_versions.c.scan_required == BIT_TRUE,
            document_versions.c.scan_status == literal("awaiting"),
        )
        .order_by(document_versions.c.committed_at)
        .limit(SCAN_BATCH)
    ).all()
    return [
        AwaitingVersion(
            id=row.id,
            document_id=row.document_id,
            search_index_id=row.search_index_id,
            version_number=row.version_number,
            blob_path=row.blob_path,
            blob_last_modified=row.blob_last_modified,
            committed_at=row.committed_at,
            uploaded_by=row.uploaded_by,
            uploaded_via_project_id=row.uploaded_via_project_id,
        )
        for row in rows
    ]


def _awaiting(version_id: UUID) -> Any:
    return (
        (document_versions.c.id == version_id)
        & (document_versions.c.state == literal("pending"))
        & (document_versions.c.scan_status == literal("awaiting"))
    )


def record_clean(connection: Connection, version: AwaitingVersion, scan_time: datetime) -> bool:
    changed = connection.execute(
        update(document_versions)
        .where(_awaiting(version.id))
        .values(scan_status=literal("clean"), scan_time=scan_time)
    ).rowcount
    return bool(changed == 1)


def record_failure(
    connection: Connection,
    version: AwaitingVersion,
    reading: ScanReading,
    *,
    actor_id: UUID,
) -> list[UUID] | None:
    """A failure row of the mapping in one transaction: the version `failed` with its code, its
    queued items `failed` with the same code and, for a malicious file, the audit row. Returns
    the failed items' runs; `None` when the version had already left `awaiting`."""
    result = reading.result
    if result is None or result is CanaryResult.CLEAN:
        raise ValueError("only a failure row fails a version")
    code = FAILURE_CODES[result]
    changed = connection.execute(
        update(document_versions)
        .where(_awaiting(version.id))
        .values(
            state=literal("failed"),
            scan_status=result.value,
            scan_time=reading.scan_time,
            scan_raw_result=reading.raw_result,
            failure_code=code,
            failed_at=SYSUTCDATETIME,
        )
    ).rowcount
    if changed != 1:
        return None
    runs = connection.execute(
        update(ingestion_run_items)
        .where(
            ingestion_run_items.c.document_version_id == version.id,
            ingestion_run_items.c.state == literal("queued"),
        )
        .values(
            state=literal("failed"),
            outcome_code=code,
            outcome_message=catalogue_message(code),
            completed_at=SYSUTCDATETIME,
        )
        .returning(ingestion_run_items.c.run_id)
    ).all()
    if result is CanaryResult.MALICIOUS:
        write_audit_event(
            connection,
            AuditEvent(
                action="document.malware_detected",
                actor_type="system",
                actor_object_id=actor_id,
                subject_object_id=version.uploaded_by,
                target_type="document_version",
                target_id=str(version.id),
                project_id=version.uploaded_via_project_id,
                metadata=DocumentMalwareDetectedMetadata(
                    index_id=version.search_index_id,
                    document_id=version.document_id,
                    version_number=version.version_number,
                ),
            ),
        )
    return [row.run_id for row in runs]


async def delete_absent_ok(container: ContainerPort, blob_path: str) -> None:
    """`delete_blob()`, an absent blob counting as deleted."""
    with contextlib.suppress(ResourceNotFoundError):
        await container.delete_blob(blob_path)


async def resolve_versions(process: IngestionProcess, scan_wait_minutes: int) -> list[UUID]:
    """Sweep duty 3; returns the runs whose queued items it failed."""
    versions = await process.sql.run(
        awaiting_versions, name="sweep_scan_results", retry="transient"
    )
    if not versions:
        return []
    readings = await read_tags(process.documents, [version.blob_path for version in versions])
    now = process.clock.now()
    touched: list[UUID] = []
    for version, tags in zip(versions, readings, strict=True):
        if tags is None:
            continue
        deadline = stored_utc(version.committed_at) + timedelta(minutes=scan_wait_minutes)
        reading = map_scan_tags(tags, not_before=version.not_before, deadline=deadline, now=now)
        if reading.time_unusable:
            log_event(
                logger,
                "scan_time_unusable",
                logging.WARNING,
                **{"docchat.version_id": str(version.id)},
            )
        touched.extend(await _apply(process, version, reading))
    return touched


async def _apply(
    process: IngestionProcess, version: AwaitingVersion, reading: ScanReading
) -> list[UUID]:
    result = reading.result
    if result is None:
        return []
    if result is CanaryResult.CLEAN:
        scan_time = reading.scan_time
        if scan_time is None:
            raise ValueError("a clean reading carries its scan time")
        recorded = await process.sql.run(
            lambda connection: record_clean(connection, version, scan_time),
            name="sweep_scan_clean",
            retry="transient",
        )
        if recorded:
            SCAN_RESULTS.add(1, result=result.value)
        return []
    runs = await process.sql.run(
        lambda connection: record_failure(
            connection, version, reading, actor_id=process.workload_principal_id
        ),
        name="sweep_scan_failure",
        retry="none",
    )
    if runs is None:
        return []
    SCAN_RESULTS.add(1, result=result.value)
    if result is CanaryResult.MALICIOUS:
        emit_event(
            "malware_detected",
            indexId=str(version.search_index_id),
            documentId=str(version.document_id),
            versionId=str(version.id),
        )
        with contextlib.suppress(Exception):
            # A failed delete leaves the failed version's source to sweep duty 6.
            await delete_absent_ok(process.documents, version.blob_path)
    return runs


# --- Scanner canaries (maintenance step 3 and sweep duty 11) -----------------------------------


def canary_pdf(canary_id: UUID) -> bytes:
    """A one-page 612 x 792-point PDF whose document ID and `/Info /Subject` are the canary ID,
    so every canary's bytes are new."""
    buffer = io.BytesIO()
    with pikepdf.Pdf.new() as pdf:
        pdf.add_blank_page(page_size=CANARY_PAGE_SIZE)
        pdf.docinfo["/Subject"] = str(canary_id)
        identifier = pikepdf.String(canary_id.bytes)
        pdf.trailer.ID = pikepdf.Array([identifier, identifier])
        pdf.save(buffer)
    return buffer.getvalue()


def insert_canary(connection: Connection, canary_id: UUID) -> None:
    connection.execute(
        insert(scan_canaries).values(id=canary_id, blob_path=scanner_canary_path(canary_id))
    )


async def write_canary(
    process: IngestionProcess, *, new_id: Callable[[], UUID] = uuid4, statement: str
) -> UUID:
    """Write one canary: the row first, so a scan time can never precede `written_at` and an
    upload that fails leaves a row the sweep resolves as `unavailable`; then the PDF."""
    canary_id = new_id()
    await process.sql.run(
        lambda connection: insert_canary(connection, canary_id), name=statement, retry="none"
    )
    data = await asyncio.to_thread(canary_pdf, canary_id)
    await process.documents.upload_blob(
        scanner_canary_path(canary_id),
        data,
        overwrite=False,
        content_settings=ContentSettings(content_type="application/pdf"),
    )
    return canary_id


@dataclass(frozen=True, slots=True)
class OpenCanary:
    id: UUID
    blob_path: str
    written_at: datetime


def canary_state(connection: Connection) -> tuple[bool, list[OpenCanary]]:
    """Whether any canary row exists, and the 10 oldest open canaries."""
    any_row = connection.execute(select(scan_canaries.c.id).limit(1)).first() is not None
    rows = connection.execute(
        select(scan_canaries.c.id, scan_canaries.c.blob_path, scan_canaries.c.written_at)
        .where(scan_canaries.c.result.is_(None))
        .order_by(scan_canaries.c.written_at)
        .limit(OPEN_CANARIES)
    ).all()
    return any_row, [OpenCanary(row.id, row.blob_path, row.written_at) for row in rows]


def whole_minutes(start: datetime, end: datetime) -> int:
    return max(0, math.floor((end - start).total_seconds() / 60))


def record_canary(
    connection: Connection,
    canary_id: UUID,
    result: CanaryResult,
    scanned_at: datetime | None,
    minutes: int,
) -> bool:
    changed = connection.execute(
        update(scan_canaries)
        .where(scan_canaries.c.id == canary_id, scan_canaries.c.result.is_(None))
        .values(result=result.value, scanned_at=scanned_at, minutes_to_result=minutes)
    ).rowcount
    return bool(changed == 1)


async def resolve_canaries(process: IngestionProcess, scan_wait_minutes: int) -> None:
    """Sweep duty 11: the first canary when the table holds none, then up to 10 open canaries
    resolved with the mapping against `written_at`."""
    any_row, open_canaries = await process.sql.run(
        canary_state, name="sweep_canary_state", retry="transient"
    )
    if not any_row:
        await write_canary(process, statement="sweep_canary_write")
        return
    if not open_canaries:
        return
    readings = await read_tags(process.documents, [canary.blob_path for canary in open_canaries])
    now = process.clock.now()
    for canary, tags in zip(open_canaries, readings, strict=True):
        if tags is None:
            continue
        written = stored_utc(canary.written_at)
        reading = map_scan_tags(
            tags,
            not_before=written,
            deadline=written + timedelta(minutes=scan_wait_minutes),
            now=now,
        )
        result = reading.result
        if result is None:
            continue
        scanned_at = None if result is CanaryResult.UNAVAILABLE else reading.scan_time
        minutes = whole_minutes(written, now if scanned_at is None else scanned_at)
        recorded = await process.sql.run(
            partial(
                record_canary,
                canary_id=canary.id,
                result=result,
                scanned_at=scanned_at,
                minutes=minutes,
            ),
            name="sweep_canary_result",
            retry="transient",
        )
        if not recorded:
            continue
        emit_event("scanner_canary_result", result=result.value, minutes=float(minutes))
        await delete_absent_ok(process.documents, canary.blob_path)
