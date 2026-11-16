"""`document fetch-source` (T-541; `86-operations.md` section 2.8.10 and acceptance criterion 27;
`14-ingestion.md` section 19.1 and acceptance criterion 59; `80-data-model.md` section 2.21;
`82-security.md` SEC-54).

The command runs through the real parser against the scripted SQL layer and the faked container
`documents` of `conftest.py`, writing real files under the test's temporary directory. Its proof
against Azurite and SQL Server with and without level `user_data` is the source fetch command test
(T-617, integration).
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from typing import TYPE_CHECKING, Any
from uuid import UUID

import pytest
from azure.core.exceptions import HttpResponseError, ServiceRequestError

from docchat_core.sql.engine import SqlUnavailable
from docchat_ops.commands import document
from docchat_ops.framework.auth import cli_credential
from docchat_ops.framework.exitcodes import ExitCode, UsageError

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from ops.tests.unit.conftest import OpsHarness, Outcome

TICKET = ("--ticket", "LEGAL-0042", "--reason", "audit request for the uploaded contract")
VERSION_ID = UUID("2b3c4d5e-6f7a-4b8c-9d0e-1f2a3b4c5d6e")
INDEX_ID = UUID("3f2c0d9e-8b7a-41c6-a5d4-e3f2c1b0a998")
DOCUMENT_ID = UUID("7a8b9c0d-1e2f-4a3b-8c4d-5e6f7a8b9c0d")
PROJECT_ID = UUID("8b9c0d1e-2f3a-4b4c-9d5e-6f7a8b9c0d1e")
SOURCE = b"%PDF-1.7 SENTINEL-CONTENT-7f3a uploaded bytes\n%%EOF\n"
PATH = f"{INDEX_ID}/{DOCUMENT_ID}/3/source"
BLOB_ENDPOINT = "https://stdocchatdevabc123.blob.core.windows.net/"


def _version(**overrides: Any) -> dict[str, Any]:
    row = {
        "search_index_id": INDEX_ID,
        "document_id": DOCUMENT_ID,
        "version_number": 3,
        "sha256": hashlib.sha256(SOURCE).digest(),
        "size_bytes": len(SOURCE),
        "content_type": "application/pdf",
        "blob_path": PATH,
        "state": "ingested",
        "failure_code": None,
        "uploaded_via_project_id": PROJECT_ID,
    }
    return row | overrides


def _fetch(ops: OpsHarness, out: Path, *, environment: str = "dev") -> Outcome:
    return ops.run(
        "--env",
        environment,
        "--json",
        "document",
        "fetch-source",
        "--version",
        str(VERSION_ID),
        "--out",
        str(out),
        *TICKET,
    )


def _http_error(status: int, code: str | None = None) -> HttpResponseError:
    error = HttpResponseError(message="refused")
    error.status_code = status
    error.error_code = code  # type: ignore[attr-defined]
    return error


@pytest.fixture
def out(tmp_path: Path) -> Path:
    directory = tmp_path / "fetched"
    directory.mkdir(mode=0o700)
    return directory / "source.bin"


def test_t541_fetch_source_writes_the_source_with_mode_0600_and_its_audit_row(
    ops: OpsHarness, out: Path
) -> None:
    ops.database.answer(_version())
    ops.blobs.blobs[PATH] = SOURCE
    printed: list[str] = []
    ops.blobs.on_download = lambda: printed.append(ops.stderr_stream.getvalue())
    outcome = _fetch(ops, out)
    assert outcome.code == ExitCode.SUCCESS, outcome.stderr
    assert out.read_bytes() == SOURCE
    mode = stat.S_IMODE(out.stat().st_mode)
    assert mode == 0o600
    assert mode & 0o111 == 0
    # The warning comes before the download, and the confirmation prompt never.
    assert document.UNTRUSTED_WARNING in printed[0]
    assert "Dry run" not in outcome.stderr
    assert ops.blobs.downloads == [PATH]
    assert ops.blobs.endpoints == [BLOB_ENDPOINT]
    assert ops.blobs.closed
    assert "https://storage.azure.com/.default" in ops.credential.scopes
    assert ops.database.units == [
        ("ops_connection_check", "transient"),
        ("document_source_read", "transient"),
        ("write_audit_event", "none"),
    ]
    (audit,) = ops.database.audit_rows
    assert audit["action"] == "document.source_fetched"
    assert audit["actor_type"] == "operator"
    assert str(audit["actor_object_id"]) == ops.operator_oid
    assert audit["subject_object_id"] is None
    assert audit["target_type"] == "document_version"
    assert audit["target_id"] == str(VERSION_ID)
    assert str(audit["project_id"]) == str(PROJECT_ID)
    assert audit["correlation_id"] == outcome.correlation_id
    assert json.loads(audit["metadata_json"]) == {
        "indexId": str(INDEX_ID),
        "documentId": str(DOCUMENT_ID),
        "versionNumber": 3,
        "sizeBytes": len(SOURCE),
        "ticket": "LEGAL-0042",
    }
    report = outcome.json()
    assert {key: report[key] for key in ("result", "versionId", "mediaType", "sizeBytes")} == {
        "result": "fetched",
        "versionId": str(VERSION_ID),
        "mediaType": "application/pdf",
        "sizeBytes": len(SOURCE),
    }
    assert report["sha256"] == hashlib.sha256(SOURCE).hexdigest()
    # Neither the content nor the reason reaches standard output.
    assert ops.sentinel not in outcome.stdout
    assert ops.sentinel not in outcome.stderr
    assert "audit request" not in outcome.stdout
    assert str(out) not in outcome.stdout
    sql = ops.database.sql("document_source_read")[0]
    assert "FROM app.document_versions" in sql


def test_fetch_source_mode_is_exact_whatever_the_umask(ops: OpsHarness, out: Path) -> None:
    ops.database.answer(_version())
    ops.blobs.blobs[PATH] = SOURCE
    previous = os.umask(0o277)
    try:
        outcome = _fetch(ops, out)
    finally:
        os.umask(previous)
    assert outcome.code == ExitCode.SUCCESS, outcome.stderr
    assert stat.S_IMODE(out.stat().st_mode) == 0o600


def test_fetch_source_refuses_an_existing_out_before_sign_in(ops: OpsHarness, out: Path) -> None:
    out.write_bytes(b"keep me")
    outcome = _fetch(ops, out)
    assert outcome.code == ExitCode.USAGE
    assert "already exists" in outcome.failure
    assert out.read_bytes() == b"keep me"
    assert ops.credentials_made == 0
    assert ops.az.calls == []


def test_fetch_source_refuses_a_symbolic_link_even_a_dangling_one(
    ops: OpsHarness, out: Path, tmp_path: Path
) -> None:
    out.symlink_to(tmp_path / "elsewhere")
    outcome = _fetch(ops, out)
    assert outcome.code == ExitCode.USAGE
    assert not (tmp_path / "elsewhere").exists()


def test_fetch_source_refuses_a_missing_directory(ops: OpsHarness, tmp_path: Path) -> None:
    outcome = _fetch(ops, tmp_path / "absent" / "source.bin")
    assert outcome.code == ExitCode.USAGE
    assert "doesn't exist" in outcome.failure


def test_a_file_created_after_the_check_is_never_replaced(
    ops: OpsHarness, out: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ops.database.answer(_version())
    ops.blobs.blobs[PATH] = SOURCE
    monkeypatch.setattr(os.path, "lexists", lambda path: False)
    out.write_bytes(b"created meanwhile")
    outcome = _fetch(ops, out)
    assert outcome.code == ExitCode.USAGE
    assert out.read_bytes() == b"created meanwhile"
    assert ops.blobs.downloads == []
    assert ops.database.audit_rows == []


def test_a_path_that_cant_be_created_exits_2(tmp_path: Path) -> None:
    with pytest.raises(UsageError, match="can't be created"):
        document._create(tmp_path / "absent" / "source.bin")


def test_fetch_source_of_an_unknown_version_exits_4(ops: OpsHarness, out: Path) -> None:
    ops.database.answer()
    outcome = _fetch(ops, out)
    assert outcome.code == ExitCode.NOT_FOUND
    assert not out.exists()
    assert ops.blobs.downloads == []


def test_fetch_source_of_a_version_failed_with_source_missing_exits_2(
    ops: OpsHarness, out: Path
) -> None:
    ops.database.answer(_version(state="failed", failure_code="source_missing"))
    outcome = _fetch(ops, out)
    assert outcome.code == ExitCode.USAGE
    assert "source_missing" in outcome.failure
    assert ops.blobs.downloads == []
    assert not out.exists()


def test_fetch_source_of_a_missing_blob_exits_4_and_leaves_no_file(
    ops: OpsHarness, out: Path
) -> None:
    ops.database.answer(_version(state="failed", failure_code="malware_detected"))
    outcome = _fetch(ops, out)
    assert outcome.code == ExitCode.NOT_FOUND
    assert "source blob is missing" in outcome.failure
    assert not out.exists()
    assert ops.database.audit_rows == []


def test_fetch_source_of_changed_bytes_exits_5_and_leaves_no_file(
    ops: OpsHarness, out: Path
) -> None:
    ops.database.answer(_version())
    ops.blobs.blobs[PATH] = SOURCE + b"tampered"
    outcome = _fetch(ops, out)
    assert outcome.code == ExitCode.INCOMPLETE
    assert "don't match the version's SHA-256" in outcome.failure
    assert not out.exists()
    assert ops.database.audit_rows == []


@pytest.mark.parametrize(
    ("before", "after", "code"),
    [
        (ServiceRequestError("unreachable"), None, ExitCode.UNREACHABLE),
        (None, ServiceRequestError("reset"), ExitCode.UNREACHABLE),
        (_http_error(403, "AuthorizationFailure"), None, ExitCode.UNREACHABLE),
        (_http_error(403, "AuthorizationPermissionMismatch"), None, ExitCode.ACCESS_DENIED),
    ],
)
def test_fetch_source_storage_failures_leave_no_file_and_no_row(
    ops: OpsHarness,
    out: Path,
    before: Exception | None,
    after: Exception | None,
    code: ExitCode,
) -> None:
    ops.database.answer(_version())
    ops.blobs.blobs[PATH] = SOURCE
    ops.blobs.error = before
    ops.blobs.error_after_first_chunk = after
    outcome = _fetch(ops, out)
    assert outcome.code == code
    assert not out.exists()
    assert ops.database.audit_rows == []
    assert ops.blobs.closed


def test_t541_fetch_source_without_level_user_data_exits_3(ops: OpsHarness, out: Path) -> None:
    ops.database.connect_error = SqlUnavailable("connection", 18456)
    outcome = _fetch(ops, out)
    assert outcome.code == ExitCode.ACCESS_DENIED
    assert not out.exists()
    assert ops.blobs.downloads == []
    assert ops.database.audit_rows == []


def test_fetch_source_without_a_storage_token_exits_3_before_the_download(
    ops: OpsHarness, out: Path
) -> None:
    ops.database.answer(_version())
    ops.credential.refused.add("https://storage.azure.com/.default")
    outcome = _fetch(ops, out)
    assert outcome.code == ExitCode.ACCESS_DENIED
    assert ops.blobs.endpoints == []
    assert not out.exists()


def test_a_failed_audit_insert_removes_the_file_and_exits_1(ops: OpsHarness, out: Path) -> None:
    ops.database.answer(_version())
    ops.blobs.blobs[PATH] = SOURCE
    ops.database.write_error = SqlUnavailable("connection", 10054)
    outcome = _fetch(ops, out)
    assert outcome.code == ExitCode.UNEXPECTED
    assert "the file was removed" in outcome.failure
    assert not out.exists()


def test_a_stored_path_other_than_the_source_path_exits_1(ops: OpsHarness, out: Path) -> None:
    ops.database.answer(_version(blob_path=f"{INDEX_ID}/{DOCUMENT_ID}/3/converted.pdf"))
    outcome = _fetch(ops, out)
    assert outcome.code == ExitCode.UNEXPECTED
    assert ops.blobs.downloads == []
    assert not out.exists()


@pytest.mark.parametrize("value", ["http://stdocchat.blob.core.windows.net/", "", "x y"])
def test_fetch_source_needs_a_secure_blob_endpoint(ops: OpsHarness, out: Path, value: str) -> None:
    ops.write_azd("dev", DOCCHAT_STORAGE_BLOB_ENDPOINT=value)
    outcome = _fetch(ops, out)
    assert outcome.code == ExitCode.USAGE
    assert "DOCCHAT_STORAGE_BLOB_ENDPOINT" in outcome.failure
    assert ops.credentials_made == 0


def test_fetch_source_in_prod_asks_nothing(ops: OpsHarness, out: Path) -> None:
    ops.database.answer(_version())
    ops.blobs.blobs[PATH] = SOURCE
    outcome = _fetch(ops, out, environment="prod")
    assert outcome.code == ExitCode.SUCCESS, outcome.stderr
    assert "Type the environment name" not in outcome.stderr


# --- the storage adapter --------------------------------------------------------------------------


class _Downloader:
    async def chunks(self) -> AsyncIterator[bytes]:
        for chunk in (b"ab", b"cd"):
            yield chunk


class _BlobClient:
    def __init__(self, calls: list[Any]) -> None:
        self._calls = calls

    async def download_blob(self) -> _Downloader:
        self._calls.append("download_blob")
        return _Downloader()


class _ServiceClient:
    def __init__(self) -> None:
        self.calls: list[Any] = []

    def get_blob_client(self, container: str, path: str) -> _BlobClient:
        self.calls.append((container, path))
        return _BlobClient(self.calls)

    async def close(self) -> None:
        self.calls.append("close")


async def test_the_blob_adapter_streams_the_documents_blob() -> None:
    client = _ServiceClient()
    blobs = document.StorageSourceBlobs(client)  # type: ignore[arg-type]
    assert [chunk async for chunk in blobs.download(PATH)] == [b"ab", b"cd"]
    await blobs.close()
    assert client.calls == [("documents", PATH), "download_blob", "close"]


@pytest.fixture
def blob_store() -> tuple[Any, Any]:
    credential = cli_credential()
    return document.blob_store(BLOB_ENDPOINT, credential), credential


async def test_the_blob_store_targets_the_data_account(blob_store: tuple[Any, Any]) -> None:
    blobs, credential = blob_store
    assert blobs._client.url.startswith(BLOB_ENDPOINT)
    await blobs.close()
    await credential.close()
