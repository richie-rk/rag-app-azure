"""`document fetch-source` (`86-operations.md` section 2.8.10; `14-ingestion.md` section 19.1;
`80-data-model.md` section 2.21; `82-security.md` SEC-54; runbook R37).

The supported way to receive an uploaded file back, for an audit or legal request: an operator at
level `user_data` reads the version's row under `docchat_user_data_operator`, downloads its source
from the container `documents` under the Storage Blob Data Reader role the level grants, checks the
bytes against the version's SHA-256 and writes them to `--out` with mode 0600, creating the file and
refusing any existing path or symbolic link. Only then is the `document.source_fetched` row written,
in a transaction of its own; a failed insert removes the file, so no fetched file lacks its row.
Without the level the SQL login, the statement or the blob read is refused (exit 3) and no file and
no row is written. The report holds the version ID, the media type, the size and the SHA-256, never
a name or content.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Protocol
from uuid import UUID

from azure.core.exceptions import AzureError, ResourceNotFoundError
from azure.storage.blob import ExponentialRetry
from azure.storage.blob.aio import BlobServiceClient
from sqlalchemy import select

from docchat_core.audit.models import DocumentSourceFetchedMetadata
from docchat_core.ingestion.models import DocumentVersionState
from docchat_core.ingestion.names import BlobContainer, source_path
from docchat_core.sql.tables.ingestion import document_versions
from docchat_ops.commands.ingestion import (
    STORAGE_CONNECTION_TIMEOUT_SECONDS,
    STORAGE_READ_TIMEOUT_SECONDS,
    identifier,
    operator_session,
    storage_endpoint,
    storage_failure,
)
from docchat_ops.framework.audit import operator_event, write_event
from docchat_ops.framework.auth import STORAGE_SCOPE, acquire_token
from docchat_ops.framework.exitcodes import (
    Incomplete,
    NotFound,
    UnexpectedError,
    UsageError,
)

if TYPE_CHECKING:
    import argparse
    from collections.abc import AsyncIterator

    from azure.core.credentials_async import AsyncTokenCredential
    from sqlalchemy import Connection

    from docchat_core.audit import AuditEvent
    from docchat_ops.cli import Run
    from docchat_ops.framework.audit import OpsDatabase

READ_UNIT: Final = "document_source_read"
BLOB_ENDPOINT: Final = "DOCCHAT_STORAGE_BLOB_ENDPOINT"
FILE_MODE: Final = 0o600
OPEN_FLAGS: Final = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
"""A new file only: no existing file is replaced and no symbolic link is followed."""
SOURCE_MISSING: Final = "source_missing"
UNTRUSTED_WARNING: Final = (
    "warning: the file is untrusted, an upload whose content no one has checked for safety; "
    "open it only in an isolated environment"
)
NEW_PATH_STEP: Final = "choose a new --out path; nothing was fetched"


class SourceBlobs(Protocol):
    """The container `documents` as `fetch-source` reads it."""

    def download(self, path: str) -> AsyncIterator[bytes]:
        """The blob's bytes, in the order the service returns them."""
        ...

    async def close(self) -> None: ...


class StorageSourceBlobs:
    """`azure.storage.blob.aio.BlobServiceClient` under the operator's own credential."""

    def __init__(self, client: BlobServiceClient) -> None:
        self._client = client

    async def download(self, path: str) -> AsyncIterator[bytes]:
        blob = self._client.get_blob_client(BlobContainer.DOCUMENTS.value, path)
        downloader = await blob.download_blob()
        async for chunk in downloader.chunks():
            yield chunk

    async def close(self) -> None:
        await self._client.close()


def blob_store(endpoint: str, credential: AsyncTokenCredential) -> SourceBlobs:
    """The data account's blob endpoint with the platform's storage client options."""
    return StorageSourceBlobs(
        BlobServiceClient(
            endpoint,
            credential=credential,
            connection_timeout=STORAGE_CONNECTION_TIMEOUT_SECONDS,
            read_timeout=STORAGE_READ_TIMEOUT_SECONDS,
            retry_policy=ExponentialRetry(
                initial_backoff=1, increment_base=2, retry_total=3, random_jitter_range=1
            ),
        )
    )


def output_path(value: str) -> Path:
    """`--out`: the file to create, checked again when it is opened."""
    return Path(value)


def fetch_source_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--version", type=identifier, required=True, help="the version's ID")
    parser.add_argument(
        "--out", type=output_path, required=True, help="the new file to write the source to"
    )


def _refuse_path(out: Path) -> None:
    """Before any sign-in: an existing path (a dangling link included) or a missing directory."""
    if os.path.lexists(out):
        raise UsageError(f"--out {out} already exists", NEW_PATH_STEP)
    if not out.parent.is_dir():
        raise UsageError(f"the directory of --out {out} doesn't exist", NEW_PATH_STEP)


@dataclass(frozen=True)
class _Version:
    version_id: UUID
    index_id: UUID
    document_id: UUID
    version_number: int
    sha256: bytes
    size_bytes: int
    content_type: str
    project_id: UUID
    path: str


def _version(row: Any, version_id: UUID) -> _Version:
    if row is None:
        raise NotFound(f"no document version {version_id}", "check the version ID in the ticket")
    if row.state == DocumentVersionState.FAILED and row.failure_code == SOURCE_MISSING:
        raise UsageError(
            f"the version failed with {SOURCE_MISSING}: it has no stored file",
            "record that in the ticket; nothing was fetched",
        )
    index_id = UUID(str(row.search_index_id))
    document_id = UUID(str(row.document_id))
    version_number = int(row.version_number)
    # A path read back from data is re-validated: it must be the version's own source path.
    path = source_path(index_id, document_id, version_number)
    if row.blob_path != path:
        raise UnexpectedError(
            "the version's stored path isn't its source path; nothing was fetched",
            "report the correlation ID with the version ID",
        )
    return _Version(
        version_id=version_id,
        index_id=index_id,
        document_id=document_id,
        version_number=version_number,
        sha256=bytes(row.sha256),
        size_bytes=int(row.size_bytes),
        content_type=str(row.content_type),
        project_id=UUID(str(row.uploaded_via_project_id)),
        path=path,
    )


async def _read_version(database: OpsDatabase, version_id: UUID) -> _Version:
    versions = document_versions.c
    statement = select(
        versions.search_index_id,
        versions.document_id,
        versions.version_number,
        versions.sha256,
        versions.size_bytes,
        versions.content_type,
        versions.blob_path,
        versions.state,
        versions.failure_code,
        versions.uploaded_via_project_id,
    ).where(versions.id == version_id)

    def read(connection: Connection) -> Any:
        return connection.execute(statement).first()

    row = await database.run(read, name=READ_UNIT, retry="transient")
    return _version(row, version_id)


def _create(out: Path) -> int:
    try:
        descriptor = os.open(out, OPEN_FLAGS, FILE_MODE)
    except FileExistsError:
        raise UsageError(f"--out {out} already exists", NEW_PATH_STEP) from None
    except OSError:
        raise UsageError(f"--out {out} can't be created", NEW_PATH_STEP) from None
    # The mode is exact whatever the operator's umask holds: read and write for the owner only.
    os.fchmod(descriptor, FILE_MODE)
    return descriptor


def _write_all(descriptor: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(descriptor, view)
        view = view[written:]


def _close(descriptor: int) -> None:
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


async def _download(blobs: SourceBlobs, version: _Version, out: Path) -> str:
    """Stream the source into the new file while hashing it; any failure removes the file. A hash
    other than the version's is exit 5, because the stored bytes changed after the commit."""
    descriptor = await asyncio.to_thread(_create, out)
    digest = hashlib.sha256()
    try:
        try:
            async for chunk in blobs.download(version.path):
                digest.update(chunk)
                await asyncio.to_thread(_write_all, descriptor, chunk)
        finally:
            await asyncio.to_thread(_close, descriptor)
    except BaseException:
        await asyncio.to_thread(out.unlink, missing_ok=True)
        raise
    if digest.digest() != version.sha256:
        await asyncio.to_thread(out.unlink, missing_ok=True)
        raise Incomplete(
            "the stored bytes don't match the version's SHA-256; the partial file was removed",
            "record it in the ticket and escalate to the security responder; hand nothing over",
        )
    return digest.hexdigest()


def _event(session_actor: str, ticket: str, version: _Version) -> AuditEvent:
    return operator_event(
        action="document.source_fetched",
        actor_object_id=session_actor,
        target_type="document_version",
        target_id=str(version.version_id),
        project_id=str(version.project_id),
        metadata=DocumentSourceFetchedMetadata(
            index_id=version.index_id,
            document_id=version.document_id,
            version_number=version.version_number,
            size_bytes=version.size_bytes,
            ticket=ticket,
        ),
    )


async def fetch_source(run: Run, args: argparse.Namespace) -> None:
    version_id: UUID = args.version
    out: Path = args.out
    _refuse_path(out)
    environment = run.azd()
    endpoint = storage_endpoint(environment, BLOB_ENDPOINT)
    async with operator_session(run, environment) as session:
        version = await _read_version(session.database, version_id)
        # Built before the download, so a row the catalogue refuses fails before any file exists.
        event = _event(session.actor, run.ticket or "", version)
        await acquire_token(session.credential, STORAGE_SCOPE, "Blob Storage")
        blobs = run.toolkit.blobs(endpoint, session.credential)
        try:
            run.progress(UNTRUSTED_WARNING)
            try:
                sha256 = await _download(blobs, version, out)
            except ResourceNotFoundError:
                raise NotFound(
                    "the version's source blob is missing; nothing was fetched",
                    "check the version ID; a purged or deleted version has no file",
                ) from None
            except AzureError as error:
                raise storage_failure(error, "Blob Storage", "user_data") from None
        finally:
            await blobs.close()
        try:
            await write_event(session.database, event)
        except Exception:  # noqa: BLE001 - any failed insert removes the file (section 2.8.10)
            await asyncio.to_thread(out.unlink, missing_ok=True)
            raise UnexpectedError(
                "the document.source_fetched row couldn't be written; the file was removed",
                "run the command again",
            ) from None
    run.report(
        "fetched",
        {
            "versionId": str(version_id),
            "mediaType": version.content_type,
            "sizeBytes": version.size_bytes,
            "sha256": sha256,
        },
    )
