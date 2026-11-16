"""The ingestion app's `df.DFApp` (`14-ingestion.md` sections 4.1, 4.2 and 4.11; `81-config.md`
section 6.1; `85-observability.md` section 1.2; ADRs 0046, 0049 and 0144).

The root shim `apps/ingestion/function_app.py` imports `app` from here once per Functions worker
process. Telemetry is configured first, so a failed check is exported; then the seven start-up
checks run in order, and only after the last one passes are the functions registered and
`ingestion_worker_started` emitted, which the `postdeploy` hook waits for (`83-infrastructure.md`
section 10). A failed check is logged once (`job_failed`, or a shared refusal under its own name)
and raises a first-party exception with a constant message, so the host loads no function and
ingestion fails closed.

Checks 5 and 7 run on a private event loop in a one-off thread, because the worker imports the app
inside its own running loop; check 5 opens and closes its own credential and SQL engine, the one
sanctioned exception to "one credential per process", so the process's shared clients are still
created once, at the first invocation. Their bounds (40 and 15 seconds) keep the start-up below the
host's 60-second limit for the language worker to start.
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import os
import stat
from collections.abc import Awaitable, Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import dataclass, field
from typing import Final

# The package ships no type information (ADR 0128).
import azure.durable_functions as df

from docchat_core.clients import ClientProfile, ClientRegistry, ServiceEndpoints
from docchat_core.ingestion.methods import (
    PAGEWISE_V1,
    RECURSIVE_V1,
    MethodDescriptor,
    MethodRegistry,
    read_active_methods,
)
from docchat_core.ingestion.settings import EmbeddingModelCatalog, IngestionAppSettings
from docchat_core.settings import (
    CoreSettings,
    LocalSettings,
    StartupRefusalError,
    check_endpoints,
    check_identity_client_id,
    check_local_emulators,
    check_local_environment,
    load_settings,
)
from docchat_core.sql.engine import SqlEngine
from docchat_core.sql.settings import SqlSettings, check_sql_server
from docchat_core.telemetry import (
    LOGGER_NAME,
    TestExporters,
    configure_telemetry,
    emit_event,
    log_event,
    qualified_type_name,
)
from docchat_core.telemetry.settings import TelemetrySettings, check_telemetry_log_level
from docchat_ingestion import selfcheck, starters
from docchat_ingestion.activities import (
    chunk_document,
    claim_ingestion_items,
    clean_up_item,
    create_search_index,
    delete_search_index,
    embed_chunks,
    enrich_document,
    extract_document,
    flip_chunk_set,
    index_chunks,
    load_dispatch_settings,
    purge_document_artifacts,
    purge_index_blobs,
    record_item_outcome,
    render_pages,
    rerender_pages,
    runtime,
    sweep_ingestion_state,
    validate_item,
)
from docchat_ingestion.isolation.runner import PARSE_USERS, USERS_PER_SLOT, Layout
from docchat_ingestion.orchestrators import (
    document_item_orchestrator,
    index_item_orchestrator,
    ingestion_dispatcher,
)

SERVICE: Final = "docchat-ingestion"
WORKER_STARTED: Final = "ingestion_worker_started"

METHOD_IMPLEMENTATIONS: Final[tuple[tuple[str, MethodDescriptor], ...]] = (
    ("docchat_ingestion.methods.pagewise", PAGEWISE_V1),
    ("docchat_ingestion.methods.recursive", RECURSIVE_V1),
)
"""Each method module and the core descriptor its `IMPLEMENTATION` registers against."""
IMPLEMENTATION_ATTRIBUTE: Final = "IMPLEMENTATION"

METHOD_CHECK_SECONDS: Final = 40.0
PROBE_SECONDS: Final = 15.0
MAX_PARSE_SLOTS: Final = PARSE_USERS // USERS_PER_SLOT
"""Each parse slot holds two parse users at once (a child and its verifier)."""
LOCK_FILE_MODE: Final = selfcheck.LOCK_MODE
WORKER_PROCESS_COUNT: Final = "FUNCTIONS_WORKER_PROCESS_COUNT"

BLUEPRINTS: Final = (
    starters.bp,
    ingestion_dispatcher.bp,
    document_item_orchestrator.bp,
    index_item_orchestrator.bp,
    load_dispatch_settings.bp,
    sweep_ingestion_state.bp,
    claim_ingestion_items.bp,
    create_search_index.bp,
    delete_search_index.bp,
    purge_index_blobs.bp,
    validate_item.bp,
    extract_document.bp,
    enrich_document.bp,
    chunk_document.bp,
    embed_chunks.bp,
    index_chunks.bp,
    render_pages.bp,
    rerender_pages.bp,
    flip_chunk_set.bp,
    clean_up_item.bp,
    purge_document_artifacts.bp,
    record_item_outcome.bp,
)
"""Every function of this app; no HTTP trigger among them (section 1.2 principle 1)."""

logger: Final = logging.getLogger(LOGGER_NAME)


class WorkerStartError(Exception):
    """A start-up check failed; the constant message keeps every value out of the host's record
    of the failed import."""

    def __init__(self) -> None:
        super().__init__("ingestion worker start-up check failed")


class StartupCheckFailedError(Exception):
    """A start-up check of section 4.11 other than the settings checks failed."""

    def __init__(self) -> None:
        super().__init__("start-up check failed")


class NotRootError(StartupCheckFailedError):
    """Check 1: the worker doesn't run as UID 0, which the parsing child's user switch needs."""


class EncodingsUnavailableError(StartupCheckFailedError):
    """Check 3: an encoding doesn't load from `TIKTOKEN_CACHE_DIR`."""


class MissingImplementationError(StartupCheckFailedError):
    """Check 5: an active catalogue row has no registered implementation, or the query didn't
    answer within 40 seconds."""


class ParsePoolExceededError(StartupCheckFailedError):
    """Check 6, `parse_pool_exceeded`: more parse slots than the parse-user pool covers, or a
    pool lock file missing or not root's with mode 0600."""


class ParseIsolationFailedError(StartupCheckFailedError):
    """Check 7, `parse_isolation_failed`: the isolation probe didn't report every check true
    within 15 seconds."""


# --- The checks --------------------------------------------------------------------------------


def run_on_private_loop[T](work: Callable[[], Awaitable[T]], timeout_seconds: float) -> T:
    """`asyncio.run(work())` in a one-off thread, waited for at most `timeout_seconds`; a wait
    that runs out raises `TimeoutError`."""
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="docchat-startup")
    try:
        future = executor.submit(lambda: asyncio.run(_awaited(work)))
        try:
            return future.result(timeout=timeout_seconds)
        except FutureTimeoutError:
            raise TimeoutError from None
    finally:
        executor.shutdown(wait=False, cancel_futures=True)


async def _awaited[T](work: Callable[[], Awaitable[T]]) -> T:
    return await work()


def check_root_user() -> None:
    """Check 1."""
    if os.geteuid() != 0:
        raise NotRootError


def check_settings() -> None:
    """Check 2: every settings class loads and validates, with the shared refusals of
    `81-config.md` section 6.1 (rows 1 to 4, 7, 9 and 10) and the SQL layer's server rule."""
    core = load_settings(CoreSettings)
    local = load_settings(LocalSettings)
    sql = load_settings(SqlSettings)
    telemetry = load_settings(TelemetrySettings)
    ingestion = load_settings(IngestionAppSettings)
    load_settings(EmbeddingModelCatalog)
    check_local_emulators(local)
    check_endpoints(local, sql, ingestion)
    check_local_environment(core)
    check_identity_client_id(core)
    check_sql_server(sql, local)
    check_telemetry_log_level(telemetry, local)


def check_encodings() -> None:
    """Check 3: `cl100k_base` and `o200k_base` load from `TIKTOKEN_CACHE_DIR`."""
    if selfcheck.check_encodings() is not True:
        raise EncodingsUnavailableError


def register_methods(registry: MethodRegistry) -> MethodRegistry:
    """Check 4: each implementation registers against its core descriptor; the registry refuses
    a duplicate (`key`, `version`)."""
    for module_name, descriptor in METHOD_IMPLEMENTATIONS:
        module = importlib.import_module(module_name)
        registry.register(descriptor, getattr(module, IMPLEMENTATION_ATTRIBUTE))
    return registry


async def read_active_rows() -> list[tuple[str, int]]:
    """Check 5's one query, on its own credential and engine, both closed before returning."""
    core = load_settings(CoreSettings)
    local = load_settings(LocalSettings)
    registry = ClientRegistry(
        core, local, profile=ClientProfile.INGESTION, endpoints=ServiceEndpoints()
    )
    try:
        engine = SqlEngine(
            "ingestion", sql=load_settings(SqlSettings), local=local, credential=registry.credential
        )
        try:
            return await engine.run(
                read_active_methods, name="startup_active_methods", retry="transient"
            )
        finally:
            await engine.dispose()
    finally:
        await registry.close()


def check_active_methods(
    registry: MethodRegistry, read_rows: Callable[[], Awaitable[Sequence[tuple[str, int]]]]
) -> None:
    """Check 5: every active catalogue row has a registered implementation."""
    try:
        rows = run_on_private_loop(read_rows, METHOD_CHECK_SECONDS)
    except TimeoutError:
        raise MissingImplementationError from None
    if registry.missing(list(rows)):
        raise MissingImplementationError


def check_parse_pool(layout: Layout) -> None:
    """Check 6: the worker processes' parse slots fit the pool, and every lock file is a regular
    file owned by root with mode 0600."""
    try:
        processes = int(os.environ.get(WORKER_PROCESS_COUNT) or "1")
    except ValueError:
        raise ParsePoolExceededError from None
    slots = load_settings(IngestionAppSettings).ingestion_parse_slots_per_process
    if processes < 1 or processes * slots > MAX_PARSE_SLOTS:
        raise ParsePoolExceededError
    for uid in layout.uids:
        try:
            info = layout.lock_path(uid).lstat()
        except OSError:
            raise ParsePoolExceededError from None
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0:
            raise ParsePoolExceededError
        if stat.S_IMODE(info.st_mode) != LOCK_FILE_MODE:
            raise ParsePoolExceededError


async def run_isolation_probe() -> bool:
    """The parsing runner's `probe` children (section 15.5): the default probe and the
    `convert`-mode probe (section 15.11 item 7.3)."""
    return await selfcheck.run_isolation_probe()


def check_isolation(probe: Callable[[], Awaitable[bool]]) -> None:
    """Check 7: the probe passes within 15 seconds."""
    try:
        passed = run_on_private_loop(probe, PROBE_SECONDS)
    except TimeoutError:
        raise ParseIsolationFailedError from None
    except Exception:  # noqa: BLE001 - any other outcome of the probe is the check's failure
        raise ParseIsolationFailedError from None
    if passed is not True:
        raise ParseIsolationFailedError


@dataclass(frozen=True, slots=True)
class StartupChecks:
    """The seven checks of section 4.11 with the ports the tests replace."""

    root_user: Callable[[], None] = check_root_user
    settings: Callable[[], None] = check_settings
    encodings: Callable[[], None] = check_encodings
    methods: Callable[[MethodRegistry], MethodRegistry] = register_methods
    active_rows: Callable[[], Awaitable[Sequence[tuple[str, int]]]] = read_active_rows
    layout: Layout = field(default_factory=Layout)
    probe: Callable[[], Awaitable[bool]] = run_isolation_probe
    registry: Callable[[], MethodRegistry] = field(default=MethodRegistry)

    def run(self) -> MethodRegistry:
        """Every check in order; the filled method registry."""
        self.root_user()
        self.settings()
        self.encodings()
        registry = self.methods(self.registry())
        check_active_methods(registry, self.active_rows)
        check_parse_pool(self.layout)
        check_isolation(self.probe)
        return registry


def record_startup_failure(failure: Exception) -> None:
    """Log a failed check once: a shared refusal under its own record name, any other failure as
    `job_failed` with its qualified type name."""
    if isinstance(failure, StartupRefusalError) and failure.code is not None:
        log_event(logger, failure.code, logging.ERROR)
        return
    log_event(
        logger, "job_failed", logging.ERROR, **{"error.type": qualified_type_name(type(failure))}
    )


def create_app(
    exporters: TestExporters | None = None, checks: StartupChecks | None = None
) -> df.DFApp:
    """Configure telemetry, run the start-up checks, register the functions and emit
    `ingestion_worker_started`. `exporters` is the test-only in-memory pipeline."""
    configure_telemetry(SERVICE, load_settings(TelemetrySettings), exporters)
    try:
        registry = (StartupChecks() if checks is None else checks).run()
    except Exception as failure:  # noqa: BLE001 - logged once; the import fails closed
        record_startup_failure(failure)
        raise WorkerStartError from None
    runtime.install_methods(registry)
    function_app = df.DFApp()
    for blueprint in BLUEPRINTS:
        function_app.register_functions(blueprint)
    emit_event(WORKER_STARTED)
    return function_app


app: Final = create_app()
