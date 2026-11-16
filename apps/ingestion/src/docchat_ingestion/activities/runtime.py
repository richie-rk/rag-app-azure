"""What one ingestion worker process holds for its activities and timers (`14-ingestion.md`
sections 4.4 rule 4 and 18.6; ADR 0113).

Every client, the SQL engine and the ops-config store are created once, at the process's first
invocation, from settings the start-up checks validated when the worker imported the function app:
one `ClientRegistry` (one `ManagedIdentityCredential`), one `SqlEngine` on that credential, whose
pool is warmed before that invocation's first unit of work (`01-repository.md` section 6.2 item
11), and one `OpsConfigStore` for the `ingestion` area with `report_invalid=False`, because
`load_dispatch_settings` reports an invalid area itself, once per wake. Invocations that arrive
together wait for the one creation. Per-document facts never live here; they travel in typed
inputs and results.

The activities see the process through the narrow ports below, so their tests pass fakes.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from typing import Any, Final, Protocol, cast
from uuid import UUID

from sqlalchemy import Connection

from docchat_core.clients import ClientProfile, ClientRegistry, ServiceEndpoints
from docchat_core.ingestion.methods import MethodRegistry
from docchat_core.ingestion.names import POISON_QUEUE, BlobContainer
from docchat_core.ingestion.settings import IngestionAppSettings
from docchat_core.models import Clock, SystemClock
from docchat_core.opsconfig import INGESTION_AREAS, Area, OpsConfig, OpsConfigStore
from docchat_core.settings import CoreSettings, LocalSettings, load_settings
from docchat_core.sql.engine import Retry, SqlEngine
from docchat_core.sql.settings import SqlSettings
from docchat_core.telemetry import monitor_loop_lag
from docchat_core.telemetry.settings import TelemetrySettings
from docchat_ingestion.isolation.pdf_schema import RENDITION_RULES_VERSION

# --- Ports -------------------------------------------------------------------------------------


class SqlRunner(Protocol):
    """The data-access layer, seen through its unit of work."""

    async def run[T](self, work: Callable[[Connection], T], *, name: str, retry: Retry) -> T: ...


class OpsSource(Protocol):
    """The ops-config store, seen through the snapshot and the keys that failed."""

    async def snapshot(self) -> OpsConfig: ...

    def invalid_keys(self, area: Area) -> tuple[str, ...]: ...


class BlobPort(Protocol):
    """One blob, seen through its tag read."""

    def get_blob_tags(self, **kwargs: Any) -> Awaitable[dict[str, str]]: ...


class BlobItemPort(Protocol):
    @property
    def name(self) -> str: ...


class ContainerPort(Protocol):
    """A container of the data account, seen through the calls ingestion makes on it."""

    def get_blob_client(self, blob: str, **kwargs: Any) -> BlobPort: ...

    def upload_blob(self, name: str, data: Any, **kwargs: Any) -> Awaitable[Any]: ...

    def delete_blob(self, blob: str, **kwargs: Any) -> Awaitable[None]: ...

    def list_blobs(self, **kwargs: Any) -> AsyncIterator[BlobItemPort]: ...

    def delete_blobs(self, *blobs: str, **kwargs: Any) -> Awaitable[AsyncIterator[Any]]: ...


class QueuePeekPort(Protocol):
    """`ingestion-requests-poison`, seen through its peek."""

    def peek_messages(self, **kwargs: Any) -> Awaitable[Iterable[object]]: ...


class SearchIndexPort(Protocol):
    """The search service's index operations (`SearchIndexClient`)."""

    def create_index(self, index: Any, **kwargs: Any) -> Awaitable[Any]: ...

    def get_index(self, name: str, **kwargs: Any) -> Awaitable[Any]: ...

    def delete_index(self, index: Any, **kwargs: Any) -> Awaitable[None]: ...


# --- The process -------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class IngestionProcess:
    """One worker process's resources, created once at its first invocation."""

    sql: SqlRunner
    ops: OpsSource
    documents: ContainerPort
    renditions: ContainerPort
    work: ContainerPort
    poison_queue: QueuePeekPort
    search_indexes: SearchIndexPort
    scanning_enabled: bool
    workload_principal_id: UUID
    rendition_rules_version: int
    clock: Clock = field(default_factory=SystemClock)
    background: tuple[asyncio.Task[None], ...] = ()


async def build_process() -> IngestionProcess:
    """Create the registry, the SQL engine on its credential (warmed before anything uses it),
    the ops-config store and the background tasks: the SQL idle release, so the `dev` database
    can pause between wakes, and the loop-lag monitor."""
    core = load_settings(CoreSettings)
    local = load_settings(LocalSettings)
    telemetry = load_settings(TelemetrySettings)
    settings = load_settings(IngestionAppSettings)
    registry = ClientRegistry(
        core,
        local,
        profile=ClientProfile.INGESTION,
        endpoints=ServiceEndpoints(
            blob_endpoint=settings.storage_blob_endpoint,
            queue_endpoint=settings.storage_queue_endpoint,
            queue_names=(POISON_QUEUE,),
            search_endpoint=settings.search_endpoint,
            openai_endpoint=settings.openai_endpoint,
            docintel_endpoint=settings.docintel_endpoint,
        ),
    )
    try:
        engine = SqlEngine(
            "ingestion",
            sql=load_settings(SqlSettings),
            local=local,
            credential=registry.credential,
        )
    except BaseException:
        # An open registry would refuse every later attempt to create the process's clients.
        await registry.close()
        raise
    try:
        # Before the first unit of work (`01-repository.md` section 6.2 item 11).
        await engine.warm()
    except BaseException:
        # The warm-up reports its own SQL failures; a cancelled first invocation gets here, and
        # the next one builds the process afresh.
        await engine.dispose()
        await registry.close()
        raise
    background = (
        asyncio.create_task(engine.run_idle_release()),
        asyncio.create_task(monitor_loop_lag(telemetry.telemetry_loop_lag_interval_ms)),
    )
    return IngestionProcess(
        sql=engine,
        ops=OpsConfigStore(engine, INGESTION_AREAS, report_invalid=False),
        # The SDK clients' overloaded signatures are wider than the ports; the ports name only
        # the calls ingestion makes.
        documents=cast(ContainerPort, registry.blob_container(BlobContainer.DOCUMENTS)),
        renditions=cast(ContainerPort, registry.blob_container(BlobContainer.RENDITIONS)),
        work=cast(ContainerPort, registry.blob_container(BlobContainer.INGESTION_WORK)),
        poison_queue=cast(QueuePeekPort, registry.queue(POISON_QUEUE)),
        search_indexes=cast(SearchIndexPort, registry.search_index_client),
        scanning_enabled=settings.storage_malware_scanning_enabled,
        workload_principal_id=core.core_workload_principal_id,
        rendition_rules_version=RENDITION_RULES_VERSION,
        background=background,
    )


@dataclass(slots=True)
class _ProcessSlot:
    process: IngestionProcess | None = None
    methods: MethodRegistry | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


_SLOT: Final = _ProcessSlot()

type ProcessProvider = Callable[[], Awaitable[IngestionProcess]]
"""How an activity gets its process: `runtime` in the worker, a fake in a test."""


async def runtime() -> IngestionProcess:
    """The process's resources, created at the first invocation that asks."""
    async with _SLOT.lock:
        if _SLOT.process is None:
            _SLOT.process = await build_process()
        return _SLOT.process


def install_methods(registry: MethodRegistry) -> None:
    """Keep the method registry the start-up checks filled; the function app installs it before
    the worker serves."""
    _SLOT.methods = registry


def method_registry() -> MethodRegistry:
    """The implementations this process runs (`14-ingestion.md` section 9.1)."""
    if _SLOT.methods is None:
        raise RuntimeError("no method registry is installed in this process")
    return _SLOT.methods
