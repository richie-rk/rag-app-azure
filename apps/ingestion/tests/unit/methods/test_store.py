"""Unit tests of the methods' blob ports (`14-ingestion.md` sections 6.3, 6.5 and 6.14)."""

from __future__ import annotations

import asyncio
import hashlib
import io
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any, cast

import pytest
from azure.core.exceptions import HttpResponseError, ResourceNotFoundError, ServiceRequestError
from azure.storage.blob.aio import ContainerClient

from docchat_core.ingestion.methods import MethodContext, MethodFailedError
from docchat_core.ingestion.names import BlobContainer, extract_document_path
from docchat_ingestion.methods.store import BlobPdfSource, BlobStore

type ContextFactory = Callable[..., MethodContext]
WORK = BlobContainer.INGESTION_WORK


def _status_error(status: int) -> HttpResponseError:
    error = HttpResponseError(message="refused")
    error.status_code = status
    return error


class _Downloader:
    def __init__(self, data: bytes) -> None:
        self._data = data

    async def readall(self) -> bytes:
        return self._data

    async def chunks(self) -> AsyncIterator[bytes]:
        for start in range(0, len(self._data), 4):
            yield self._data[start : start + 4]


class _Container:
    """The two calls of `ContainerClient` the store makes."""

    def __init__(self) -> None:
        self.blobs: dict[str, bytes] = {}
        self.failure: Exception | None = None
        self.uploads: list[tuple[str, bool]] = []

    async def download_blob(self, name: str) -> _Downloader:
        if self.failure is not None:
            raise self.failure
        if name not in self.blobs:
            raise ResourceNotFoundError("absent")
        return _Downloader(self.blobs[name])

    async def upload_blob(self, name: str, data: bytes, *, overwrite: bool) -> None:
        if self.failure is not None:
            raise self.failure
        self.blobs[name] = data
        self.uploads.append((name, overwrite))


@pytest.fixture
def containers() -> dict[str, _Container]:
    return {name.value: _Container() for name in BlobContainer}


@pytest.fixture
def blob_store(containers: dict[str, _Container]) -> BlobStore:
    return BlobStore(lambda name: cast("ContainerClient", containers[name]))


async def test_reads_and_overwriting_writes(
    blob_store: BlobStore, containers: dict[str, _Container], make_context: ContextFactory
) -> None:
    path = extract_document_path(make_context().work_prefix)

    assert await blob_store.read(WORK, path) is None
    await blob_store.write(WORK, path, b"{}")

    assert await blob_store.read(WORK, path) == b"{}"
    assert containers["ingestion-work"].uploads == [(path, True)]


async def test_a_path_of_another_shape_is_refused(blob_store: BlobStore) -> None:
    with pytest.raises(ValueError, match="blob path"):
        await blob_store.read(WORK, "../elsewhere.json")


@pytest.mark.parametrize(
    "failure", [_status_error(503), _status_error(429), ServiceRequestError("x")]
)
async def test_storage_failures_after_retries_are_retryable(
    blob_store: BlobStore,
    containers: dict[str, _Container],
    make_context: ContextFactory,
    failure: Exception,
) -> None:
    containers["ingestion-work"].failure = failure
    path = extract_document_path(make_context().work_prefix)

    with pytest.raises(MethodFailedError) as read_failed:
        await blob_store.read(WORK, path)
    with pytest.raises(MethodFailedError) as write_failed:
        await blob_store.write(WORK, path, b"x")

    assert read_failed.value.code == write_failed.value.code == "storage_unavailable"


async def test_other_statuses_reach_the_boundary(
    blob_store: BlobStore, containers: dict[str, _Container], make_context: ContextFactory
) -> None:
    containers["ingestion-work"].failure = _status_error(403)
    path = extract_document_path(make_context().work_prefix)

    with pytest.raises(HttpResponseError):
        await blob_store.read(WORK, path)
    with pytest.raises(HttpResponseError):
        await blob_store.write(WORK, path, b"x")


async def test_download_checks_the_sha256(
    blob_store: BlobStore, containers: dict[str, _Container], make_context: ContextFactory
) -> None:
    data = b"%PDF-1.7 the version's bytes"
    ctx = make_context(pdf_sha256=hashlib.sha256(data).hexdigest())
    target = io.BytesIO()

    with pytest.raises(MethodFailedError) as missing:
        await blob_store.download_to(ctx.pdf_container, ctx.pdf_path, target, ctx.pdf_sha256)
    assert missing.value.code == "source_missing"

    containers["documents"].blobs[ctx.pdf_path] = data
    await blob_store.download_to(ctx.pdf_container, ctx.pdf_path, target, ctx.pdf_sha256)
    assert target.getvalue() == data

    with pytest.raises(MethodFailedError) as changed:
        await blob_store.download_to(ctx.pdf_container, ctx.pdf_path, io.BytesIO(), "1" * 64)
    assert changed.value.code == "source_changed"

    containers["documents"].failure = _status_error(500)
    with pytest.raises(MethodFailedError) as unavailable:
        await blob_store.download_to(ctx.pdf_container, ctx.pdf_path, io.BytesIO(), "1" * 64)
    assert unavailable.value.code == "storage_unavailable"


async def test_the_pdf_source_opens_a_checked_copy(
    blob_store: BlobStore,
    containers: dict[str, _Container],
    make_context: ContextFactory,
    tmp_path: Path,
) -> None:
    data = b"%PDF-1.7 " + b"x" * 100
    ctx = make_context(pdf_sha256=hashlib.sha256(data).hexdigest())
    containers["documents"].blobs[ctx.pdf_path] = data
    source = BlobPdfSource(blob_store, tmp_path)

    async with source.open(ctx) as stream:
        assert stream.read() == data
        handle: Any = stream
    assert handle.closed
    # The copy is an anonymous file: nothing of it was ever named in the directory.
    assert not any(await asyncio.to_thread(lambda: list(tmp_path.iterdir())))

    other = make_context(pdf_sha256="2" * 64, pdf_path=ctx.pdf_path)
    with pytest.raises(MethodFailedError):
        async with source.open(other):
            pass
