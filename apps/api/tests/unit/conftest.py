"""Fixtures shared by the unit tests of `docchat_api`: an in-memory telemetry pipeline, a
data-access layer without SQL, and the in-process app over both.

The OpenTelemetry providers are process globals that can be set once, so `captured` resets them
for one test and restores them afterwards. `FakeSqlEngine` answers the statements the stage's
routes make by statement-set name: the ops-config read (the 73 seeded rows of
`80-data-model.md` section 2.24), provisioning and the project list; every call is recorded, so a
test can prove a request touched no store.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import AsyncIterator, Callable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Annotated, Any, cast, override
from uuid import NAMESPACE_URL, uuid5

import opentelemetry.metrics._internal as metrics_internal
import opentelemetry.trace as trace_api
import pytest
from fastapi import APIRouter, Depends, Query, Request
from fastapi import Path as PathParam
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.util._once import Once
from pydantic import Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from docchat_api.app import Ports
from docchat_api.http.access import access
from docchat_api.http.models import QueryModel, RequestModel
from docchat_api.http.preconditions import IfMatchHeader, if_match_header
from docchat_api.http.routing import CurrentPrincipal, DocChatRoute, route_options
from docchat_api.http.text import SingleLineText
from docchat_core.access import ProjectNotFound
from docchat_core.models import CanonicalUuid
from docchat_core.sql.engine import SqlEngine, SqlUnavailable
from docchat_core.sql.repositories.users import ProvisionResult
from docchat_core.telemetry import LOGGER_NAME
from docchat_testing.clock import FakeClock
from docchat_testing.fixtures.api import (
    ApiUnderTest,
    BuiltApp,
    build_app,
    ports_under_test,
    serving,
)
from docchat_testing.tokens import SigningKeys

CUSTOM_EVENT_NAME = "microsoft.custom_event.name"


def seeded_rows() -> dict[str, str]:
    """`app.ops_config` right after the first revision, `config_key` to `value_json`."""
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "docs" / "spec" / "80-data-model.md"
        if candidate.is_file():
            text = candidate.read_text(encoding="utf-8")
            return dict(re.findall(r"^\| `([a-z_.]+)` \| `(\{.*?\})` \| `\w+` \|$", text, re.M))
    raise FileNotFoundError("80-data-model.md")


SEEDED_ROWS = seeded_rows()


# --- Telemetry --------------------------------------------------------------------------------


class _Collector(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    @override
    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


_LOG_RECORD_FIELDS = frozenset(logging.makeLogRecord({}).__dict__) | {"message", "asctime"}


@dataclass
class Captured:
    """What one test emitted: finished spans, metric points and `docchat` log records."""

    spans: InMemorySpanExporter
    metrics: InMemoryMetricReader
    collector: _Collector = field(default_factory=_Collector)

    def finished_spans(self, name: str | None = None) -> list[ReadableSpan]:
        return [
            span for span in self.spans.get_finished_spans() if name is None or span.name == name
        ]

    def server_spans(self) -> list[ReadableSpan]:
        return [span for span in self.spans.get_finished_spans() if span.kind.name == "SERVER"]

    def points(self, metric_name: str) -> dict[tuple[tuple[str, object], ...], float]:
        data = self.metrics.get_metrics_data()
        found: dict[tuple[tuple[str, object], ...], float] = {}
        if data is None:
            return found
        for resource in data.resource_metrics:
            for scope in resource.scope_metrics:
                for metric in scope.metrics:
                    if metric.name != metric_name:
                        continue
                    for point in metric.data.data_points:
                        key = tuple(sorted((point.attributes or {}).items()))
                        found[key] = point.value  # type: ignore[union-attr]
        return found

    def records(self, message: str) -> list[logging.LogRecord]:
        return [
            record
            for record in self.collector.records
            if record.getMessage() == message and CUSTOM_EVENT_NAME not in record.__dict__
        ]

    def events(self, name: str) -> list[dict[str, Any]]:
        return [
            {
                key: value
                for key, value in record.__dict__.items()
                if key[:1].islower()
                and "." not in key
                and not key.startswith("otel")
                and key not in _LOG_RECORD_FIELDS
            }
            for record in self.collector.records
            if record.__dict__.get(CUSTOM_EVENT_NAME) == name
        ]


@pytest.fixture
def captured(monkeypatch: pytest.MonkeyPatch) -> Iterator[Captured]:
    """In-memory span and metric pipelines and the `docchat` logger's records for one test."""
    monkeypatch.setattr(trace_api, "_TRACER_PROVIDER_SET_ONCE", Once())
    monkeypatch.setattr(trace_api, "_TRACER_PROVIDER", None)
    monkeypatch.setattr(metrics_internal, "_METER_PROVIDER_SET_ONCE", Once())
    monkeypatch.setattr(metrics_internal, "_METER_PROVIDER", None)
    spans = InMemorySpanExporter()
    tracer_provider = TracerProvider()
    tracer_provider.add_span_processor(SimpleSpanProcessor(spans))
    trace_api.set_tracer_provider(tracer_provider)
    reader = InMemoryMetricReader()
    metrics_internal.set_meter_provider(MeterProvider(metric_readers=[reader]))
    capture = Captured(spans=spans, metrics=reader)
    logger = logging.getLogger(LOGGER_NAME)
    logger.addHandler(capture.collector)
    try:
        yield capture
    finally:
        logger.removeHandler(capture.collector)


# --- A data-access layer without SQL -----------------------------------------------------------


@dataclass
class SqlCall:
    method: str
    name: str
    parameters: Mapping[str, Any] | None


@dataclass
class FakeSqlEngine:
    """Answers the stage's statements by name; `failures` makes a name raise."""

    rows: dict[str, str] = field(default_factory=lambda: dict(SEEDED_ROWS))
    provision: ProvisionResult = field(
        default_factory=lambda: ProvisionResult(provisioned=False, default_project_count=0)
    )
    projects: list[SimpleNamespace] = field(default_factory=list)
    failures: dict[str, BaseException] = field(default_factory=dict)
    calls: list[SqlCall] = field(default_factory=list)

    def names(self) -> list[str]:
        return [call.name for call in self.calls]

    def _record(self, method: str, name: str, parameters: Mapping[str, Any] | None) -> None:
        self.calls.append(SqlCall(method, name, parameters))
        failure = self.failures.get(name)
        if failure is not None:
            raise failure

    async def fetch_all(
        self,
        statement: object,
        parameters: Mapping[str, Any] | None = None,
        *,
        name: str,
        retry: str = "transient",
    ) -> list[Any]:
        self._record("fetch_all", name, parameters)
        if name == "read_ops_config":
            return list(self.rows.items())
        if name == "list_my_projects":
            limit = int((parameters or {})["limit_plus_one"])
            after = (parameters or {}).get("after_name")
            ordered = sorted(self.projects, key=lambda row: row.name)
            return [row for row in ordered if after is None or row.name > after][:limit]
        raise AssertionError(f"unexpected statement {name}")

    async def fetch_one(
        self,
        statement: object,
        parameters: Mapping[str, Any] | None = None,
        *,
        name: str,
        retry: str = "transient",
    ) -> Any:
        self._record("fetch_one", name, parameters)
        raise AssertionError(f"unexpected statement {name}")

    async def run(self, work: Callable[[Any], Any], *, name: str, retry: str) -> Any:
        self._record("run", name, None)
        if name == "ensure_user":
            return self.provision
        raise AssertionError(f"unexpected unit of work {name}")

    async def dispose(self) -> None:
        return


def project_row(name: str, *, has_default_index: bool = True) -> SimpleNamespace:
    """One Q5 row as the driver returns it."""
    return SimpleNamespace(
        id=uuid5(NAMESPACE_URL, name),
        name=name,
        description=f"About {name}",
        example_questions_json='["What changed?"]',
        has_default_index=1 if has_default_index else 0,
    )


API_ENVIRONMENT: dict[str, str] = {
    "DOCCHAT_CORE_ENVIRONMENT": "dev",
    "DOCCHAT_CORE_IDENTITY_CLIENT_ID": "6f1d3c2a-7b8e-4c9d-a0b1-c2d3e4f5a6b7",
    "DOCCHAT_CORE_WORKLOAD_PRINCIPAL_ID": "7c9e6679-7425-40de-944b-e07fc1f90ae7",
    "DOCCHAT_AUTH_TENANT_ID": "5b0c2c8e-7a51-4c3e-9d6f-1f2a3b4c5d6e",
    "DOCCHAT_AUTH_API_CLIENT_ID": "8e3f1a52-4c6d-4b7e-a9f0-2c1d3e4f5a6b",
    "DOCCHAT_AUTH_WEB_CLIENT_ID": "1d2e3f4a-5b6c-4d7e-8f90-a1b2c3d4e5f6",
    "DOCCHAT_AUTH_TEST_CLIENT_ID": "9a8b7c6d-5e4f-4a3b-b2c1-d0e9f8a7b6c5",
    "DOCCHAT_BUILD_COMMIT": "0123456789abcdef0123456789abcdef01234567",
    "DOCCHAT_SQL_SERVER": "sql-docchat-dev-abc123.database.windows.net",
    "DOCCHAT_STORAGE_BLOB_ENDPOINT": "https://stdocchatdatadevabc123.blob.core.windows.net/",
    "DOCCHAT_STORAGE_QUEUE_ENDPOINT": "https://stdocchatdatadevabc123.queue.core.windows.net/",
    "DOCCHAT_STORAGE_MALWARE_SCANNING_ENABLED": "false",
    "DOCCHAT_SEARCH_MAX_INDEXES": "15",
    "DOCCHAT_SEARCH_ENDPOINT": "https://srch-docchat-dev.search.windows.net",
    "DOCCHAT_OPENAI_ENDPOINT": "https://oai-docchat-dev.openai.azure.com",
    "DOCCHAT_COSMOS_ENDPOINT": "https://cosmos-docchat-dev.documents.azure.com:443/",
    "DOCCHAT_CONTENTSAFETY_ENDPOINT": "https://cs-docchat-dev.cognitiveservices.azure.com",
    "DOCCHAT_API_WORKERS": "1",
}
"""A deployed API's settings in `dev`, every setting without a default present."""

_HOST_VARIABLES = (
    "WEBSITE_SITE_NAME",
    "WEBSITE_INSTANCE_ID",
    "APPLICATIONINSIGHTS_CONNECTION_STRING",
)


@pytest.fixture
def settings_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """A process environment with no `DOCCHAT_*` setting and no App Service host variable."""
    for name in list(os.environ):
        if name.startswith("DOCCHAT_") or name in _HOST_VARIABLES:
            monkeypatch.delenv(name)
    return monkeypatch


@pytest.fixture
def api_env(settings_env: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """A deployed API's environment (`API_ENVIRONMENT`)."""
    for name, value in API_ENVIRONMENT.items():
        settings_env.setenv(name, value)
    return settings_env


@pytest.fixture
def fake_sql() -> FakeSqlEngine:
    return FakeSqlEngine()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def ports(fake_sql: FakeSqlEngine, clock: FakeClock) -> Ports:
    """Permissive limiters over the fake data-access layer and the fake clock."""
    return ports_under_test(sql_engine=cast("SqlEngine", fake_sql), clock=clock)


# --- Probe routes: the platform under test, with routes of every shape --------------------------

SENTINEL = "cnry-instruction-5e1f0a77"
"""A value a test sends in every field, or raises in an exception, and expects nowhere."""

PROBE_ROW_VERSION = bytes(range(1, 9))


class ProbeBody(RequestModel):
    name: Annotated[str, SingleLineText(1, 20)]
    project_id: CanonicalUuid
    tags: Annotated[tuple[Annotated[str, Field(max_length=10)], ...], Field(max_length=2)] = ()
    count: Annotated[int, Field(ge=1, le=5)] = 1


class ProbeQuery(QueryModel):
    project_id: CanonicalUuid | None = None


def probe_router() -> APIRouter:
    """Routes the platform serves like any `/v1` route, one per shape a later route takes."""
    router = APIRouter(prefix="/v1", route_class=DocChatRoute)

    @router.post("/probe/items/{itemId}", operation_id="probeCreate")
    @access("user", scoped_params=["itemId"])
    async def probe_create(
        item_id: Annotated[CanonicalUuid, PathParam(alias="itemId")],
        body: ProbeBody,
        query: Annotated[ProbeQuery, Query()],
        principal: CurrentPrincipal,
    ) -> dict[str, str | None]:
        return {
            "itemId": str(item_id),
            "projectId": str(body.project_id),
            "name": body.name,
            "queryProjectId": None if query.project_id is None else str(query.project_id),
            "caller": str(principal.oid),
        }

    @router.get("/probe/boom", operation_id="probeBoom")
    @access("user")
    async def probe_boom() -> None:
        raise RuntimeError(SENTINEL)

    @router.get("/probe/teapot", operation_id="probeTeapot")
    @access("user")
    async def probe_teapot() -> None:
        raise StarletteHTTPException(418)

    @router.get("/probe/sql", operation_id="probeSql")
    @access("user")
    async def probe_sql() -> None:
        raise SqlUnavailable("exhausted", 40613, "")

    @router.post("/admin/probe", operation_id="probeAdmin")
    @access("admin", error_codes=["project_not_found"])
    async def probe_admin(body: ProbeBody) -> dict[str, str]:
        raise ProjectNotFound()

    @router.put("/probe/raw", operation_id="probeRaw")
    @route_options(body_limit=None, content_types=["application/octet-stream"], write_limited=False)
    @access("user")
    async def probe_raw(request: Request) -> dict[str, int]:
        size = 0
        async for chunk in request.stream():
            size += len(chunk)
        return {"size": size}

    @router.post("/probe/chat", operation_id="probeChat")
    @route_options(body_limit=32_768, burst_limited=False, write_limited=False)
    @access("user")
    async def probe_chat(body: ProbeBody) -> dict[str, str]:
        return {"name": body.name}

    @router.delete("/probe/versioned/{itemId}", operation_id="probeVersioned")
    @access("user", if_match=True, error_codes=["project_not_found"])
    async def probe_versioned(
        item_id: Annotated[CanonicalUuid, PathParam(alias="itemId")],
        if_match: Annotated[IfMatchHeader, Depends(if_match_header)],
    ) -> dict[str, str]:
        if item_id.int == 0:
            raise ProjectNotFound()
        if_match.require_row_version(PROBE_ROW_VERSION)
        return {"deleted": str(item_id)}

    return router


@pytest.fixture
def sentinel() -> str:
    return SENTINEL


@pytest.fixture
def probes() -> list[APIRouter]:
    """The probe routes, for a test that starts its own app."""
    return [probe_router()]


@pytest.fixture
def built(ports: Ports, signing_keys: SigningKeys) -> BuiltApp:
    """The in-process app over `ports`, with the probe routes, built outside the test's event
    loop: analysing every route's models takes tens of milliseconds, more with each area."""
    return build_app(ports=ports, signing_keys=signing_keys, routers=[probe_router()])


@pytest.fixture
async def started(built: BuiltApp, captured: Captured) -> AsyncIterator[ApiUnderTest]:
    """The in-process app, started; the telemetry pipeline exists before the app is
    instrumented, and the session key pair before the loop runs, because generating it holds
    the thread."""
    async with serving(built) as api:
        yield api
