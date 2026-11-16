"""Unit tests of the worker process's side of the methods' ports (`14-ingestion.md` sections 9.1
and 18.6)."""

from __future__ import annotations

from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import pytest
from azure.core.exceptions import ResourceNotFoundError

from docchat_core.clients import ClientRegistry
from docchat_core.ingestion.methods import MethodContext
from docchat_core.ingestion.names import BlobContainer, extract_document_path
from docchat_core.ingestion.settings import IngestionAppSettings
from docchat_ingestion.methods import process
from docchat_ingestion.methods.store import BlobStore


class _Absent:
    async def download_blob(self, name: str) -> None:
        raise ResourceNotFoundError(name)


async def test_the_store_uses_the_open_registrys_containers(
    monkeypatch: pytest.MonkeyPatch, make_context: Callable[..., MethodContext]
) -> None:
    asked: list[str] = []

    def container(name: str) -> _Absent:
        asked.append(name)
        return _Absent()

    monkeypatch.setattr(ClientRegistry, "_open", SimpleNamespace(blob_container=container))
    path = extract_document_path(make_context().work_prefix)

    store = process.process_store()

    assert isinstance(store, BlobStore)
    assert await store.read(BlobContainer.INGESTION_WORK, path) is None
    assert asked == ["ingestion-work"]


def test_without_an_open_registry_there_is_no_store(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ClientRegistry, "_open", None)

    with pytest.raises(RuntimeError, match="no client registry"):
        process.process_store()


def test_the_app_settings_are_read_once(monkeypatch: pytest.MonkeyPatch) -> None:
    loaded: list[Any] = []
    settings = object()

    def load(cls: Any) -> object:
        loaded.append(cls)
        return settings

    monkeypatch.setattr(process, "load_settings", load)
    process.app_settings.cache_clear()
    try:
        assert process.app_settings() is settings
        assert process.app_settings() is settings
        assert loaded == [IngestionAppSettings]
    finally:
        process.app_settings.cache_clear()
