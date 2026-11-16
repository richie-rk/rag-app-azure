"""Fixtures shared by the method unit tests: a deterministic tokenizer, an in-memory blob store
and the method context of one item.

The real `tiktoken` encodings load from the image's cache, which this machine lacks, so the
chunkers are tested with a tokenizer that counts every word, every punctuation mark and every
whitespace run as one token and decodes exactly; the tests that need the real encodings skip
without them.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from typing import Any, Final
from uuid import uuid4

import pytest

from docchat_core.ingestion.methods import MethodContext
from docchat_core.ingestion.models import ItemSettings
from docchat_core.ingestion.names import BlobContainer, source_path, work_prefix
from docchat_core.ingestion.settings import TokenEncoding
from docchat_core.telemetry import TestExporters

_TOKEN: Final = re.compile(r"\w+|[^\w\s]|\s+")


class WordTokenizer:
    """One token per word, punctuation mark or whitespace run."""

    def __init__(self) -> None:
        self._ids: dict[str, int] = {}
        self._pieces: list[str] = []

    def encode(self, text: str) -> list[int]:
        tokens: list[int] = []
        for piece in _TOKEN.findall(text):
            if piece not in self._ids:
                self._ids[piece] = len(self._pieces)
                self._pieces.append(piece)
            tokens.append(self._ids[piece])
        return tokens

    def decode(self, tokens: Sequence[int]) -> str:
        return "".join(self._pieces[token] for token in tokens)


class MemoryStore:
    """A `WorkStore` over a dictionary, recording every write in order."""

    def __init__(self) -> None:
        self.blobs: dict[tuple[BlobContainer, str], bytes] = {}
        self.writes: list[tuple[BlobContainer, str]] = []

    async def read(self, container: BlobContainer, path: str) -> bytes | None:
        return self.blobs.get((container, path))

    async def write(self, container: BlobContainer, path: str, data: bytes) -> None:
        self.blobs[(container, path)] = data
        self.writes.append((container, path))


def item_settings(**overrides: Any) -> ItemSettings:
    values: dict[str, Any] = {
        "activity_time_budget_seconds": 1200,
        "max_pages_per_document": 2000,
        "pagewise_pages_per_extract": 40,
        "max_parse_seconds_per_document": 7200,
        "embedding_batch_max_inputs": 64,
        "embedding_batch_max_tokens": 60000,
        "embedding_concurrency": 2,
        "index_batch_max_documents": 100,
        "index_batch_max_bytes": 8_000_000,
        "table_summary_concurrency": 4,
        "table_summary_max_input_tokens": 6000,
        "table_summary_max_output_tokens": 300,
    }
    values.update(overrides)
    return ItemSettings(**values)


type ContextFactory = Callable[..., MethodContext]


@pytest.fixture
def tokenizer() -> WordTokenizer:
    return WordTokenizer()


@pytest.fixture
def store() -> MemoryStore:
    return MemoryStore()


@pytest.fixture
def make_context() -> ContextFactory:
    """`make_context(page_count=..., settings=...)`: an item's context with fresh IDs."""

    def make(**overrides: Any) -> MethodContext:
        settings = overrides.pop("settings", None) or item_settings()
        run, index, document = uuid4(), uuid4(), uuid4()
        fields: dict[str, Any] = {
            "run_id": run,
            "item_id": uuid4(),
            "index_id": index,
            "document_id": document,
            "version_id": uuid4(),
            "version_number": 2,
            "chunk_set_id": uuid4(),
            "work_prefix": work_prefix(run, document, 2),
            "pdf_container": BlobContainer.DOCUMENTS,
            "pdf_path": source_path(index, document, 2),
            "pdf_sha256": "0" * 64,
            "page_count": 3,
            "title": "Quarterly report",
            "encoding": TokenEncoding.CL100K_BASE,
            "settings": settings,
        }
        fields.update(overrides)
        return MethodContext(**fields)

    return make


type MetricPoints = Callable[[str], list[Any]]


@pytest.fixture
def metric_points(telemetry: TestExporters) -> MetricPoints:
    """`metric_points(name)`: every data point the in-memory pipeline holds for a metric."""

    def points(name: str) -> list[Any]:
        data = telemetry.metrics.get_metrics_data()
        if data is None:
            return []
        return [
            point
            for resource in data.resource_metrics
            for scope in resource.scope_metrics
            for metric in scope.metrics
            if metric.name == name
            for point in metric.data.data_points
        ]

    return points


@pytest.fixture
def make_settings() -> Callable[..., ItemSettings]:
    """`make_settings(name=value, ...)`: the item settings snapshot with some values changed."""
    return item_settings
