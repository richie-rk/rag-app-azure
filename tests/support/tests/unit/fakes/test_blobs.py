"""Unit tests of `BlobTransport`, the in-memory Blob container behind a real `ContainerClient`."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest
from azure.core.exceptions import HttpResponseError, ResourceNotFoundError
from azure.storage.blob.aio import ContainerClient

from docchat_testing.fakes.blobs import BlobRequest, BlobTransport

ACCOUNT = "https://stdocchatdatatest.blob.core.windows.net"
MIB = 1_048_576
BLOB = bytes(range(256)) * 10_000
PATH = "/renditions/a/b/1/document.pdf"


@pytest.fixture
def transport() -> BlobTransport:
    return BlobTransport(blobs={PATH: BLOB})


@pytest.fixture
async def container(transport: BlobTransport) -> AsyncIterator[ContainerClient]:
    client = ContainerClient(
        ACCOUNT,
        "renditions",
        transport=transport,
        retry_total=0,
        max_single_get_size=MIB,
        max_chunk_get_size=MIB,
    )
    async with client:
        yield client


async def test_a_download_reads_ranged_chunks_with_the_first_etag(
    transport: BlobTransport, container: ContainerClient
) -> None:
    download = await container.get_blob_client("a/b/1/document.pdf").download_blob()
    received = [chunk async for chunk in download.chunks()]

    assert download.size == len(BLOB)
    assert b"".join(received) == BLOB
    assert [len(chunk) for chunk in received] == [MIB, MIB, len(BLOB) - 2 * MIB]
    assert transport.paths() == [PATH] * 3
    assert [request.range for request in transport.requests] == [
        f"bytes=0-{MIB - 1}",
        f"bytes={MIB}-{2 * MIB - 1}",
        f"bytes={2 * MIB}-{len(BLOB) - 1}",
    ]
    assert [request.start for request in transport.requests] == [0, MIB, 2 * MIB]
    assert transport.requests[1].headers.get("If-Match") is not None


async def test_a_blob_not_held_is_a_404(container: ContainerClient) -> None:
    with pytest.raises(ResourceNotFoundError):
        await container.get_blob_client("a/b/1/pages/9.pdf").download_blob()


async def test_a_failing_offset_breaks_the_download_after_its_first_read(
    transport: BlobTransport, container: ContainerClient
) -> None:
    transport.failing[PATH] = MIB
    download = await container.get_blob_client("a/b/1/document.pdf").download_blob()
    chunks = aiter(download.chunks())

    assert len(await anext(chunks)) == MIB
    with pytest.raises(HttpResponseError):
        await anext(chunks)


async def test_a_stalled_read_never_answers(
    transport: BlobTransport, container: ContainerClient
) -> None:
    transport.stalled[PATH] = 0
    blob = container.get_blob_client("a/b/1/document.pdf")

    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.05):
            await blob.download_blob()


async def test_a_read_without_a_range_gets_the_whole_blob(transport: BlobTransport) -> None:
    client = ContainerClient(ACCOUNT, "renditions", transport=transport, retry_total=0)
    async with client:
        data = await (await client.get_blob_client("a/b/1/document.pdf").download_blob()).readall()

    assert data == BLOB


def test_a_request_without_a_range_starts_at_zero() -> None:
    assert BlobRequest("GET", PATH, None, {}).start == 0
