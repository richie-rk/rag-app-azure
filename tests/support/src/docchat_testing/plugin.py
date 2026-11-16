"""The pytest plugin every DocChat test session loads (`-p docchat_testing.plugin`).

`84-testing.md` section 1.4 and `01-repository.md` section 5.4: the options `--live`, `--deployed`
and `--stage-acceptance`; the automatic `integration` marker for every test under a
`tests/integration/` directory; the compose readiness checks, which fail the session (never skip
it) when a selected integration test meets an unreachable service; the event-loop guard (section
2.8) and the rule that only the guard's own meta-tests may carry `allow_blocking`; the telemetry
capture and the session-wide canary audit (section 2.10); and the skipping of the stage-acceptance
tests of `tests/deployed` without `--stage-acceptance` (section 6.5).

A test meets a compose service through the service's session fixture (`compose_sqlserver`,
`compose_azurite`, `compose_cosmos`, `compose_model_stub`), which the fixtures that connect to the
service request. Before the first test runs, every service that a selected integration test's
fixtures reach is probed once; an integration test that reaches none (the model stub's own suite,
which runs the stub in process) needs no service.
"""

from __future__ import annotations

import asyncio
import gc
import re
import ssl
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Final, Literal

import httpx
import pytest

from docchat_testing.loopguard import ALLOW_BLOCKING_MARKER, guarded_event_loop

if TYPE_CHECKING:
    from collections.abc import Callable, Generator, Iterable, Mapping, Sequence

FIXTURE_MODULES: Final = (
    "docchat_testing.loopguard",
    "docchat_testing.telemetry_capture",
    "docchat_testing.fixtures.session",
    "docchat_testing.fixtures.identities",
    "docchat_testing.fixtures.sql",
    "docchat_testing.fixtures.cosmos",
    "docchat_testing.fixtures.storage",
    "docchat_testing.fixtures.api",
)
"""Modules whose fixtures every session gets: the loop guard, the telemetry capture, the
fixtures over the compose services and the shared principals, and the in-process API
(`84-testing.md` section 1.4).

This module imports nothing of `docchat_core` at its own import, and these are imported in
`pytest_configure`: pytest loads a `-p` plugin before pytest-cov starts measuring, so a
`docchat_core` module first imported here would be missing from the coverage report."""

INTEGRATION_MARKER: Final = "integration"
_INTEGRATION_DIR: Final = ("tests", "integration")
_DEPLOYED_DIR: Final = ("tests", "deployed")
GUARD_META_TESTS: Final = ("tests", "support", "tests")
"""The only directory whose tests may carry `allow_blocking` (section 2.8)."""

CATALOGUE_PATH: Final = Path("docs", "spec", "84-testing.md")
_CATALOGUE_HEADING: Final = "## 5. Test catalogue"
_TEST_NAME_ID_RE: Final = re.compile(r"\Atest_t(?P<number>\d{3})_")
_ROW_ID_RE: Final = re.compile(r"\AT-\d{3}\Z")
_CATALOGUE_COLUMNS: Final = 4
"""ID, test, level, proves."""
# A cell boundary is a `|` that the Markdown table doesn't escape as `\|`.
_CELL_SPLIT_RE: Final = re.compile(r"(?<!\\)\|")

COMPOSE_HINT: Final = "Start the services with `docker compose up -d --wait`."
PROBE_TIMEOUT_SECONDS: Final = 5.0
POLL_INTERVAL_SECONDS: Final = 2.0
_HTTP_OK: Final = 200


# --- The compose services ---------------------------------------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class ComposeService:
    """One `compose.yaml` service as the tests reach it (`84-testing.md` section 2.1)."""

    name: str
    """The compose service name."""
    address: str
    """What a client connects to: the ODBC server form for SQL Server, a URL otherwise."""
    probe: Literal["tcp", "http"]
    probe_host: str
    probe_port: int
    probe_path: str = ""
    wait_seconds: float = 0.0
    """How long readiness is polled; 0 means one attempt, because `docker compose up --wait`
    has already waited for the service's own health check."""

    @property
    def probe_target(self) -> str:
        if self.probe == "http":
            return f"http://{self.probe_host}:{self.probe_port}{self.probe_path}"
        return f"{self.probe_host}:{self.probe_port}"


COMPOSE_SERVICES: dict[str, ComposeService] = {
    "sqlserver": ComposeService(
        name="sqlserver",
        address="127.0.0.1,14330",
        probe="tcp",
        probe_host="127.0.0.1",
        probe_port=14330,
    ),
    "azurite": ComposeService(
        name="azurite",
        address="http://127.0.0.1:10000",
        probe="tcp",
        probe_host="127.0.0.1",
        probe_port=10000,
    ),
    # The emulator has no compose health check; its own probe port answers once it serves.
    "cosmos": ComposeService(
        name="cosmos",
        address="http://127.0.0.1:8081",
        probe="http",
        probe_host="127.0.0.1",
        probe_port=18080,
        probe_path="/ready",
        wait_seconds=180.0,
    ),
    "model-stub": ComposeService(
        name="model-stub",
        address="http://127.0.0.1:8091",
        probe="http",
        probe_host="127.0.0.1",
        probe_port=8091,
        probe_path="/healthz",
    ),
}
"""The compose services by name. Tests of the plugin replace entries to probe a closed port."""

SERVICE_FIXTURES: Final[Mapping[str, str]] = {
    "compose_sqlserver": "sqlserver",
    "compose_azurite": "azurite",
    "compose_cosmos": "cosmos",
    "compose_model_stub": "model-stub",
    # The session fixtures of `docchat_testing.fixtures` that connect to a service.
    "sql_server": "sqlserver",
    "storage_account": "azurite",
    "cosmos_server": "cosmos",
}
"""The fixtures through which a test meets each service."""


@dataclass(slots=True)
class _Readiness:
    """Each service's probe outcome, once probed: `None` when ready, else why not."""

    outcomes: dict[str, str | None] = field(default_factory=dict)


_READINESS_KEY: Final = pytest.StashKey[_Readiness]()


async def _attempt(service: ComposeService) -> str | None:
    if service.probe == "tcp":
        try:
            async with asyncio.timeout(PROBE_TIMEOUT_SECONDS):
                _, writer = await asyncio.open_connection(service.probe_host, service.probe_port)
        except (OSError, TimeoutError) as error:
            return f"no TCP connection to {service.probe_target} ({type(error).__name__})"
        writer.close()
        await writer.wait_closed()
        return None
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(PROBE_TIMEOUT_SECONDS),
            follow_redirects=False,
            verify=_tls_context(),
        ) as client:
            response = await client.get(service.probe_target)
    except httpx.HTTPError as error:
        return f"GET {service.probe_target} failed ({type(error).__name__})"
    if response.status_code != _HTTP_OK:
        return f"GET {service.probe_target} answered {response.status_code}"
    return None


async def probe_service(service: ComposeService) -> str | None:
    """`None` once the service answers its readiness probe, else why it didn't within its
    waiting time."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + service.wait_seconds
    while True:
        reason = await _attempt(service)
        remaining = deadline - loop.time()
        if reason is None or remaining <= 0:
            return reason
        await asyncio.sleep(min(POLL_INTERVAL_SECONDS, remaining))


async def _probe_all(services: Sequence[ComposeService]) -> list[str | None]:
    return list(await asyncio.gather(*(probe_service(service) for service in services)))


def probe_services(services: Sequence[ComposeService]) -> dict[str, str | None]:
    """Probe the services together on a private loop, so the probe neither needs nor disturbs a
    test's event loop."""
    loop = asyncio.new_event_loop()
    try:
        outcomes = loop.run_until_complete(_probe_all(services))
    finally:
        loop.close()
    return {service.name: outcome for service, outcome in zip(services, outcomes, strict=True)}


def _readiness(config: pytest.Config) -> _Readiness:
    readiness = config.stash.get(_READINESS_KEY, None)
    if readiness is None:
        readiness = config.stash[_READINESS_KEY] = _Readiness()
    return readiness


def unreachable_message(service: ComposeService, reason: str, needed_by: Sequence[str]) -> str:
    """The session failure for one unreachable service, naming the tests that need it."""
    first = f"; first: {needed_by[0]}" if needed_by else ""
    return (
        f"compose service {service.name} ({service.address}) is unreachable: {reason}. "
        f"{len(needed_by)} selected test(s) need it{first}. {COMPOSE_HINT}"
    )


def require_services(config: pytest.Config, needed: Mapping[str, Sequence[str]]) -> None:
    """Probe each needed service not probed yet, and end the session as failed, never skipped,
    when one is unreachable. `needed` maps a service name to the node IDs that need it."""
    readiness = _readiness(config)
    fresh = [COMPOSE_SERVICES[name] for name in needed if name not in readiness.outcomes]
    if fresh:
        readiness.outcomes.update(probe_services(fresh))
    failures = [
        unreachable_message(COMPOSE_SERVICES[name], reason, needed[name])
        for name in needed
        if (reason := readiness.outcomes[name]) is not None
    ]
    if failures:
        pytest.exit("\n".join(failures), returncode=pytest.ExitCode.TESTS_FAILED)


def services_needed(items: Iterable[pytest.Item]) -> dict[str, list[str]]:
    """The services the selected integration tests reach through their fixtures."""
    needed: dict[str, list[str]] = {}
    for item in items:
        if item.get_closest_marker(INTEGRATION_MARKER) is None:
            continue
        for fixture_name in getattr(item, "fixturenames", ()):
            service = SERVICE_FIXTURES.get(fixture_name)
            if service is not None:
                needed.setdefault(service, []).append(item.nodeid)
    return needed


def _service_fixture(service_name: str) -> Callable[[pytest.FixtureRequest], ComposeService]:
    def fixture(request: pytest.FixtureRequest) -> ComposeService:
        # The session-start probe covered every test that names this fixture; a test reaching it
        # another way (`getfixturevalue`) is checked here, with the same session failure.
        require_services(request.config, {service_name: [request.node.nodeid]})
        return COMPOSE_SERVICES[service_name]

    fixture.__name__ = f"compose_{service_name.replace('-', '_')}"
    fixture.__doc__ = f"The compose service `{service_name}`, once it answers its probe."
    return fixture


compose_sqlserver = pytest.fixture(scope="session", name="compose_sqlserver")(
    _service_fixture("sqlserver")
)
compose_azurite = pytest.fixture(scope="session", name="compose_azurite")(
    _service_fixture("azurite")
)
compose_cosmos = pytest.fixture(scope="session", name="compose_cosmos")(_service_fixture("cosmos"))
compose_model_stub = pytest.fixture(scope="session", name="compose_model_stub")(
    _service_fixture("model-stub")
)


# --- The test catalogue -----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CatalogueRow:
    """One row of `84-testing.md` section 5."""

    identifier: str
    name: str
    level: str
    proves: str


def _cells(line: str) -> list[str]:
    return [cell.strip() for cell in _CELL_SPLIT_RE.split(line.strip()[1:-1])]


def parse_catalogue(text: str) -> tuple[CatalogueRow, ...]:
    """The rows of every table of section 5 ("Test catalogue"), in document order."""
    lines = text.splitlines()
    try:
        start = lines.index(_CATALOGUE_HEADING)
    except ValueError:
        raise ValueError("the testing specification has no test catalogue") from None
    rows: list[CatalogueRow] = []
    for line in lines[start + 1 :]:
        if line.startswith("## "):
            break
        if not line.startswith("|"):
            continue
        cells = _cells(line)
        if len(cells) == _CATALOGUE_COLUMNS and _ROW_ID_RE.match(cells[0]):
            rows.append(CatalogueRow(*cells))
    return tuple(rows)


def read_catalogue(root: Path) -> tuple[CatalogueRow, ...]:
    """The catalogue of the repository at `root`."""
    return parse_catalogue((root / CATALOGUE_PATH).read_text(encoding="utf-8"))


def catalogue_identifier(test_name: str) -> str | None:
    """The catalogue identifier a test function's name carries (`test_t001_…` → `T-001`,
    section 5.1 item 3)."""
    match = _TEST_NAME_ID_RE.match(test_name)
    return None if match is None else f"T-{match.group('number')}"


STAGE_ONLY_LEVELS: Final = frozenset({"deployed (stage 0)", "stage acceptance", "stage 0"})
"""Levels that, like `deployed (stage acceptance)`, run once in their build stage and again when
their subject changes (section 5.1 item 4), never in a suite's routine run."""


def is_stage_acceptance_level(level: str) -> bool:
    """`deployed (stage acceptance)` (with or without an environment), `deployed (stage 0)`, and
    the `stage acceptance` and `stage 0` rows whose function sits in `tests/deployed`."""
    return level.startswith("deployed (stage acceptance") or level in STAGE_ONLY_LEVELS


# --- Paths ------------------------------------------------------------------------------------


def _relative_parts(config: pytest.Config, path: Path) -> tuple[str, ...]:
    try:
        return path.relative_to(config.rootpath).parts
    except ValueError:
        return path.parts


def _contains(parts: Sequence[str], sequence: Sequence[str]) -> bool:
    width = len(sequence)
    return any(
        tuple(parts[index : index + width]) == tuple(sequence) for index in range(len(parts))
    )


def _under_integration_dir(parts: Sequence[str]) -> bool:
    return _contains(parts, _INTEGRATION_DIR)


def _starts_with(parts: Sequence[str], prefix: Sequence[str]) -> bool:
    return tuple(parts[: len(prefix)]) == tuple(prefix)


# --- Hooks ------------------------------------------------------------------------------------


def _tls_context() -> ssl.SSLContext:
    from docchat_core.clients.tls import TLS_CONTEXT  # noqa: PLC0415 - see FIXTURE_MODULES

    return TLS_CONTEXT


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("docchat")
    group.addoption("--live", action="store_true", default=False, help="run live tests")
    group.addoption("--deployed", action="store_true", default=False, help="run deployed tests")
    group.addoption(
        "--stage-acceptance",
        action="store_true",
        default=False,
        help="run the stage-acceptance tests of tests/deployed",
    )


def pytest_configure(config: pytest.Config) -> None:
    for name in FIXTURE_MODULES:
        if config.pluginmanager.has_plugin(name):
            continue
        if name in sys.modules:
            # This module imported it already (the loop guard), so pytest can't rewrite it.
            config.pluginmanager.register(sys.modules[name], name)
        else:
            config.pluginmanager.import_plugin(name)
    from docchat_testing.tokens import session_signing_keys  # noqa: PLC0415 - after coverage starts

    # The session's RSA key pair takes tens of milliseconds to generate; made here, outside any
    # event loop, a test that first mints a token inside a coroutine doesn't trip the loop guard.
    session_signing_keys()


def _stage_acceptance_skips(config: pytest.Config, items: Sequence[pytest.Item]) -> None:
    deployed = [
        item for item in items if _starts_with(_relative_parts(config, item.path), _DEPLOYED_DIR)
    ]
    if not deployed or config.getoption("--stage-acceptance"):
        return
    try:
        catalogue = read_catalogue(config.rootpath)
    except (OSError, ValueError) as error:
        # Without the catalogue no stage-acceptance test could be told apart, and it would run.
        raise pytest.UsageError(
            f"the test catalogue {CATALOGUE_PATH} can't be read: {error}"
        ) from None
    levels = {row.identifier: row.level for row in catalogue}
    skip = pytest.mark.skip(reason="stage-acceptance test: runs only with --stage-acceptance")
    for item in deployed:
        identifier = catalogue_identifier(getattr(item, "originalname", item.name))
        if identifier is not None and is_stage_acceptance_level(levels.get(identifier, "")):
            item.add_marker(skip)


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    live = config.getoption("--live")
    deployed = config.getoption("--deployed")
    for item in items:
        if _under_integration_dir(_relative_parts(config, item.path)):
            item.add_marker(pytest.mark.integration)
        if item.get_closest_marker("live") is not None and not live:
            item.add_marker(pytest.mark.skip(reason="live test: runs only with --live"))
        if item.get_closest_marker("deployed") is not None and not deployed:
            item.add_marker(pytest.mark.skip(reason="deployed test: runs only with --deployed"))
    _stage_acceptance_skips(config, items)


def pytest_asyncio_loop_factories() -> Mapping[str, Callable[[], asyncio.AbstractEventLoop]]:
    """Every test loop is a guarded loop (section 2.8 step 1)."""
    return {"guarded": guarded_event_loop}


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_setup(item: pytest.Item) -> None:
    marker = item.get_closest_marker(ALLOW_BLOCKING_MARKER)
    if marker is None:
        return
    if not _starts_with(_relative_parts(item.config, item.path), GUARD_META_TESTS):
        pytest.fail(
            "allow_blocking is allowed only in tests/support/tests, the loop guard's own "
            "meta-tests",
            pytrace=False,
        )
    reason = marker.kwargs.get("reason", marker.args[0] if marker.args else None)
    if not isinstance(reason, str) or not reason.strip():
        pytest.fail("allow_blocking needs a reason", pytrace=False)


@pytest.hookimpl(wrapper=True)
def pytest_runtest_protocol(
    item: pytest.Item, nextitem: pytest.Item | None
) -> Generator[None, object, object]:
    """Automatic garbage collection waits while a test runs, so a full collection (tens of
    milliseconds over the session's heap) never lands inside a test's event loop and trips the
    loop guard; the allocations counted meanwhile start it between tests instead. An explicit
    `gc.collect()` still works."""
    del item, nextitem
    gc.disable()
    try:
        return (yield)
    finally:
        gc.enable()


@pytest.hookimpl(tryfirst=True)
def pytest_runtestloop(session: pytest.Session) -> None:
    """Before the first test: freeze the collected heap, probe the services the selected
    integration tests reach, then install the telemetry capture for an integration session."""
    config = session.config
    if config.option.collectonly or not session.items:
        return
    # A full collection scans everything collection allocated (every imported library and
    # collected test). Started by whichever allocation crosses the threshold, it can take
    # 200 ms or more and fail that test's loop guard, so the heap leaves the collector's reach.
    gc.collect()
    gc.freeze()
    needed = services_needed(session.items)
    if needed:
        require_services(config, needed)
    if any(item.get_closest_marker(INTEGRATION_MARKER) for item in session.items):
        from docchat_testing.telemetry_capture import session_capture  # noqa: PLC0415 - see FIXTURE_MODULES

        session_capture(config)


def pytest_sessionfinish(session: pytest.Session) -> None:
    """The session-wide canary audit over everything the capture received."""
    from docchat_testing.telemetry_capture import CAPTURE_KEY, format_findings  # noqa: PLC0415 - see FIXTURE_MODULES

    capture = session.config.stash.get(CAPTURE_KEY, None)
    if capture is None:
        return
    findings = capture.audit()
    if not findings:
        return
    report = format_findings(findings)
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    if reporter is None:
        sys.stderr.write(report + "\n")
    else:
        reporter.write_sep("=", "canary audit", red=True)
        reporter.write_line(report)
    if session.exitstatus in {pytest.ExitCode.OK, pytest.ExitCode.NO_TESTS_COLLECTED}:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
