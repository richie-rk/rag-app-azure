"""Fixtures shared by the unit tests of `docchat_ingestion`: the process environment of a deployed
ingestion worker, an in-memory telemetry pipeline, and the fakes of the worker process's ports
(a scripted SQL connection whose every statement is compiled for SQL Server, the data account's
containers and poison queue, the search service's index operations and the ops-config store).

The OpenTelemetry providers are process globals that can be set once, so the telemetry fixture
resets them for one test and restores them, the loggers and the telemetry state afterwards;
nothing is exported anywhere. `docchat_testing`'s telemetry capture replaces it once that
fixture exists.

The heap the imports built is frozen before the first test: a full collection of it takes about
150 ms here, and the loop guard counts such a pause against whichever coroutine step it lands in,
so a test that only allocates would fail as if it blocked the loop.
"""

from __future__ import annotations

import logging
import os
from collections import deque
from collections.abc import AsyncIterator, Callable, Iterator
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import opentelemetry._logs._internal as logs_internal
import opentelemetry.metrics._internal as metrics_internal
import opentelemetry.trace as trace_api
import pytest
from azure.core.exceptions import ResourceExistsError, ResourceNotFoundError
from opentelemetry import propagate
from opentelemetry.util._once import Once
from sqlalchemy.dialects import mssql
from sqlalchemy.sql.dml import Insert
from sqlalchemy.sql.elements import ClauseElement

from docchat_core.ingestion.settings import IngestionOpsConfig
from docchat_core.opsconfig import OpsConfig
from docchat_core.settings import load_settings
from docchat_core.telemetry import TestExporters, configure_telemetry
from docchat_core.telemetry import configure as telemetry_configure
from docchat_core.telemetry.settings import TelemetrySettings
from docchat_core.telemetry.state import STATE
from docchat_ingestion.activities.runtime import IngestionProcess
from docchat_testing.clock import FakeClock

BUILD_COMMIT = "0123456789abcdef0123456789abcdef01234567"
IDENTITY_CLIENT_ID = "6f1d3c2a-7b8e-4c9d-a0b1-c2d3e4f5a6b7"
WORKLOAD_PRINCIPAL_ID = "7c9e6679-7425-40de-944b-e07fc1f90ae7"
SQL_SERVER = "sql-docchat-dev-abc123.database.windows.net"
BLOB_ENDPOINT = "https://stdocchatdatadevabc123.blob.core.windows.net/"
QUEUE_ENDPOINT = "https://stdocchatdatadevabc123.queue.core.windows.net/"

WORKER_ENVIRONMENT = {
    "DOCCHAT_CORE_ENVIRONMENT": "dev",
    "DOCCHAT_CORE_IDENTITY_CLIENT_ID": IDENTITY_CLIENT_ID,
    "DOCCHAT_CORE_WORKLOAD_PRINCIPAL_ID": WORKLOAD_PRINCIPAL_ID,
    "DOCCHAT_BUILD_COMMIT": BUILD_COMMIT,
    "DOCCHAT_TELEMETRY_LOG_LEVEL": "INFO",
    "DOCCHAT_SQL_SERVER": SQL_SERVER,
    "DOCCHAT_SQL_THREADS": "4",
    "DOCCHAT_SQL_COMMAND_TIMEOUT_SECONDS": "120",
    "DOCCHAT_STORAGE_BLOB_ENDPOINT": BLOB_ENDPOINT,
    "DOCCHAT_STORAGE_QUEUE_ENDPOINT": QUEUE_ENDPOINT,
    "DOCCHAT_STORAGE_MALWARE_SCANNING_ENABLED": "false",
    "DOCCHAT_SEARCH_ENDPOINT": "https://srch-docchat-dev-abc123.search.windows.net",
    "DOCCHAT_OPENAI_ENDPOINT": "https://oai-docchat-dev-abc123.openai.azure.com",
    "DOCCHAT_DOCINTEL_ENDPOINT": "https://di-docchat-dev-abc123.cognitiveservices.azure.com",
}
"""The settings the ingestion app reads, as Bicep writes them for `dev`."""

_CLEARED_PREFIXES = ("DOCCHAT_", "WEBSITE_")
_CLEARED_NAMES = ("APPLICATIONINSIGHTS_CONNECTION_STRING",)
_PROCESS_VARIABLES = (
    "OTEL_TRACES_SAMPLER",
    "OTEL_TRACES_SAMPLER_ARG",
    "OTEL_PROPAGATORS",
    "AZURE_SDK_TRACING_IMPLEMENTATION",
)
_LOGGERS = (
    "docchat",
    "uvicorn.access",
    "azure",
    "azure.core.pipeline.policies.http_logging_policy",
    "httpx",
    "httpcore",
    "httpx2",
    "openai",
    "opentelemetry",
    "uvicorn.error",
)


@pytest.fixture
def worker_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """The process environment of a deployed worker and nothing else: no other `DOCCHAT_*`
    setting, no App Service host variable and no connection string."""
    for name in list(os.environ):
        if name.startswith(_CLEARED_PREFIXES) or name in _CLEARED_NAMES:
            monkeypatch.delenv(name)
    for name, value in WORKER_ENVIRONMENT.items():
        monkeypatch.setenv(name, value)
    return monkeypatch


@pytest.fixture
def telemetry(worker_env: pytest.MonkeyPatch) -> Iterator[TestExporters]:
    """The in-memory pipeline of `configure_telemetry` for the ingestion service, with every
    global it touches restored afterwards. The library instrumentations stay off, because they
    patch libraries for the whole session."""
    monkeypatch = worker_env
    monkeypatch.setattr(trace_api, "_TRACER_PROVIDER_SET_ONCE", Once())
    monkeypatch.setattr(trace_api, "_TRACER_PROVIDER", None)
    monkeypatch.setattr(metrics_internal, "_METER_PROVIDER_SET_ONCE", Once())
    monkeypatch.setattr(metrics_internal, "_METER_PROVIDER", None)
    monkeypatch.setattr(logs_internal, "_LOGGER_PROVIDER_SET_ONCE", Once())
    monkeypatch.setattr(logs_internal, "_LOGGER_PROVIDER", None)
    monkeypatch.setattr(propagate, "_HTTP_TEXT_FORMAT", propagate.get_global_textmap())
    monkeypatch.setattr(telemetry_configure, "instrument_libraries", lambda service: None)
    monkeypatch.setattr(STATE, "strict", STATE.strict)
    monkeypatch.setattr(STATE, "service", STATE.service)
    saved_variables = {name: os.environ.get(name) for name in _PROCESS_VARIABLES}
    saved_loggers = {
        name: (
            logging.getLogger(name).level,
            logging.getLogger(name).propagate,
            logging.getLogger(name).disabled,
            list(logging.getLogger(name).handlers),
            list(logging.getLogger(name).filters),
        )
        for name in _LOGGERS
    }
    exporters = TestExporters()
    configure_telemetry("docchat-ingestion", load_settings(TelemetrySettings), exporters)
    yield exporters
    for name, (level, propagates, disabled, handlers, filters) in saved_loggers.items():
        logger = logging.getLogger(name)
        logger.setLevel(level)
        logger.propagate = propagates
        logger.disabled = disabled
        logger.handlers[:] = handlers
        logger.filters[:] = filters
    for name, value in saved_variables.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value


# --- Fakes of the worker process's ports -------------------------------------------------------

INGESTION_DEFAULTS: dict[str, int] = {
    "ingestion.max_parallel_items": 1,
    "ingestion.active_wake_seconds": 60,
    "ingestion.idle_wake_minutes": 60,
    "ingestion.scan_wait_minutes": 240,
    "ingestion.stale_upload_hours": 24,
    "ingestion.orphan_pending_minutes": 15,
    "ingestion.cleanup_retry_minutes": 15,
    "ingestion.max_item_claims": 3,
    "ingestion.retry_max_attempts": 7,
    "ingestion.retry_first_delay_seconds": 15,
    "ingestion.retry_max_delay_seconds": 480,
    "ingestion.activity_time_budget_seconds": 1200,
    "ingestion.max_pages_per_document": 2000,
    "ingestion.pagewise_pages_per_extract": 40,
    "ingestion.max_parse_seconds_per_document": 7200,
    "ingestion.embedding_batch_max_inputs": 64,
    "ingestion.embedding_batch_max_tokens": 60000,
    "ingestion.embedding_concurrency": 2,
    "ingestion.index_batch_max_documents": 100,
    "ingestion.index_batch_max_bytes": 8000000,
    "ingestion.table_summary_concurrency": 4,
    "ingestion.table_summary_max_input_tokens": 6000,
    "ingestion.table_summary_max_output_tokens": 300,
    "ingestion.run_retention_days": 180,
    "ingestion.durable_history_retention_days": 30,
}
"""The seeded values of the `ingestion` area (migration `0001`)."""

DIALECT = mssql.dialect()  # type: ignore[no-untyped-call]


def ingestion_settings(**overrides: int) -> IngestionOpsConfig:
    """The area with its seeded values, `overrides` by field name."""
    values = dict(INGESTION_DEFAULTS)
    for name, value in overrides.items():
        values[f"ingestion.{name}"] = value
    return IngestionOpsConfig.model_validate(values)


def row(**values: Any) -> SimpleNamespace:
    """A result row with attribute access."""
    return SimpleNamespace(**values)


@dataclass
class Rows:
    """What one scripted statement answers: its rows and its row count."""

    rows: list[Any] = field(default_factory=list)
    rowcount: int = 0

    def first(self) -> Any:
        return self.rows[0] if self.rows else None

    def one(self) -> Any:
        assert len(self.rows) == 1
        return self.rows[0]

    def all(self) -> list[Any]:
        return list(self.rows)

    def scalar_one(self) -> Any:
        return self.one()

    def scalar_one_or_none(self) -> Any:
        return self.first()

    def scalars(self) -> list[Any]:
        return list(self.rows)


@dataclass
class Executed:
    sql: str
    statement: ClauseElement
    parameters: Any

    @property
    def params(self) -> dict[str, Any]:
        return dict(self.statement.compile(dialect=DIALECT).params)


class ScriptedConnection:
    """Every statement is compiled for SQL Server and answered from the script in order; inserts
    are recorded and answered without consuming the script."""

    def __init__(self, *results: Rows) -> None:
        self.script: deque[Rows] = deque(results)
        self.executed: list[Executed] = []

    def execute(self, statement: ClauseElement, parameters: Any = None) -> Rows:
        compiled = statement.compile(dialect=DIALECT)
        self.executed.append(Executed(str(compiled), statement, parameters))
        if isinstance(statement, Insert):
            return Rows(rowcount=len(parameters) if isinstance(parameters, list) else 1)
        if not self.script:
            raise AssertionError(f"unscripted statement: {compiled}")
        return self.script.popleft()

    def inserts(self) -> list[Executed]:
        return [item for item in self.executed if isinstance(item.statement, Insert)]


class FakeSql:
    """The data-access layer: each unit of work runs on one scripted connection; `failures` makes
    a named unit raise."""

    def __init__(self, *results: Rows, failures: dict[str, BaseException] | None = None) -> None:
        self.connection = ScriptedConnection(*results)
        self.units: list[tuple[str, str]] = []
        self.failures = dict(failures or {})

    async def run(self, work: Callable[[Any], Any], *, name: str, retry: str) -> Any:
        self.units.append((name, retry))
        failure = self.failures.get(name)
        if failure is not None:
            raise failure
        return work(self.connection)

    def names(self) -> list[str]:
        return [name for name, _ in self.units]


class FakeBlob:
    def __init__(self, container: FakeContainer, name: str) -> None:
        self._container = container
        self._name = name

    async def get_blob_tags(self, **kwargs: Any) -> dict[str, str]:
        failure = self._container.tag_failures.get(self._name)
        if failure is not None:
            raise failure
        return dict(self._container.tags.get(self._name, {}))


@dataclass
class FakeResponse:
    status_code: int


class FakeContainer:
    """A container of the data account in memory."""

    def __init__(self, *names: str) -> None:
        self.blobs: dict[str, bytes] = dict.fromkeys(names, b"")
        self.tags: dict[str, dict[str, str]] = {}
        self.tag_failures: dict[str, BaseException] = {}
        self.uploads: list[tuple[str, dict[str, Any]]] = []
        self.deleted: list[str] = []
        self.delete_failure: BaseException | None = None
        self.delete_status: dict[str, int] = {}

    def get_blob_client(self, blob: str, **kwargs: Any) -> FakeBlob:
        return FakeBlob(self, blob)

    async def upload_blob(self, name: str, data: Any, **kwargs: Any) -> None:
        if not kwargs.get("overwrite", False) and name in self.blobs:
            raise ResourceExistsError("exists")
        self.blobs[name] = bytes(data)
        self.uploads.append((name, kwargs))

    async def delete_blob(self, blob: str, **kwargs: Any) -> None:
        if self.delete_failure is not None:
            raise self.delete_failure
        if blob not in self.blobs:
            raise ResourceNotFoundError("absent")
        del self.blobs[blob]
        self.deleted.append(blob)

    async def list_blobs(self, **kwargs: Any) -> AsyncIterator[SimpleNamespace]:
        prefix = kwargs.get("name_starts_with", "")
        for name in sorted(self.blobs):
            if name.startswith(prefix):
                yield SimpleNamespace(name=name)

    async def delete_blobs(self, *blobs: str, **kwargs: Any) -> AsyncIterator[FakeResponse]:
        responses: list[FakeResponse] = []
        for name in blobs:
            status = self.delete_status.get(name, 202 if name in self.blobs else 404)
            if status in (200, 202):
                del self.blobs[name]
                self.deleted.append(name)
            responses.append(FakeResponse(status))

        async def iterate() -> AsyncIterator[FakeResponse]:
            for response in responses:
                yield response

        return iterate()


class FakeQueue:
    def __init__(self, messages: int = 0) -> None:
        self.messages = messages
        self.peeks: list[dict[str, Any]] = []
        self.failure: BaseException | None = None

    async def peek_messages(self, **kwargs: Any) -> list[object]:
        self.peeks.append(kwargs)
        if self.failure is not None:
            raise self.failure
        return [object() for _ in range(min(self.messages, kwargs.get("max_messages", 1)))]


class FakeSearchIndexes:
    def __init__(self) -> None:
        self.created: list[Any] = []
        self.deleted: list[str] = []
        self.existing: dict[str, Any] = {}
        self.create_failure: BaseException | None = None
        self.delete_failure: BaseException | None = None

    async def create_index(self, index: Any, **kwargs: Any) -> Any:
        if self.create_failure is not None:
            raise self.create_failure
        if index.name in self.existing:
            raise ResourceExistsError("exists")
        self.existing[index.name] = index
        self.created.append(index)
        return index

    async def get_index(self, name: str, **kwargs: Any) -> Any:
        return self.existing[name]

    async def delete_index(self, index: Any, **kwargs: Any) -> None:
        if self.delete_failure is not None:
            raise self.delete_failure
        if index not in self.existing:
            raise ResourceNotFoundError("absent")
        del self.existing[index]
        self.deleted.append(index)


class FakeOps:
    """The ops-config store: the last valid snapshot and the keys that failed the latest read."""

    def __init__(
        self, settings: IngestionOpsConfig | None = None, invalid: tuple[str, ...] = ()
    ) -> None:
        self.settings = ingestion_settings() if settings is None else settings
        self.invalid = invalid
        self.failure: BaseException | None = None
        self.empty = False

    async def snapshot(self) -> OpsConfig:
        if self.failure is not None:
            raise self.failure
        if self.empty:
            return OpsConfig.model_validate({})
        return OpsConfig.model_validate({"ingestion": self.settings})

    def invalid_keys(self, area: str) -> tuple[str, ...]:
        return self.invalid


def make_process(**overrides: Any) -> IngestionProcess:
    """An `IngestionProcess` of fakes, any port replaced by keyword."""
    values: dict[str, Any] = {
        "sql": FakeSql(),
        "ops": FakeOps(),
        "documents": FakeContainer(),
        "renditions": FakeContainer(),
        "work": FakeContainer(),
        "poison_queue": FakeQueue(),
        "search_indexes": FakeSearchIndexes(),
        "scanning_enabled": False,
        "workload_principal_id": UUID(WORKLOAD_PRINCIPAL_ID),
        "rendition_rules_version": 2,
        "clock": FakeClock(),
    }
    values.update(overrides)
    return IngestionProcess(**values)


@pytest.fixture
def fakes() -> SimpleNamespace:
    """The fake classes and helpers, for tests to build their own ports."""
    return SimpleNamespace(
        Rows=Rows,
        row=row,
        FakeSql=FakeSql,
        FakeContainer=FakeContainer,
        FakeQueue=FakeQueue,
        FakeSearchIndexes=FakeSearchIndexes,
        FakeOps=FakeOps,
        settings=ingestion_settings,
        process=make_process,
        dialect=DIALECT,
    )
