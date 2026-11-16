"""The blob ports of the methods (`14-ingestion.md` sections 6.3, 6.5 and 6.14): reading and
writing work files, and opening the PDF a build reads.

The methods see blob paths only; every path is checked against its container's shapes before a
call, because a path reaches an implementation from a plan. Storage failures after the Blob
client's own retries become retryable `storage_unavailable`; a missing source and changed bytes
are `source_missing` and `source_changed`.
"""

from __future__ import annotations

import asyncio
import hashlib
import tempfile
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from pathlib import Path
from typing import IO, Final, Protocol

from azure.core.exceptions import (
    HttpResponseError,
    ResourceNotFoundError,
    ServiceRequestError,
    ServiceResponseError,
)
from azure.storage.blob.aio import ContainerClient

from docchat_core.ingestion.methods import MethodContext, MethodFailedError
from docchat_core.ingestion.names import BlobContainer, validate_blob_path

SERVER_ERROR: Final = 500
THROTTLED: Final = 429


class WorkStore(Protocol):
    """Whole-blob reads and overwriting writes in the data account's containers."""

    async def read(self, container: BlobContainer, path: str) -> bytes | None:
        """The blob's bytes, or `None` when it doesn't exist."""
        ...

    async def write(self, container: BlobContainer, path: str, data: bytes) -> None:
        """`upload_blob(overwrite=True)`."""
        ...


_STORAGE_ERRORS: Final = (HttpResponseError, ServiceRequestError, ServiceResponseError)


def _unavailable(failure: Exception) -> bool:
    """A connection failure, throttling or a server error, after the client's retries; any other
    status is a programming or configuration error, which the failure boundary records."""
    if isinstance(failure, HttpResponseError):
        status = failure.status_code or 0
        return status == THROTTLED or status >= SERVER_ERROR
    return True


class BlobStore:
    """`WorkStore` over the data account's container clients (`ClientRegistry.blob_container`),
    which carry the Blob client's retries and timeouts."""

    def __init__(self, containers: Callable[[str], ContainerClient]) -> None:
        self._containers = containers

    def _client(self, container: BlobContainer, path: str) -> ContainerClient:
        validate_blob_path(container, path)
        return self._containers(BlobContainer(container).value)

    async def read(self, container: BlobContainer, path: str) -> bytes | None:
        client = self._client(container, path)
        try:
            downloader = await client.download_blob(path)
            return await downloader.readall()
        except ResourceNotFoundError:
            return None
        except _STORAGE_ERRORS as failure:
            if _unavailable(failure):
                raise MethodFailedError("storage_unavailable") from None
            raise

    async def write(self, container: BlobContainer, path: str, data: bytes) -> None:
        client = self._client(container, path)
        try:
            await client.upload_blob(path, data, overwrite=True)
        except _STORAGE_ERRORS as failure:
            if _unavailable(failure):
                raise MethodFailedError("storage_unavailable") from None
            raise

    async def download_to(
        self, container: BlobContainer, path: str, target: IO[bytes], expected_sha256: str
    ) -> None:
        """Stream the blob into `target` chunk by chunk, each chunk's write and hash update in
        one worker-thread call, and check its SHA-256: `source_missing` for no blob,
        `source_changed` for other bytes."""
        client = self._client(container, path)
        hasher = hashlib.sha256()

        def take(chunk: bytes) -> None:
            target.write(chunk)
            hasher.update(chunk)

        try:
            downloader = await client.download_blob(path)
            async for chunk in downloader.chunks():
                await asyncio.to_thread(take, chunk)
        except ResourceNotFoundError:
            raise MethodFailedError("source_missing") from None
        except _STORAGE_ERRORS as failure:
            if _unavailable(failure):
                raise MethodFailedError("storage_unavailable") from None
            raise
        if hasher.hexdigest() != expected_sha256:
            raise MethodFailedError("source_changed")


class PdfSource(Protocol):
    """Opens the PDF a build reads (`ctx.pdf_container`/`ctx.pdf_path`, bytes `ctx.pdf_sha256`)
    as a binary stream, for the layout call. The Extract activity may give the copy it already
    downloaded and inspected."""

    def open(self, ctx: MethodContext) -> AbstractAsyncContextManager[IO[bytes]]: ...


def _temporary_file(directory: Path | None) -> IO[bytes]:
    return tempfile.TemporaryFile(dir=directory)


class BlobPdfSource:
    """`PdfSource` that downloads the blob into an anonymous temporary file and checks its
    SHA-256 against the context's."""

    def __init__(self, store: BlobStore, directory: Path | None = None) -> None:
        self._store = store
        self._directory = directory

    @asynccontextmanager
    async def _open(self, ctx: MethodContext) -> AsyncIterator[IO[bytes]]:
        stream = await asyncio.to_thread(_temporary_file, self._directory)
        try:
            await self._store.download_to(ctx.pdf_container, ctx.pdf_path, stream, ctx.pdf_sha256)
            await asyncio.to_thread(stream.seek, 0)
            yield stream
        finally:
            await asyncio.to_thread(stream.close)

    def open(self, ctx: MethodContext) -> AbstractAsyncContextManager[IO[bytes]]:
        return self._open(ctx)
