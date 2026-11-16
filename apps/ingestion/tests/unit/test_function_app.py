"""`docchat_ingestion.function_app` (`14-ingestion.md` sections 1.2, 4.2 and 4.11;
`85-observability.md` section 1.2): telemetry first, the seven start-up checks in order, the
functions registered (and no HTTP trigger, T-143), and exactly one `ingestion_worker_started` per
process (T-495).

The module builds its app when it is imported, as the Functions worker imports it through the root
shim, so each test imports it afresh inside a worker environment whose checks pass: UID 0, the
method implementations, the encodings, the active catalogue rows, the parse-user pool's lock files
and the isolation probe are stood in for at their boundaries."""

from __future__ import annotations

import asyncio
import importlib
import os
import stat
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from opentelemetry.sdk._logs import ReadableLogRecord

from docchat_core.ingestion.methods import PAGEWISE_V1, MethodRegistry
from docchat_core.settings import SettingsValidationError
from docchat_core.sql.engine import SqlEngine
from docchat_core.telemetry import TestExporters
from docchat_ingestion import selfcheck
from docchat_ingestion.isolation.runner import Layout

_MODULE = "docchat_ingestion.function_app"
_BUILD_COMMIT = "0123456789abcdef0123456789abcdef01234567"
_FUNCTIONS = {
    "ingestion_queue_starter",
    "ingestion_keepalive",
    "ingestion_maintenance",
    "ingestion_dispatcher",
    "document_item_orchestrator",
    "index_item_orchestrator",
    "load_dispatch_settings",
    "sweep_ingestion_state",
    "claim_ingestion_items",
    "create_search_index",
    "delete_search_index",
    "purge_index_blobs",
    "validate_item",
    "extract_document",
    "enrich_document",
    "chunk_document",
    "embed_chunks",
    "index_chunks",
    "render_pages",
    "rerender_pages",
    "flip_chunk_set",
    "clean_up_item",
    "purge_document_artifacts",
    "record_item_outcome",
}
_REAL_LSTAT = Path.lstat


class FakeMethod:
    """Satisfies the `IngestionMethod` protocol; the start-up checks never call it."""

    def extract_plan(self, source: Any) -> Any:
        raise NotImplementedError

    async def extract(self, ctx: Any, params: Any, part: Any) -> Any:
        raise NotImplementedError

    async def enrich(self, ctx: Any, params: Any) -> Any:
        raise NotImplementedError

    async def chunk(self, ctx: Any, params: Any) -> Any:
        raise NotImplementedError


def _lock_stat(mode: int = stat.S_IFREG | 0o600, uid: int = 0) -> os.stat_result:
    return os.stat_result((mode, 0, 0, 1, uid, 0, 0, 0, 0, 0))


@pytest.fixture
def passing(worker_env: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """Every start-up check passes at its boundary."""
    monkeypatch = worker_env
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    package = ModuleType("docchat_ingestion.methods")
    package.__path__ = []
    monkeypatch.setitem(sys.modules, "docchat_ingestion.methods", package)
    for name in ("pagewise", "recursive"):
        module = ModuleType(f"docchat_ingestion.methods.{name}")
        module.IMPLEMENTATION = FakeMethod()  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, f"docchat_ingestion.methods.{name}", module)
    monkeypatch.setattr(selfcheck, "check_encodings", lambda: True)

    async def probe(pool: Any = None) -> bool:
        return True

    monkeypatch.setattr(selfcheck, "run_isolation_probe", probe)

    async def active_rows(self: SqlEngine, work: Any, *, name: str, retry: str) -> Any:
        return [("pagewise", 1), ("recursive", 1)]

    async def dispose(self: SqlEngine) -> None:
        return None

    monkeypatch.setattr(SqlEngine, "run", active_rows)
    monkeypatch.setattr(SqlEngine, "dispose", dispose)

    def lstat(self: Path) -> os.stat_result:
        if str(self).startswith("/run/docchat-parse/"):
            return _lock_stat()
        return _REAL_LSTAT(self)

    monkeypatch.setattr(Path, "lstat", lstat)
    return monkeypatch


def _import(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    monkeypatch.delitem(sys.modules, _MODULE, raising=False)
    return importlib.import_module(_MODULE)


@pytest.fixture
def function_app(passing: pytest.MonkeyPatch, telemetry: TestExporters) -> ModuleType:
    """The module, imported once with passing checks; its start-up records cleared."""
    module = _import(passing)
    telemetry.logs.clear()
    return module


def _named(exporters: TestExporters, name: str) -> list[ReadableLogRecord]:
    return [
        item
        for item in exporters.logs.get_finished_logs()
        if item.log_record.body == name
        or (item.log_record.attributes or {}).get("microsoft.custom_event.name") == name
    ]


def _attributes(record: ReadableLogRecord) -> dict[str, Any]:
    return dict(record.log_record.attributes or {})


def test_t495_importing_the_module_emits_one_worker_started_event(
    passing: pytest.MonkeyPatch, telemetry: TestExporters
) -> None:
    module = _import(passing)

    (event,) = _named(telemetry, "ingestion_worker_started")
    assert _attributes(event) == {"microsoft.custom_event.name": "ingestion_worker_started"}
    assert event.resource.attributes["service.name"] == "docchat-ingestion"
    assert event.resource.attributes["service.version"] == _BUILD_COMMIT
    assert _named(telemetry, "job_failed") == []
    assert {function.get_function_name() for function in module.app.get_functions()} == _FUNCTIONS


def test_t143_no_function_has_an_http_trigger(function_app: ModuleType) -> None:
    triggers = {
        binding.type
        for function in function_app.app.get_functions()
        for binding in function.get_bindings()
        if binding.type.endswith("Trigger")
    }

    assert triggers == {"queueTrigger", "timerTrigger", "orchestrationTrigger", "activityTrigger"}


def test_the_root_shim_exposes_the_same_app(function_app: ModuleType) -> None:
    namespace: dict[str, Any] = {}

    exec("from docchat_ingestion.function_app import app", namespace)  # noqa: S102 - the shim's line

    assert namespace["app"] is function_app.app


def test_the_started_worker_holds_its_method_registry(function_app: ModuleType) -> None:
    from docchat_ingestion.activities import runtime  # noqa: PLC0415 - read after the import

    registry = runtime.method_registry()
    assert ("pagewise", 1) in registry
    assert ("recursive", 1) in registry


def _checks(function_app: ModuleType, **overrides: Any) -> Any:
    return function_app.StartupChecks(**overrides)


def _fails(
    function_app: ModuleType, telemetry: TestExporters, checks: Any, error_type: str
) -> None:
    with pytest.raises(function_app.WorkerStartError) as raised:
        function_app.create_app(checks=checks)

    assert str(raised.value) == "ingestion worker start-up check failed"
    assert raised.value.__cause__ is None
    assert raised.value.__suppress_context__ is True
    (record,) = _named(telemetry, "job_failed")
    assert _attributes(record) == {"error.type": f"docchat_ingestion.function_app.{error_type}"}
    assert _named(telemetry, "ingestion_worker_started") == []


def test_t495_check_1_a_worker_not_running_as_root_fails(
    function_app: ModuleType, passing: pytest.MonkeyPatch, telemetry: TestExporters
) -> None:
    passing.setattr(os, "geteuid", lambda: 1000)

    _fails(function_app, telemetry, _checks(function_app), "NotRootError")


def test_check_3_encodings_that_do_not_load_fail(
    function_app: ModuleType, passing: pytest.MonkeyPatch, telemetry: TestExporters
) -> None:
    passing.setattr(selfcheck, "check_encodings", lambda: False)

    _fails(function_app, telemetry, _checks(function_app), "EncodingsUnavailableError")


def test_check_4_a_duplicate_registration_fails(
    function_app: ModuleType, telemetry: TestExporters
) -> None:
    def twice(registry: MethodRegistry) -> MethodRegistry:
        registry.register(PAGEWISE_V1, FakeMethod())
        registry.register(PAGEWISE_V1, FakeMethod())
        return registry

    with pytest.raises(function_app.WorkerStartError):
        function_app.create_app(checks=_checks(function_app, methods=twice))

    (record,) = _named(telemetry, "job_failed")
    assert _attributes(record)["error.type"].endswith("DuplicateMethodError")


def test_t089_check_5_an_active_row_without_an_implementation_fails(
    function_app: ModuleType, telemetry: TestExporters
) -> None:
    async def rows() -> list[tuple[str, int]]:
        return [("pagewise", 1), ("pagewise", 2)]

    _fails(
        function_app,
        telemetry,
        _checks(function_app, active_rows=rows),
        "MissingImplementationError",
    )


def test_t089_check_5_a_query_past_its_bound_fails_like_a_missing_implementation(
    function_app: ModuleType, passing: pytest.MonkeyPatch, telemetry: TestExporters
) -> None:
    passing.setattr(function_app, "METHOD_CHECK_SECONDS", 0.05)

    async def slow() -> list[tuple[str, int]]:
        await asyncio.sleep(2)
        return []

    _fails(
        function_app,
        telemetry,
        _checks(function_app, active_rows=slow),
        "MissingImplementationError",
    )


@pytest.mark.parametrize(
    ("processes", "slots"), [("9", "1"), ("3", "3"), ("zero", "1"), ("0", "1")]
)
def test_check_6_more_parse_slots_than_the_pool_covers_fail(
    function_app: ModuleType,
    passing: pytest.MonkeyPatch,
    telemetry: TestExporters,
    processes: str,
    slots: str,
) -> None:
    passing.setenv("FUNCTIONS_WORKER_PROCESS_COUNT", processes)
    passing.setenv("DOCCHAT_INGESTION_PARSE_SLOTS_PER_PROCESS", slots)

    _fails(function_app, telemetry, _checks(function_app), "ParsePoolExceededError")


def test_check_6_eight_slots_pass(function_app: ModuleType, passing: pytest.MonkeyPatch) -> None:
    passing.setenv("FUNCTIONS_WORKER_PROCESS_COUNT", "2")
    passing.setenv("DOCCHAT_INGESTION_PARSE_SLOTS_PER_PROCESS", "4")

    function_app.check_parse_pool(Layout())


@pytest.mark.parametrize(
    "result",
    [
        FileNotFoundError(),
        _lock_stat(mode=stat.S_IFREG | 0o644),
        _lock_stat(uid=10002),
        _lock_stat(mode=stat.S_IFLNK | 0o600),
    ],
)
def test_check_6_a_missing_or_wrong_lock_file_fails(
    function_app: ModuleType, passing: pytest.MonkeyPatch, result: Any
) -> None:
    def lstat(self: Path) -> os.stat_result:
        if self.name == "10009.lock":
            if isinstance(result, BaseException):
                raise result
            return result  # type: ignore[no-any-return]
        return _lock_stat()

    passing.setattr(Path, "lstat", lstat)

    with pytest.raises(function_app.ParsePoolExceededError):
        function_app.check_parse_pool(Layout())


@pytest.mark.parametrize("outcome", ["false", "raise", "slow"])
def test_check_7_a_probe_that_does_not_pass_fails(
    function_app: ModuleType, passing: pytest.MonkeyPatch, telemetry: TestExporters, outcome: str
) -> None:
    passing.setattr(function_app, "PROBE_SECONDS", 0.05)

    async def probe() -> bool:
        if outcome == "raise":
            raise OSError("cnry-probe-0a1b2c3d")
        if outcome == "slow":
            await asyncio.sleep(2)
        return False

    _fails(function_app, telemetry, _checks(function_app, probe=probe), "ParseIsolationFailedError")
    assert "cnry-" not in str(telemetry.logs.get_finished_logs())


async def test_the_default_probe_is_the_selfcheck_probe(function_app: ModuleType) -> None:
    assert await function_app.run_isolation_probe() is True


def test_a_failed_settings_check_logs_job_failed_and_emits_no_event(
    function_app: ModuleType, passing: pytest.MonkeyPatch, telemetry: TestExporters
) -> None:
    passing.setenv("DOCCHAT_SQL_THREADS", "1")

    with pytest.raises(function_app.WorkerStartError):
        function_app.create_app()

    (record,) = _named(telemetry, "job_failed")
    assert _attributes(record) == {
        "error.type": "docchat_core.settings.startup.SettingsValidationError"
    }
    assert _named(telemetry, "ingestion_worker_started") == []


def test_a_failed_import_loads_no_function(
    function_app: ModuleType, passing: pytest.MonkeyPatch, telemetry: TestExporters
) -> None:
    passing.delenv("DOCCHAT_SEARCH_ENDPOINT")

    with pytest.raises(Exception, match=r"^ingestion worker start-up check failed$") as raised:
        _import(passing)

    assert type(raised.value).__qualname__ == "WorkerStartError"
    assert len(_named(telemetry, "job_failed")) == 1
    assert _named(telemetry, "ingestion_worker_started") == []


def test_local_emulator_mode_inside_azure_is_refused_under_its_own_name(
    function_app: ModuleType, passing: pytest.MonkeyPatch, telemetry: TestExporters
) -> None:
    passing.setenv("DOCCHAT_LOCAL_EMULATORS", "true")
    passing.setenv("DOCCHAT_LOCAL_SQL_SA_PASSWORD", "Aa1!" + "x" * 20)
    passing.setenv("WEBSITE_SITE_NAME", "func-docchat-ingest-dev-abc123")

    with pytest.raises(function_app.WorkerStartError):
        function_app.create_app()

    (record,) = _named(telemetry, "local_emulators_in_azure")
    assert _attributes(record) == {}
    assert _named(telemetry, "job_failed") == []


def test_an_insecure_endpoint_is_refused_under_its_own_name(
    function_app: ModuleType, passing: pytest.MonkeyPatch, telemetry: TestExporters
) -> None:
    passing.setenv("DOCCHAT_DOCINTEL_ENDPOINT", "http://127.0.0.1:9")

    with pytest.raises(function_app.WorkerStartError):
        function_app.create_app()

    assert len(_named(telemetry, "insecure_endpoint")) == 1
    assert _named(telemetry, "ingestion_worker_started") == []


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("DOCCHAT_TELEMETRY_LOG_LEVEL", "DEBUG"),
        ("DOCCHAT_CORE_IDENTITY_CLIENT_ID", ""),
        ("DOCCHAT_SQL_SERVER", ""),
        ("DOCCHAT_STORAGE_MALWARE_SCANNING_ENABLED", "yes"),
        ("DOCCHAT_OPENAI_EMBEDDING_MODELS", "[]"),
    ],
)
def test_check_2_refuses_each_settings_rule(
    function_app: ModuleType, passing: pytest.MonkeyPatch, name: str, value: str
) -> None:
    passing.setenv(name, value)

    with pytest.raises(SettingsValidationError) as raised:
        function_app.check_settings()

    assert raised.value.settings == (name,)


def test_a_telemetry_setting_that_fails_stops_the_import_before_telemetry(
    function_app: ModuleType, passing: pytest.MonkeyPatch, telemetry: TestExporters
) -> None:
    passing.setenv("DOCCHAT_BUILD_COMMIT", "not-a-commit")

    with pytest.raises(SettingsValidationError):
        function_app.create_app()

    assert _named(telemetry, "ingestion_worker_started") == []
