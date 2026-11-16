"""The worker process's resources (`14-ingestion.md` sections 4.4 rule 4 and 18.6): one registry,
one SQL engine on its credential, warmed first (`01-repository.md` section 6.2 item 11), an
ops-config store that leaves invalid areas to the caller, created once at the first invocation."""

from __future__ import annotations

import asyncio
from typing import Any, ClassVar

import pytest

from docchat_core.clients import ClientProfile
from docchat_core.ingestion.methods import MethodRegistry
from docchat_core.opsconfig import OpsConfigStore
from docchat_ingestion.activities import runtime
from docchat_ingestion.isolation.pdf_schema import RENDITION_RULES_VERSION


class FakeRegistry:
    instances: ClassVar[list[FakeRegistry]] = []

    def __init__(self, core: Any, local: Any, *, profile: Any, endpoints: Any) -> None:
        self.profile = profile
        self.endpoints = endpoints
        self.credential = object()
        self.closed = False
        self.search_index_client = object()
        FakeRegistry.instances.append(self)

    def blob_container(self, name: str) -> str:
        return f"container:{name}"

    def queue(self, name: str) -> str:
        return f"queue:{name}"

    async def close(self) -> None:
        self.closed = True


class FakeEngine:
    instances: ClassVar[list[FakeEngine]] = []

    def __init__(self, service: str, **kwargs: Any) -> None:
        self.service = service
        self.kwargs = kwargs
        self.events: list[str] = []
        FakeEngine.instances.append(self)

    async def warm(self) -> int:
        self.events.append("warm")
        return 4

    async def run_idle_release(self) -> None:
        self.events.append("idle-release")
        await asyncio.Event().wait()

    async def dispose(self) -> None:
        self.events.append("dispose")


async def _forever(interval: int) -> None:
    await asyncio.Event().wait()


@pytest.fixture
def patched(worker_env: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    FakeRegistry.instances.clear()
    FakeEngine.instances.clear()
    worker_env.setattr(runtime, "ClientRegistry", FakeRegistry)
    worker_env.setattr(runtime, "SqlEngine", FakeEngine)
    worker_env.setattr(runtime, "monitor_loop_lag", _forever)
    return worker_env


async def test_build_process_wires_every_port(patched: pytest.MonkeyPatch) -> None:
    process: Any = await runtime.build_process()
    try:
        (registry,) = FakeRegistry.instances
        assert registry.profile is ClientProfile.INGESTION
        assert registry.endpoints.queue_names == ("ingestion-requests-poison",)
        assert registry.endpoints.search_endpoint is not None
        assert isinstance(process.sql, FakeEngine)
        assert process.sql.service == "ingestion"
        assert process.sql.kwargs["credential"] is registry.credential
        assert isinstance(process.ops, OpsConfigStore)
        assert process.ops.areas == ("ingestion",)
        assert process.documents == "container:documents"
        assert process.renditions == "container:renditions"
        assert process.work == "container:ingestion-work"
        assert process.poison_queue == "queue:ingestion-requests-poison"
        assert process.search_indexes is registry.search_index_client
        assert process.scanning_enabled is False
        assert process.rendition_rules_version == RENDITION_RULES_VERSION
        assert len(process.background) == 2
        # Warmed before the process is handed out, so before any unit of work.
        assert process.sql.events == ["warm"]
    finally:
        for task in process.background:
            task.cancel()
        await asyncio.gather(*process.background, return_exceptions=True)


async def test_a_cancelled_warm_up_leaves_nothing_open(patched: pytest.MonkeyPatch) -> None:
    async def cancelled(self: FakeEngine) -> int:
        self.events.append("warm")
        raise asyncio.CancelledError

    patched.setattr(FakeEngine, "warm", cancelled)

    with pytest.raises(asyncio.CancelledError):
        await runtime.build_process()

    (engine,) = FakeEngine.instances
    assert engine.events == ["warm", "dispose"]
    assert FakeRegistry.instances[0].closed is True


async def test_a_failed_engine_closes_the_registry(patched: pytest.MonkeyPatch) -> None:
    def broken(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("no engine")

    patched.setattr(runtime, "SqlEngine", broken)

    with pytest.raises(RuntimeError, match="no engine"):
        await runtime.build_process()

    assert FakeRegistry.instances[0].closed is True


async def test_the_process_is_created_once(monkeypatch: pytest.MonkeyPatch) -> None:
    created: list[object] = []

    async def build() -> Any:
        created.append(object())
        return created[-1]

    slot = runtime._ProcessSlot()
    monkeypatch.setattr(runtime, "_SLOT", slot)
    monkeypatch.setattr(runtime, "build_process", build)

    first, second = await asyncio.gather(runtime.runtime(), runtime.runtime())

    assert first is second
    assert len(created) == 1


def test_the_method_registry_is_installed_once_started(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runtime, "_SLOT", runtime._ProcessSlot())
    with pytest.raises(RuntimeError, match="no method registry"):
        runtime.method_registry()

    registry = MethodRegistry()
    runtime.install_methods(registry)

    assert runtime.method_registry() is registry
