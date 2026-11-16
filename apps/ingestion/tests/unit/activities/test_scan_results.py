"""The scan-result mapping and the scanner canaries (`14-ingestion.md` sections 5.4 and 5.5;
T-157, the scan-result mapping unit tests; the canary flow against SQL Server and Azurite is
T-159)."""

from __future__ import annotations

import io
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pikepdf
import pytest
from azure.core.exceptions import HttpResponseError

from docchat_core.ingestion.models import CanaryResult
from docchat_core.telemetry import TestExporters
from docchat_ingestion.activities import scan_results
from docchat_ingestion.activities.scan_results import Decision, map_scan_tags

T0 = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
DEADLINE = T0 + timedelta(minutes=240)
VERSION = UUID("5b1c2d3e-4f50-4a61-8b72-93a4b5c6d7e8")
DOCUMENT = UUID("6c2d3e4f-5061-4b72-9c83-a4b5c6d7e8f9")
INDEX = UUID("3f2c0d9e-8b7a-41c6-a5d4-e3f2c1b0a998")
UPLOADER = UUID("7d3e4f50-6172-4c83-ad94-b5c6d7e8f9a0")
PROJECT = UUID("8e4f5061-7283-4d94-bea5-c6d7e8f9a0b1")
RUN = UUID("2d3e4f5a-6b7c-4d8e-9f0a-1b2c3d4e5f60")
BLOB = f"{INDEX}/{DOCUMENT}/1/source"


def _tags(result: str | None, time: str | None = "2026-01-01T12:05:00Z") -> dict[str, str]:
    tags: dict[str, str] = {}
    if result is not None:
        tags["Malware scanning scan result"] = result
    if time is not None:
        tags["Malware scanning scan time"] = time
    return tags


def _map(
    tags: dict[str, str], now: datetime = T0 + timedelta(minutes=10)
) -> scan_results.ScanReading:
    return map_scan_tags(tags, not_before=T0, deadline=DEADLINE, now=now)


def test_t157_no_threats_with_a_valid_scan_time_is_the_only_clean_result() -> None:
    reading = _map(_tags("No threats found"))

    assert reading.decision is Decision.CLEAN
    assert reading.scan_time == T0 + timedelta(minutes=5)
    assert reading.result is CanaryResult.CLEAN


def test_t157_a_scan_time_at_the_modification_instant_is_clean() -> None:
    assert _map(_tags("No threats found", T0.isoformat())).decision is Decision.CLEAN


@pytest.mark.parametrize("time", [None, "not a time", "2026-01-01T11:59:59Z"])
def test_t157_no_threats_with_a_missing_unparseable_or_earlier_time_keeps_waiting(
    time: str | None,
) -> None:
    reading = _map(_tags("No threats found", time))

    assert reading.decision is Decision.WAITING
    assert reading.time_unusable is True
    assert reading.result is None


def test_t157_an_unusable_time_past_the_deadline_is_unavailable() -> None:
    assert _map(_tags("No threats found", None), now=DEADLINE).decision is Decision.UNAVAILABLE


def test_t157_malicious_fails_the_version() -> None:
    reading = _map(_tags("Malicious"))

    assert reading.decision is Decision.MALICIOUS
    assert reading.scan_time == T0 + timedelta(minutes=5)


@pytest.mark.parametrize(
    "result", [None, "Error: SAM259213 throttled", "SAM259215 the scan is delayed"]
)
def test_t157_no_result_throttled_or_delayed_waits_until_240_minutes(result: str | None) -> None:
    assert _map(_tags(result)).decision is Decision.WAITING
    assert _map(_tags(result), now=DEADLINE - timedelta(seconds=1)).decision is Decision.WAITING
    assert _map(_tags(result), now=DEADLINE).decision is Decision.UNAVAILABLE


@pytest.mark.parametrize("result", ["Error", "Not scanned", "SAM259201 other", "x" * 300])
def test_t157_any_other_value_fails_with_its_first_256_characters(result: str) -> None:
    reading = _map(_tags(result))

    assert reading.decision is Decision.FAILED
    assert reading.raw_result == result[:256]


def test_tag_keys_are_matched_without_case() -> None:
    tags = {
        "malware scanning SCAN RESULT": "No threats found",
        "MALWARE scanning scan time": "2026-01-01T12:01:00",
    }

    assert _map(tags).decision is Decision.CLEAN


def test_a_naive_scan_time_is_utc_and_an_offset_is_converted() -> None:
    assert scan_results.parse_scan_time("2026-01-01T12:00:00") == T0
    assert scan_results.parse_scan_time("2026-01-01T13:00:00+01:00") == T0
    assert scan_results.parse_scan_time(None) is None


def test_whole_minutes_round_down_and_never_go_negative() -> None:
    assert scan_results.whole_minutes(T0, T0 + timedelta(seconds=179)) == 2
    assert scan_results.whole_minutes(T0, T0 - timedelta(minutes=5)) == 0


def _version(**overrides: Any) -> scan_results.AwaitingVersion:
    values: dict[str, Any] = {
        "id": VERSION,
        "document_id": DOCUMENT,
        "search_index_id": INDEX,
        "version_number": 1,
        "blob_path": BLOB,
        "blob_last_modified": T0.replace(tzinfo=None),
        "committed_at": T0.replace(tzinfo=None),
        "uploaded_by": UPLOADER,
        "uploaded_via_project_id": PROJECT,
    }
    values.update(overrides)
    return scan_results.AwaitingVersion(**values)


def _version_row(fakes: SimpleNamespace) -> Any:
    version = _version()
    return fakes.row(**{name: getattr(version, name) for name in version.__slots__})


async def test_a_clean_result_is_stored_and_counted(fakes: SimpleNamespace) -> None:
    documents = fakes.FakeContainer(BLOB)
    documents.tags[BLOB] = _tags("No threats found")
    sql = fakes.FakeSql(fakes.Rows([_version_row(fakes)]), fakes.Rows(rowcount=1))
    process = fakes.process(sql=sql, documents=documents)
    process.clock.advance_to(T0 + timedelta(minutes=10))

    runs = await scan_results.resolve_versions(process, 240)

    assert runs == []
    awaiting, clean = sql.connection.executed
    assert "TOP" in awaiting.sql
    assert "scan_status = N'awaiting'" in awaiting.sql
    assert "SET scan_status=N'clean'" in clean.sql
    assert "app.document_versions.state = N'pending'" in clean.sql
    assert documents.deleted == []


async def test_a_malicious_file_fails_its_version_and_items_and_is_deleted(
    fakes: SimpleNamespace, telemetry: TestExporters
) -> None:
    documents = fakes.FakeContainer(BLOB)
    documents.tags[BLOB] = _tags("Malicious")
    sql = fakes.FakeSql(
        fakes.Rows([_version_row(fakes)]),
        fakes.Rows(rowcount=1),
        fakes.Rows([fakes.row(run_id=RUN)]),
    )
    process = fakes.process(sql=sql, documents=documents)

    runs = await scan_results.resolve_versions(process, 240)

    assert runs == [RUN]
    _, version_update, items_update, audit = sql.connection.executed
    assert "SET state=N'failed'" in version_update.sql
    assert version_update.params["failure_code"] == "malware_detected"
    assert version_update.params["scan_status"] == "malicious"
    assert "OUTPUT inserted.run_id" in items_update.sql
    assert items_update.params["outcome_code"] == "malware_detected"
    assert "audit_events" in audit.sql
    assert audit.parameters["action"] == "document.malware_detected"
    assert audit.parameters["actor_type"] == "system"
    assert audit.parameters["subject_object_id"] == UPLOADER
    assert documents.deleted == [BLOB]
    events = [
        dict(item.log_record.attributes or {})
        for item in telemetry.logs.get_finished_logs()
        if (item.log_record.attributes or {}).get("microsoft.custom_event.name")
        == "malware_detected"
    ]
    assert events[0]["versionId"] == str(VERSION)


async def test_a_failed_source_delete_after_malicious_is_left_to_duty_6(
    fakes: SimpleNamespace,
) -> None:
    documents = fakes.FakeContainer(BLOB)
    documents.tags[BLOB] = _tags("Malicious")
    documents.delete_failure = HttpResponseError("refused")
    sql = fakes.FakeSql(fakes.Rows([_version_row(fakes)]), fakes.Rows(rowcount=1), fakes.Rows([]))

    runs = await scan_results.resolve_versions(fakes.process(sql=sql, documents=documents), 240)

    assert runs == []


async def test_a_version_that_left_awaiting_changes_nothing(fakes: SimpleNamespace) -> None:
    documents = fakes.FakeContainer(BLOB)
    documents.tags[BLOB] = _tags("Error")
    sql = fakes.FakeSql(fakes.Rows([_version_row(fakes)]), fakes.Rows(rowcount=0))

    assert (
        await scan_results.resolve_versions(fakes.process(sql=sql, documents=documents), 240) == []
    )
    assert len(sql.connection.executed) == 2


async def test_a_failed_tag_read_is_no_result_for_this_wake(fakes: SimpleNamespace) -> None:
    documents = fakes.FakeContainer(BLOB)
    documents.tag_failures[BLOB] = HttpResponseError("403")
    sql = fakes.FakeSql(fakes.Rows([_version_row(fakes)]))

    assert (
        await scan_results.resolve_versions(fakes.process(sql=sql, documents=documents), 240) == []
    )
    assert len(sql.connection.executed) == 1


async def test_an_unusable_scan_time_writes_its_warning(
    fakes: SimpleNamespace, telemetry: TestExporters
) -> None:
    documents = fakes.FakeContainer(BLOB)
    documents.tags[BLOB] = _tags("No threats found", None)
    sql = fakes.FakeSql(fakes.Rows([_version_row(fakes)]))

    await scan_results.resolve_versions(fakes.process(sql=sql, documents=documents), 240)

    (record,) = [
        item
        for item in telemetry.logs.get_finished_logs()
        if item.log_record.body == "scan_time_unusable"
    ]
    assert dict(record.log_record.attributes or {}) == {"docchat.version_id": str(VERSION)}


async def test_no_awaiting_version_reads_no_tag(fakes: SimpleNamespace) -> None:
    sql = fakes.FakeSql(fakes.Rows([]))
    assert await scan_results.resolve_versions(fakes.process(sql=sql), 240) == []


def test_record_failure_refuses_a_clean_or_waiting_reading(fakes: SimpleNamespace) -> None:
    with pytest.raises(ValueError, match="failure row"):
        scan_results.record_failure(
            fakes.FakeSql().connection,
            _version(),
            scan_results.ScanReading(Decision.CLEAN, scan_time=T0),
            actor_id=UPLOADER,
        )


def test_a_canary_pdf_is_one_page_with_the_canary_id() -> None:
    data = scan_results.canary_pdf(VERSION)

    with pikepdf.open(io.BytesIO(data)) as pdf:
        assert len(pdf.pages) == 1
        assert [float(value) for value in pdf.pages[0].mediabox] == [0, 0, 612, 792]
        assert str(pdf.docinfo["/Subject"]) == str(VERSION)
        assert bytes(pdf.trailer.ID[0]) == VERSION.bytes
    assert scan_results.canary_pdf(DOCUMENT) != data


async def test_write_canary_inserts_the_row_before_the_upload(fakes: SimpleNamespace) -> None:
    sql = fakes.FakeSql()
    documents = fakes.FakeContainer()

    canary = await scan_results.write_canary(
        fakes.process(sql=sql, documents=documents), new_id=lambda: VERSION, statement="x_write"
    )

    assert canary == VERSION
    (insert,) = sql.connection.inserts()
    assert insert.params["blob_path"] == f"_scanner-canary/{VERSION}.pdf"
    ((name, options),) = documents.uploads
    assert name == f"_scanner-canary/{VERSION}.pdf"
    assert options["overwrite"] is False
    assert options["content_settings"].content_type == "application/pdf"


async def test_the_sweep_writes_the_first_canary_when_none_exists(fakes: SimpleNamespace) -> None:
    sql = fakes.FakeSql(fakes.Rows([]), fakes.Rows([]))
    documents = fakes.FakeContainer()

    await scan_results.resolve_canaries(fakes.process(sql=sql, documents=documents), 240)

    assert len(sql.connection.inserts()) == 1
    assert len(documents.uploads) == 1


async def test_open_canaries_are_resolved_deleted_and_reported_once(
    fakes: SimpleNamespace, telemetry: TestExporters
) -> None:
    clean_path = f"_scanner-canary/{VERSION}.pdf"
    late_path = f"_scanner-canary/{DOCUMENT}.pdf"
    waiting_path = f"_scanner-canary/{INDEX}.pdf"
    documents = fakes.FakeContainer(clean_path, late_path, waiting_path)
    documents.tags[clean_path] = _tags("No threats found", "2026-01-01T12:07:30Z")
    sql = fakes.FakeSql(
        fakes.Rows([fakes.row(id=UPLOADER)]),
        fakes.Rows(
            [
                fakes.row(id=VERSION, blob_path=clean_path, written_at=T0.replace(tzinfo=None)),
                fakes.row(
                    id=DOCUMENT,
                    blob_path=late_path,
                    written_at=(T0 - timedelta(hours=5)).replace(tzinfo=None),
                ),
                fakes.row(id=INDEX, blob_path=waiting_path, written_at=T0.replace(tzinfo=None)),
            ]
        ),
        fakes.Rows(rowcount=1),
        fakes.Rows(rowcount=1),
    )
    process = fakes.process(sql=sql, documents=documents, scanning_enabled=True)
    process.clock.advance_to(T0 + timedelta(minutes=10))

    await scan_results.resolve_canaries(process, 240)

    clean_update, late_update = sql.connection.executed[2:]
    assert clean_update.params["result"] == "clean"
    assert clean_update.params["minutes_to_result"] == 7
    assert late_update.params["result"] == "unavailable"
    assert late_update.params["scanned_at"] is None
    assert late_update.params["minutes_to_result"] == 310
    assert sorted(documents.deleted) == sorted([clean_path, late_path])
    events = [
        dict(item.log_record.attributes or {})
        for item in telemetry.logs.get_finished_logs()
        if (item.log_record.attributes or {}).get("microsoft.custom_event.name")
        == "scanner_canary_result"
    ]
    assert [(event["result"], event["minutes"]) for event in events] == [
        ("clean", 7.0),
        ("unavailable", 310.0),
    ]


async def test_a_canary_resolved_by_another_writer_is_not_reported(fakes: SimpleNamespace) -> None:
    path = f"_scanner-canary/{VERSION}.pdf"
    documents = fakes.FakeContainer(path)
    documents.tags[path] = _tags("Malicious")
    sql = fakes.FakeSql(
        fakes.Rows([fakes.row(id=VERSION)]),
        fakes.Rows([fakes.row(id=VERSION, blob_path=path, written_at=T0.replace(tzinfo=None))]),
        fakes.Rows(rowcount=0),
    )

    await scan_results.resolve_canaries(fakes.process(sql=sql, documents=documents), 240)

    assert documents.deleted == []


async def test_no_open_canary_reads_no_tag(fakes: SimpleNamespace) -> None:
    sql = fakes.FakeSql(fakes.Rows([fakes.row(id=VERSION)]), fakes.Rows([]))

    await scan_results.resolve_canaries(fakes.process(sql=sql), 240)

    assert len(sql.connection.executed) == 2
