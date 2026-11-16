"""The plugin: integration marking, the catalogue reader, stage-acceptance skipping, the compose
readiness checks, and T-563, the compose service meta-test (`84-testing.md` sections 1.4, 2.1 and
6.5), which runs nested sessions with pytester and watches them fail."""

from __future__ import annotations

import ast
import gc
import shlex
import socket
import threading
import tomllib
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, override

import pytest

from docchat_testing import plugin
from docchat_testing.plugin import (
    COMPOSE_SERVICES,
    SERVICE_FIXTURES,
    CatalogueRow,
    ComposeService,
    _relative_parts,
    _under_integration_dir,
    catalogue_identifier,
    is_stage_acceptance_level,
    parse_catalogue,
    probe_services,
    read_catalogue,
    services_needed,
    unreachable_message,
)

pytest_plugins = ("pytester",)

REPOSITORY_ROOT = Path(__file__).resolve().parents[4]


def _repository_ini() -> str:
    """The repository's own pytest configuration, so nested sessions run as real ones do."""
    pyproject = tomllib.loads((REPOSITORY_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    options: dict[str, Any] = pyproject["tool"]["pytest"]["ini_options"]
    lines = ["[pytest]"]
    for key, value in options.items():
        if key == "testpaths":
            continue
        if key == "addopts":
            lines.append("addopts = " + " ".join(shlex.quote(option) for option in value))
        elif isinstance(value, list):
            lines.append(f"{key} =")
            lines.extend(f"    {entry}" for entry in value)
        elif isinstance(value, bool):
            lines.append(f"{key} = {str(value).lower()}")
        else:
            lines.append(f"{key} = {value}")
    return "\n".join(lines) + "\n"


@pytest.fixture
def closed_port() -> Iterator[int]:
    """A port bound without listening, so every connection to it is refused."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as bound:
        bound.bind(("127.0.0.1", 0))
        yield bound.getsockname()[1]


@pytest.fixture
def listening_port() -> Iterator[int]:
    """A listening port; the kernel completes each connection without an accept."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.bind(("127.0.0.1", 0))
        server.listen(16)
        yield server.getsockname()[1]


class _Health(BaseHTTPRequestHandler):
    @override
    def log_message(self, format: str, *args: Any) -> None:
        return

    def do_GET(self) -> None:
        status = 200 if self.path in {"/ready", "/healthz"} else 503
        self.send_response(status)
        self.send_header("Content-Length", "0")
        self.end_headers()


@pytest.fixture
def health_server() -> Iterator[int]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Health)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def _tcp(name: str, port: int, *, wait: float = 0.0) -> ComposeService:
    return ComposeService(
        name=name,
        address=f"127.0.0.1,{port}",
        probe="tcp",
        probe_host="127.0.0.1",
        probe_port=port,
        wait_seconds=wait,
    )


def _http(name: str, port: int, path: str) -> ComposeService:
    return ComposeService(
        name=name,
        address=f"http://127.0.0.1:{port}",
        probe="http",
        probe_host="127.0.0.1",
        probe_port=port,
        probe_path=path,
    )


# Integration marking


def test_integration_dir_detected() -> None:
    assert _under_integration_dir(("tests", "integration", "ops", "test_x.py"))
    assert _under_integration_dir(("apps", "api", "tests", "integration", "test_y.py"))


def test_unit_dir_not_integration() -> None:
    assert not _under_integration_dir(("apps", "api", "tests", "unit", "test_y.py"))
    assert not _under_integration_dir(("tests", "integrations", "test_y.py"))


def test_paths_are_read_below_the_rootdir(pytestconfig: pytest.Config) -> None:
    inside = pytestconfig.rootpath / "apps" / "api" / "tests" / "unit" / "test_y.py"

    assert _relative_parts(pytestconfig, inside) == ("apps", "api", "tests", "unit", "test_y.py")
    assert _relative_parts(pytestconfig, Path("/elsewhere/tests/integration/t.py"))[-3:] == (
        "tests",
        "integration",
        "t.py",
    )


def test_tests_under_an_integration_directory_are_marked(pytester: pytest.Pytester) -> None:
    pytester.makeini(_repository_ini())
    for directory in (
        "tests/integration/chat",
        "apps/api/tests/integration",
        "apps/api/tests/unit",
    ):
        path = pytester.path / directory
        path.mkdir(parents=True)
        (path / f"test_{path.parent.name}_{path.name}.py").write_text(
            "def test_it() -> None:\n    pass\n", encoding="utf-8"
        )

    recorder = pytester.inline_run("-m", "integration", "--collect-only")
    items = recorder.getcall("pytest_collection_finish").session.items

    assert sorted(item.nodeid.rsplit("/", 1)[0] for item in items) == [
        "apps/api/tests/integration",
        "tests/integration/chat",
    ]


# The catalogue

_CATALOGUE = r"""# 84 — Testing

## 5. Test catalogue

### 5.1 Conventions

| ID | Test | Level | Proves |
|---|---|---|---|
| T-901 | the first test | unit | One thing. |

### 5.2 More

| ID | Test | Level | Proves |
|---|---|---|---|
| T-902 | the second test | deployed (stage acceptance) | `ls x \| wc -l` returns 3. |
| T-903 | the third test | deployed (stage 0) | Another thing. |
| T-904 | the fourth test | deployed smoke | A smoke thing. |
| T-905 | the fifth test | deployed (stage acceptance, `dev`) | A dev thing. |

## 6. Deployed tests

| T-999 | not a catalogue row | unit | Outside section 5. |
"""


def test_the_catalogue_rows_of_section_5_are_read() -> None:
    rows = parse_catalogue(_CATALOGUE)

    assert [row.identifier for row in rows] == ["T-901", "T-902", "T-903", "T-904", "T-905"]
    assert rows[1] == CatalogueRow(
        "T-902", "the second test", "deployed (stage acceptance)", r"`ls x \| wc -l` returns 3."
    )


def test_a_document_without_the_catalogue_is_refused() -> None:
    with pytest.raises(ValueError, match="no test catalogue"):
        parse_catalogue("# 84 — Testing\n")


def test_the_repository_catalogue_reads_every_row() -> None:
    rows = read_catalogue(REPOSITORY_ROOT)

    assert len(rows) > 500
    assert rows[0].identifier == "T-001"
    assert {row.level for row in rows} >= {"unit", "integration", "meta", "withdrawn"}


@pytest.mark.parametrize(
    ("name", "identifier"),
    [
        ("test_t001_authorisation_matrix", "T-001"),
        ("test_t573_streams", "T-573"),
        ("test_t57_short", None),
        ("test_t0012_long", None),
        ("test_plain", None),
        ("t001_no_prefix", None),
    ],
)
def test_the_identifier_comes_from_the_test_name(name: str, identifier: str | None) -> None:
    assert catalogue_identifier(name) == identifier


@pytest.mark.parametrize(
    ("level", "stage_acceptance"),
    [
        ("deployed (stage acceptance)", True),
        ("deployed (stage acceptance, `dev`)", True),
        ("deployed (stage 0)", True),
        ("stage acceptance", True),
        ("stage 0", True),
        ("live (`dev`, stage acceptance)", False),
        ("deployed smoke", False),
        ("unit", False),
    ],
)
def test_the_stage_acceptance_levels(level: str, stage_acceptance: bool) -> None:
    assert is_stage_acceptance_level(level) is stage_acceptance


# Stage acceptance

_DEPLOYED_TESTS = """
def test_t902_needs_the_flag() -> None:
    pass


def test_t903_needs_the_flag_too() -> None:
    pass


def test_t904_smoke() -> None:
    pass


def test_t905_needs_the_flag_in_dev() -> None:
    pass


def test_plain() -> None:
    pass
"""


@pytest.fixture
def deployed_session(pytester: pytest.Pytester) -> pytest.Pytester:
    pytester.makeini(_repository_ini())
    (pytester.mkdir("docs") / "spec").mkdir()
    (pytester.path / "docs" / "spec" / "84-testing.md").write_text(_CATALOGUE, encoding="utf-8")
    (pytester.mkdir("tests") / "deployed").mkdir()
    (pytester.path / "tests" / "deployed" / "test_suite.py").write_text(
        _DEPLOYED_TESTS, encoding="utf-8"
    )
    (pytester.path / "tests" / "other").mkdir()
    (pytester.path / "tests" / "other" / "test_named.py").write_text(
        "def test_t902_outside_deployed() -> None:\n    pass\n", encoding="utf-8"
    )
    return pytester


def test_stage_acceptance_tests_skip_without_the_flag(deployed_session: pytest.Pytester) -> None:
    recorder = deployed_session.inline_run()

    skipped = {r.nodeid.rsplit("::", 1)[-1] for r in recorder.getreports() if r.skipped}
    assert skipped == {
        "test_t902_needs_the_flag",
        "test_t903_needs_the_flag_too",
        "test_t905_needs_the_flag_in_dev",
    }
    recorder.assertoutcome(passed=3, skipped=3)


def test_stage_acceptance_tests_run_with_the_flag(deployed_session: pytest.Pytester) -> None:
    recorder = deployed_session.inline_run("--stage-acceptance")

    recorder.assertoutcome(passed=6)


def test_deployed_tests_without_the_catalogue_stop_the_session(
    pytester: pytest.Pytester,
) -> None:
    pytester.makeini(_repository_ini())
    (pytester.mkdir("tests") / "deployed").mkdir()
    (pytester.path / "tests" / "deployed" / "test_suite.py").write_text(
        _DEPLOYED_TESTS, encoding="utf-8"
    )

    result = pytester.runpytest_inprocess()

    assert result.ret == pytest.ExitCode.USAGE_ERROR
    result.stderr.fnmatch_lines(["*the test catalogue docs/spec/84-testing.md can't be read*"])


def test_live_and_deployed_markers_skip_without_their_flags(pytester: pytest.Pytester) -> None:
    pytester.makeini(_repository_ini())
    pytester.makepyfile(
        test_marked="""
import pytest


@pytest.mark.live
def test_live() -> None:
    pass


@pytest.mark.deployed
def test_deployed() -> None:
    pass
"""
    )

    pytester.inline_run().assertoutcome(skipped=2)
    pytester.inline_run("--live", "--deployed").assertoutcome(passed=2)


# The compose services


def test_the_compose_services_are_those_of_compose_yaml() -> None:
    assert {name: service.address for name, service in COMPOSE_SERVICES.items()} == {
        "sqlserver": "127.0.0.1,14330",
        "azurite": "http://127.0.0.1:10000",
        "cosmos": "http://127.0.0.1:8081",
        "model-stub": "http://127.0.0.1:8091",
    }
    assert COMPOSE_SERVICES["sqlserver"].probe_target == "127.0.0.1:14330"
    assert COMPOSE_SERVICES["azurite"].probe_target == "127.0.0.1:10000"
    assert COMPOSE_SERVICES["cosmos"].probe_target == "http://127.0.0.1:18080/ready"
    assert COMPOSE_SERVICES["cosmos"].wait_seconds == 180
    assert COMPOSE_SERVICES["model-stub"].probe_target == "http://127.0.0.1:8091/healthz"
    assert set(SERVICE_FIXTURES.values()) == set(COMPOSE_SERVICES)


def test_a_listening_tcp_service_is_ready(listening_port: int) -> None:
    assert probe_services([_tcp("sqlserver", listening_port)]) == {"sqlserver": None}


def test_a_refused_tcp_service_is_unreachable(closed_port: int) -> None:
    outcome = probe_services([_tcp("sqlserver", closed_port)])["sqlserver"]

    assert outcome == f"no TCP connection to 127.0.0.1:{closed_port} (ConnectionRefusedError)"


def test_an_http_service_is_ready_when_its_probe_answers_200(health_server: int) -> None:
    services = [_http("cosmos", health_server, "/ready"), _http("other", health_server, "/down")]

    outcomes = probe_services(services)

    assert outcomes == {
        "cosmos": None,
        "other": f"GET http://127.0.0.1:{health_server}/down answered 503",
    }


def test_an_http_service_that_refuses_connections_is_unreachable(closed_port: int) -> None:
    outcome = probe_services([_http("model-stub", closed_port, "/healthz")])["model-stub"]

    assert outcome == f"GET http://127.0.0.1:{closed_port}/healthz failed (ConnectError)"


def test_a_polled_service_is_retried_until_its_wait_ends(closed_port: int) -> None:
    outcome = probe_services([_tcp("cosmos", closed_port, wait=0.3)])["cosmos"]

    assert outcome is not None
    assert outcome.startswith("no TCP connection")


def test_the_message_names_the_service_and_the_tests() -> None:
    message = unreachable_message(
        COMPOSE_SERVICES["sqlserver"], "no TCP connection", ["a.py::test_one", "b.py::test_two"]
    )

    assert message == (
        "compose service sqlserver (127.0.0.1,14330) is unreachable: no TCP connection. "
        "2 selected test(s) need it; first: a.py::test_one. "
        "Start the services with `docker compose up -d --wait`."
    )


def test_only_integration_tests_reaching_a_service_need_it(pytester: pytest.Pytester) -> None:
    pytester.makeini(_repository_ini())
    pytester.makeconftest(
        """
import pytest


@pytest.fixture
def database(compose_sqlserver):
    return compose_sqlserver
"""
    )
    directory = pytester.path / "tests" / "integration"
    directory.mkdir(parents=True)
    (directory / "test_reach.py").write_text(
        "def test_via_fixture(database) -> None:\n    pass\n\n\n"
        "def test_direct(compose_cosmos, compose_model_stub) -> None:\n    pass\n\n\n"
        "def test_none() -> None:\n    pass\n",
        encoding="utf-8",
    )
    pytester.makepyfile(test_unit="def test_unit(compose_azurite) -> None:\n    pass\n")

    items = pytester.inline_genitems()[0]

    assert services_needed(items) == {
        "sqlserver": ["tests/integration/test_reach.py::test_via_fixture"],
        "cosmos": ["tests/integration/test_reach.py::test_direct"],
        "model-stub": ["tests/integration/test_reach.py::test_direct"],
    }


# T-563


def _needs_sql(pytester: pytest.Pytester, body: str) -> None:
    pytester.makeini(_repository_ini())
    directory = pytester.path / "tests" / "integration"
    directory.mkdir(parents=True)
    (directory / "test_needs_sql.py").write_text(body, encoding="utf-8")


def test_t563_an_integration_session_fails_not_skips_when_a_service_is_down(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, closed_port: int
) -> None:
    monkeypatch.setitem(COMPOSE_SERVICES, "sqlserver", _tcp("sqlserver", closed_port))
    _needs_sql(pytester, "def test_query(compose_sqlserver) -> None:\n    pass\n")

    result = pytester.runpytest_inprocess()

    assert result.ret == pytest.ExitCode.TESTS_FAILED
    result.assert_outcomes()
    expected = (
        f"*compose service sqlserver (127.0.0.1,{closed_port}) is unreachable: "
        "no TCP connection*1 selected test(s) need it; "
        "first: tests/integration/test_needs_sql.py::test_query*"
    )
    result.stdout.fnmatch_lines([expected])
    assert "skipped" not in result.stdout.str()


def test_t563_a_service_reached_at_run_time_fails_the_session_too(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, closed_port: int
) -> None:
    monkeypatch.setitem(COMPOSE_SERVICES, "sqlserver", _tcp("sqlserver", closed_port))
    _needs_sql(
        pytester,
        "def test_query(request) -> None:\n    request.getfixturevalue('compose_sqlserver')\n",
    )

    result = pytester.runpytest_inprocess()

    assert result.ret == pytest.ExitCode.TESTS_FAILED
    result.assert_outcomes()
    result.stdout.fnmatch_lines(["*compose service sqlserver * is unreachable*"])


def test_t563_deselected_integration_tests_need_no_service(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, closed_port: int
) -> None:
    monkeypatch.setitem(COMPOSE_SERVICES, "sqlserver", _tcp("sqlserver", closed_port))
    _needs_sql(pytester, "def test_query(compose_sqlserver) -> None:\n    pass\n")
    pytester.makepyfile(test_unit="def test_unit() -> None:\n    pass\n")

    result = pytester.runpytest_inprocess("-m", "not integration")

    assert result.ret == pytest.ExitCode.OK
    result.assert_outcomes(passed=1, deselected=1)


def test_t563_reachable_services_let_the_session_run(
    pytester: pytest.Pytester, listening_port: int
) -> None:
    # Its own process: an integration session that runs installs the telemetry capture.
    _needs_sql(
        pytester,
        "def test_query(compose_sqlserver) -> None:\n"
        f"    assert compose_sqlserver.probe_port == {listening_port}\n\n\n"
        "def test_no_service() -> None:\n    pass\n",
    )
    pytester.makeconftest(
        f"""
from docchat_testing import plugin

plugin.COMPOSE_SERVICES["sqlserver"] = plugin.ComposeService(
    name="sqlserver",
    address="127.0.0.1,{listening_port}",
    probe="tcp",
    probe_host="127.0.0.1",
    probe_port={listening_port},
)
"""
    )

    result = pytester.runpytest_subprocess()

    assert result.ret == pytest.ExitCode.OK
    result.assert_outcomes(passed=2)


def test_the_plugin_registers_its_fixture_modules(pytestconfig: pytest.Config) -> None:
    for name in plugin.FIXTURE_MODULES:
        assert pytestconfig.pluginmanager.has_plugin(name)


def test_the_plugin_imports_no_core_module_at_its_own_import() -> None:
    # pytest loads a -p plugin before pytest-cov starts, so a core import there escapes coverage.
    tree = ast.parse(Path(plugin.__file__).read_text(encoding="utf-8"))
    top_level = [
        node.module
        for node in tree.body
        if isinstance(node, ast.ImportFrom) and node.module is not None
    ] + [alias.name for node in tree.body if isinstance(node, ast.Import) for alias in node.names]
    assert not [name for name in top_level if name.startswith("docchat_core")]


def test_the_collected_heap_is_frozen_before_the_first_test() -> None:
    assert gc.get_freeze_count() > 0


def test_automatic_collection_waits_while_a_test_runs() -> None:
    assert not gc.isenabled()
