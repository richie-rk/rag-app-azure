"""Unit tests of `scripts/deploy/deploy_web.py` (T-462, T-463; `83-infrastructure.md` 10.10).

`az`, `azd`, `node` and `npx` are faked at the script's `run_command` seam and the site at its
`http_get` seam, so nothing reaches Azure.
"""

from __future__ import annotations

import http.server
import importlib.util
import io
import json
import sys
import tarfile
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any, override

import pytest

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Mapping, Sequence
    from types import ModuleType

REPO_ROOT = Path(__file__).resolve().parents[4]
TOKEN = "swa-deployment-token-value-0123456789"
TENANT_ID = "00000000-0000-4000-8000-000000000002"
WEB_CLIENT_ID = "11111111-1111-4111-8111-111111111111"
API_BASE_URL = "https://app-docchat-api-dev-abc123.azurewebsites.net"
API_SCOPE = f"api://{TENANT_ID}/docchat-dev-abc123-api/access_as_user"
HOST = "nice-sky-0a1b2c3d4.6.azurestaticapps.net"
CHECKSUM = "ab" * 32
BUILD = "1.0.027911"
CONFIG = {
    "tenantId": TENANT_ID,
    "clientId": WEB_CLIENT_ID,
    "apiBaseUrl": API_BASE_URL,
    "apiScope": API_SCOPE,
}
CSP = (
    "default-src 'none'; script-src 'self'; img-src 'self' data: blob:; "
    f"connect-src 'self' {API_BASE_URL} https://login.microsoftonline.com; frame-ancestors 'none'"
)


def _load_script(relative: str, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, REPO_ROOT / relative)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


deploy_web = _load_script("scripts/deploy/deploy_web.py", "docchat_script_deploy_web")


def _write_cli(folder: Path, pinned: str = "2.0.10", installed: str | None = "2.0.10") -> Path:
    """A CLI folder as `npm ci --ignore-scripts` leaves it: the package and the installed CLI."""
    folder.mkdir(parents=True, exist_ok=True)
    manifest = {"private": True, "dependencies": {"@azure/static-web-apps-cli": pinned}}
    (folder / "package.json").write_text(json.dumps(manifest))
    if installed is not None:
        package = folder / "node_modules" / "@azure" / "static-web-apps-cli"
        package.mkdir(parents=True)
        (package / "package.json").write_text(json.dumps({"version": installed}))
    return folder


def _archive(path: Path, cli: Path, prefix: str = "swa-cli") -> Path:
    with tarfile.open(path, "w:gz") as bundle:
        bundle.add(cli, arcname=prefix or ".")
    return path


class FakeCommands:
    def __init__(self, home: Path) -> None:
        self.home = home
        self.calls: list[tuple[list[str], Mapping[str, str] | None, Path | None]] = []
        self.values = {
            "AZURE_TENANT_ID": TENANT_ID,
            "WEB_CLIENT_ID": WEB_CLIENT_ID,
            "API_BASE_URL": API_BASE_URL,
            "API_SCOPE": API_SCOPE,
            "WEB_BASE_URL": f"https://{HOST}",
            "DOCCHAT_STATIC_WEB_APP_NAME": "stapp-docchat-dev",
            "DOCCHAT_SQL_SERVER": "sql-docchat-dev-abc123.database.windows.net",
        }
        self.azd_status = 0
        self.azd_stdout: str | None = None
        self.generator_status = 0
        self.generator_writes = True
        self.cli_status = 0
        self.cli_output = (
            "Deploying front-end files from folder:\n"
            f"Using deployment token {TOKEN}\n"
            f"Project deployed to https://{HOST}\n"
        )
        self.cli_saves_metadata = True
        self.cli_installed: bool | None = None
        self.host = HOST

    def __call__(
        self, args: Sequence[str], *, env: Mapping[str, str] | None = None, cwd: Path | None = None
    ) -> Any:
        words = list(args)
        self.calls.append((words, env, cwd))
        result = deploy_web.CommandResult
        joined = " ".join(words)
        if joined.startswith("azd env get-values"):
            stdout = self.azd_stdout if self.azd_stdout is not None else json.dumps(self.values)
            return result(self.azd_status, stdout, "ERROR: not signed in\n")
        if words[1:2] == ["apps/web/scripts/write-env-config.mjs"]:
            if self.generator_status == 0 and self.generator_writes:
                site = Path(words[3])
                (site / "config.json").write_text(json.dumps(CONFIG, separators=(",", ":")))
                (site / "staticwebapp.config.json").write_text("{}")
            return result(self.generator_status, "", "API_SCOPE is not an api:// URI\n")
        if joined.startswith("az staticwebapp secrets list"):
            return result(0, TOKEN + "\n", "")
        if joined.startswith("az staticwebapp show"):
            return result(0, self.host + "\n", "")
        if joined.startswith("npx --no-install swa deploy"):
            assert cwd is not None
            # The archive's folder is gone once the script ends, so the fake looks at it now.
            installed = cwd / "node_modules" / "@azure" / "static-web-apps-cli" / "package.json"
            self.cli_installed = installed.is_file()
            if self.cli_saves_metadata:
                folder = self.home / ".swa" / "deploy"
                folder.mkdir(parents=True, exist_ok=True)
                saved = {"metadata": {"version": "stable", "buildId": BUILD}, "checksum": CHECKSUM}
                (folder / "StaticSitesClient.json").write_text(json.dumps(saved))
            return result(self.cli_status, self.cli_output, "")
        raise AssertionError(f"unexpected command {joined}")

    def call(self, prefix: str) -> tuple[list[str], Mapping[str, str] | None, Path | None]:
        return next(c for c in self.calls if " ".join(c[0]).startswith(prefix))

    def ran(self, prefix: str) -> bool:
        return any(" ".join(c[0]).startswith(prefix) for c in self.calls)


class FakeSite:
    """Answers of `GET /config.json`, one per attempt; the last one repeats."""

    def __init__(self, *answers: Callable[[], Any]) -> None:
        self.answers = list(answers) or [_answer]
        self.urls: list[str] = []

    def __call__(self, url: str) -> Any:
        self.urls.append(url)
        return (self.answers.pop(0) if len(self.answers) > 1 else self.answers[0])()


def _answer(status: int = 200, body: Any = None, csp: str = CSP) -> Any:
    content = json.dumps(CONFIG if body is None else body).encode()
    return deploy_web.HttpAnswer(status, {"Content-Security-Policy": csp}, content)


@pytest.fixture
def dist(tmp_path: Path) -> Path:
    build = tmp_path / "web-dist"
    (build / "assets").mkdir(parents=True)
    (build / "index.html").write_text("<!doctype html>")
    (build / "assets" / "app.js").write_text("console.log(1)")
    return build


@pytest.fixture
def sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    recorded: list[float] = []
    monkeypatch.setattr(deploy_web, "sleep", recorded.append)
    return recorded


@pytest.fixture
def site(monkeypatch: pytest.MonkeyPatch) -> FakeSite:
    fake = FakeSite()
    monkeypatch.setattr(deploy_web, "http_get", fake)
    return fake


@pytest.fixture
def commands(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, sleeps: list[float], site: FakeSite
) -> FakeCommands:
    home = tmp_path / "home"
    home.mkdir()
    fake = FakeCommands(home)
    monkeypatch.setattr(deploy_web, "run_command", fake)
    monkeypatch.setattr(deploy_web, "SWA_CLI_DIR", _write_cli(tmp_path / "checkout-swa-cli"))
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "summary.md"))
    monkeypatch.setenv("ACTIONS_ID_TOKEN_REQUEST_URL", "https://pipelines.example/idtoken")
    monkeypatch.setenv("ACTIONS_ID_TOKEN_REQUEST_TOKEN", "request-token")
    monkeypatch.setenv("ACTIONS_RUNTIME_TOKEN", "runtime-token")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", "/usr/local/bin:/usr/bin")
    return fake


def _failure(capsys: pytest.CaptureFixture[str]) -> dict[str, str]:
    parsed: dict[str, str] = json.loads(capsys.readouterr().err.strip().splitlines()[-1])
    return parsed


def _summary(tmp_path: Path) -> str:
    return (tmp_path / "summary.md").read_text()


# --- T-462: the generator's inputs and the CLI's environment ------------------------------------


def test_t462_generator_gets_exactly_the_four_inputs(commands: FakeCommands, dist: Path) -> None:
    assert deploy_web.main(["--env", "dev", "--dist", str(dist)]) == 0

    words, env, cwd = commands.call(deploy_web.shutil.which("node") or "node")
    assert words[1:3] == ["apps/web/scripts/write-env-config.mjs", "--dist"]
    assert env == {
        "AZURE_TENANT_ID": TENANT_ID,
        "WEB_CLIENT_ID": WEB_CLIENT_ID,
        "API_BASE_URL": API_BASE_URL,
        "API_SCOPE": API_SCOPE,
    }
    assert cwd is None  # the repository root, where the command's relative path starts
    site = Path(words[3])
    assert site.name == "site"
    assert site.parent != dist.parent
    assert sorted(path.name for path in dist.iterdir()) == ["assets", "index.html"]


def test_t462_the_cli_gets_the_token_only_as_swa_cli_deployment_token(
    commands: FakeCommands, dist: Path, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    assert deploy_web.main(["--env", "dev", "--dist", str(dist)]) == 0

    words, env, cwd = commands.call("npx")
    assert words[:4] == ["npx", "--no-install", "swa", "deploy"]
    assert Path(words[4]).name == "site"
    assert words[5:] == ["--env", "production", "--no-use-keychain"]
    assert env == {
        "PATH": "/usr/local/bin:/usr/bin",
        "HOME": str(commands.home),
        "SWA_CLI_DEPLOYMENT_TOKEN": TOKEN,
        "SWA_CLI_DEPLOY_BINARY_VERSION": "stable",
    }
    assert cwd == deploy_web.SWA_CLI_DIR
    assert all(TOKEN not in word for call in commands.calls for word in call[0])
    holders = [call[0][0] for call in commands.calls if TOKEN in (call[1] or {}).values()]
    assert holders == ["npx"]
    captured = capsys.readouterr()
    assert captured.out.splitlines()[0] == f"::add-mask::{TOKEN}"
    assert captured.out.count(TOKEN) == 1
    assert TOKEN not in captured.err
    assert TOKEN not in _summary(tmp_path)
    assert captured.out.splitlines()[-1] == json.dumps({"web": "deployed", "env": "dev"})


def test_outside_github_actions_the_token_is_never_printed(
    monkeypatch: pytest.MonkeyPatch,
    commands: FakeCommands,
    dist: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("GITHUB_ACTIONS")
    monkeypatch.delenv("GITHUB_STEP_SUMMARY")

    assert deploy_web.main(["--env", "dev", "--dist", str(dist)]) == 0

    captured = capsys.readouterr()
    assert TOKEN not in captured.out + captured.err
    assert "Using deployment token ***" in captured.out


def test_the_binary_build_id_and_checksum_go_to_the_job_summary(
    commands: FakeCommands, dist: Path, tmp_path: Path
) -> None:
    assert deploy_web.main(["--env", "dev", "--dist", str(dist)]) == 0

    summary = _summary(tmp_path)
    assert f"- build ID: {BUILD}" in summary
    assert f"- SHA-256: {CHECKSUM}" in summary


def test_without_the_cli_metadata_the_binary_comes_from_the_cli_output(
    commands: FakeCommands, dist: Path, tmp_path: Path
) -> None:
    commands.cli_saves_metadata = False
    commands.cli_output = (
        "- Downloading https://swalocaldeploy.azureedge.net/downloads/1.0.028000/linux/"
        "StaticSitesClient@1.0.028000\n"
        f"Checksum: {'CD' * 32}\nProject deployed\n"
    )

    assert deploy_web.main(["--env", "dev", "--dist", str(dist)]) == 0

    summary = _summary(tmp_path)
    assert "- build ID: 1.0.028000" in summary
    assert f"- SHA-256: {'cd' * 32}" in summary


def test_a_binary_the_cli_doesnt_report_is_named_so(
    commands: FakeCommands, dist: Path, tmp_path: Path
) -> None:
    commands.cli_saves_metadata = False

    assert deploy_web.main(["--env", "dev", "--dist", str(dist)]) == 0

    summary = _summary(tmp_path)
    assert "- build ID: not reported" in summary
    assert "- SHA-256: not reported" in summary


# --- the Static Web App and its deployment token ----------------------------------------------


def test_the_token_is_read_for_the_environments_static_web_app(
    commands: FakeCommands, dist: Path
) -> None:
    commands.values["DOCCHAT_STATIC_WEB_APP_NAME"] = "stapp-docchat-prod"

    assert deploy_web.main(["--env", "prod", "--dist", str(dist)]) == 0

    words, _, _ = commands.call("az staticwebapp secrets list")
    assert words[words.index("--name") + 1] == "stapp-docchat-prod"
    assert words[words.index("--resource-group") + 1] == "rg-docchat-prod"
    assert words[words.index("--query") + 1] == "properties.apiKey"
    assert words[words.index("--output") + 1] == "tsv"
    words, _, _ = commands.call("az staticwebapp show")
    assert words[words.index("--name") + 1] == "stapp-docchat-prod"


@pytest.mark.parametrize("name", ["stapp-docchat-prod", "", "stapp-other"])
def test_a_static_web_app_of_another_environment_is_never_deployed(
    commands: FakeCommands, dist: Path, capsys: pytest.CaptureFixture[str], name: str
) -> None:
    commands.values["DOCCHAT_STATIC_WEB_APP_NAME"] = name

    assert deploy_web.main(["--env", "dev", "--dist", str(dist)]) == 2

    assert _failure(capsys)["code"] == "swa_deploy_failed"
    assert not commands.ran("az staticwebapp")
    assert not commands.ran("npx")


# --- the pinned CLI -----------------------------------------------------------------------------


@pytest.mark.parametrize("prefix", ["swa-cli", ""], ids=["folder", "flat"])
def test_the_pinned_cli_comes_from_the_archive(
    commands: FakeCommands, dist: Path, tmp_path: Path, prefix: str
) -> None:
    archive = _archive(tmp_path / "swa-cli.tgz", _write_cli(tmp_path / "built" / "swa-cli"), prefix)

    assert deploy_web.main(["--env", "dev", "--dist", str(dist), "--swa-cli", str(archive)]) == 0

    _, _, cwd = commands.call("npx")
    assert cwd is not None
    assert cwd != deploy_web.SWA_CLI_DIR
    assert commands.cli_installed is True


def test_an_operator_may_name_an_installed_cli_folder(
    commands: FakeCommands, dist: Path, tmp_path: Path
) -> None:
    folder = _write_cli(tmp_path / "operator" / "swa-cli")

    assert deploy_web.main(["--env", "dev", "--dist", str(dist), "--swa-cli", str(folder)]) == 0

    assert commands.call("npx")[2] == folder


@pytest.mark.parametrize(
    ("pinned", "installed"),
    [("2.0.10", "2.0.9"), ("^2.0.10", "2.0.10"), ("2.0.10", None)],
    ids=["other-version", "range", "not-installed"],
)
def test_a_cli_that_isnt_the_pinned_version_never_gets_the_token(
    *,
    commands: FakeCommands,
    dist: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    pinned: str,
    installed: str | None,
) -> None:
    cli = _write_cli(tmp_path / "built" / "swa-cli", pinned=pinned, installed=installed)
    archive = _archive(tmp_path / "swa-cli.tgz", cli)

    assert deploy_web.main(["--env", "dev", "--dist", str(dist), "--swa-cli", str(archive)]) == 2

    assert _failure(capsys)["code"] == "swa_deploy_failed"
    assert not commands.ran("az staticwebapp secrets list")
    assert not commands.ran("npx")


def test_a_local_deploy_without_npm_ci_names_the_command(
    monkeypatch: pytest.MonkeyPatch,
    commands: FakeCommands,
    dist: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(deploy_web, "SWA_CLI_DIR", _write_cli(tmp_path / "bare", installed=None))

    assert deploy_web.main(["--env", "dev", "--dist", str(dist)]) == 2

    failure = _failure(capsys)
    assert failure["code"] == "swa_deploy_failed"
    assert "npm ci --ignore-scripts" in failure["detail"]


def test_an_archive_that_isnt_a_cli_package_is_refused(
    commands: FakeCommands, dist: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    broken = tmp_path / "broken.tgz"
    broken.write_bytes(b"not a tar archive")
    empty = tmp_path / "empty.tgz"
    with tarfile.open(empty, "w:gz") as bundle:
        info = tarfile.TarInfo("readme.txt")
        bundle.addfile(info, io.BytesIO(b""))

    for archive in (broken, empty):
        assert (
            deploy_web.main(["--env", "dev", "--dist", str(dist), "--swa-cli", str(archive)]) == 2
        )
        assert _failure(capsys)["code"] == "swa_deploy_failed"
    assert not commands.ran("az staticwebapp secrets list")


# --- failures before and during the upload -------------------------------------------------------


def test_a_cli_failure_exits_2_with_swa_deploy_failed(
    commands: FakeCommands, dist: Path, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    commands.cli_status = 1

    assert deploy_web.main(["--env", "dev", "--dist", str(dist)]) == 2

    captured = capsys.readouterr()
    failure = json.loads(captured.err.strip())
    assert failure == {
        "code": "swa_deploy_failed",
        "message": (
            "The web upload or its post-upload check failed; the previous web build still serves."
        ),
        "detail": "swa deploy exited 1",
    }
    assert captured.out.count(TOKEN) == 1  # the mask line only
    assert json.dumps(failure) in _summary(tmp_path)
    assert not commands.ran("az staticwebapp show")


def test_an_invalid_value_stops_the_job_before_the_upload(
    commands: FakeCommands, dist: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    commands.generator_status = 1

    assert deploy_web.main(["--env", "dev", "--dist", str(dist)]) == 2

    failure = _failure(capsys)
    assert failure["code"] == "swa_deploy_failed"
    assert failure["detail"] == "write-env-config.mjs: API_SCOPE is not an api:// URI"
    assert not commands.ran("az staticwebapp")
    assert not commands.ran("npx")


def test_a_generator_that_writes_nothing_stops_the_job(
    commands: FakeCommands, dist: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    commands.generator_writes = False

    assert deploy_web.main(["--env", "dev", "--dist", str(dist)]) == 2

    assert "config.json, staticwebapp.config.json" in _failure(capsys)["detail"]
    assert not commands.ran("npx")


def test_missing_outputs_stop_before_the_token_is_read(
    commands: FakeCommands, dist: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    commands.values.pop("API_SCOPE")

    assert deploy_web.main(["--env", "dev", "--dist", str(dist)]) == 2

    failure = _failure(capsys)
    assert failure["code"] == "swa_deploy_failed"
    assert failure["detail"] == "azd outputs lack API_SCOPE"
    assert not commands.ran("az staticwebapp")


@pytest.mark.parametrize(
    ("status", "stdout"), [(1, ""), (0, "not json"), (0, "[]")], ids=["fails", "text", "array"]
)
def test_unreadable_azd_values_stop_the_job(
    commands: FakeCommands,
    dist: Path,
    capsys: pytest.CaptureFixture[str],
    status: int,
    stdout: str,
) -> None:
    commands.azd_status = status
    commands.azd_stdout = stdout

    assert deploy_web.main(["--env", "dev", "--dist", str(dist)]) == 2

    assert _failure(capsys)["code"] == "swa_deploy_failed"
    assert not commands.ran("az staticwebapp")


def test_a_directory_without_a_web_build_is_refused(
    commands: FakeCommands, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()

    assert deploy_web.main(["--env", "dev", "--dist", str(empty)]) == 2

    assert _failure(capsys)["code"] == "swa_deploy_failed"
    assert commands.calls == []


# --- T-463: the post-upload config.json check ----------------------------------------------------


def test_t463_the_check_reads_config_json_from_the_static_web_app(
    commands: FakeCommands, dist: Path, site: FakeSite, sleeps: list[float]
) -> None:
    assert deploy_web.main(["--env", "dev", "--dist", str(dist)]) == 0

    assert site.urls == [f"https://{HOST}/config.json"]
    assert sleeps == []


@pytest.mark.parametrize(
    "answer",
    [
        lambda: _answer(status=404),
        lambda: _answer(status=302),
        lambda: _answer(
            body={**CONFIG, "apiBaseUrl": "https://app-docchat-api-prod-x.azurewebsites.net"}
        ),
        lambda: _answer(body={**CONFIG, "tenantId": WEB_CLIENT_ID}),
        lambda: _answer(body={**CONFIG, "debug": True}),
        lambda: _answer(body={key: CONFIG[key] for key in ("tenantId", "clientId", "apiBaseUrl")}),
        lambda: _answer(body=dict(reversed(CONFIG.items()))),
        lambda: _answer(body=["not", "an", "object"]),
        lambda: deploy_web.HttpAnswer(200, {"Content-Security-Policy": CSP}, b"<!doctype html>"),
        lambda: _answer(
            csp="default-src 'none'; connect-src 'self' https://login.microsoftonline.com"
        ),
        lambda: _answer(csp=f"connect-src 'self' {API_BASE_URL}.evil.example"),
        lambda: _answer(csp=f"connect-src 'self'; img-src {API_BASE_URL}"),
        lambda: deploy_web.HttpAnswer(200, {}, json.dumps(CONFIG).encode()),
    ],
    ids=[
        "not-found",
        "redirect",
        "other-api",
        "other-tenant",
        "extra-member",
        "missing-member",
        "member-order",
        "not-an-object",
        "not-json",
        "csp-without-api",
        "csp-lookalike",
        "api-outside-connect-src",
        "no-csp",
    ],
)
def test_t463_a_failed_config_check_exits_2_with_web_config_check_failed(
    *,
    monkeypatch: pytest.MonkeyPatch,
    commands: FakeCommands,
    dist: Path,
    capsys: pytest.CaptureFixture[str],
    sleeps: list[float],
    answer: Callable[[], Any],
) -> None:
    fake = FakeSite(answer)
    monkeypatch.setattr(deploy_web, "http_get", fake)

    assert deploy_web.main(["--env", "dev", "--dist", str(dist)]) == 2

    assert _failure(capsys)["code"] == "web_config_check_failed"
    assert len(fake.urls) == deploy_web.CONFIG_CHECK_ATTEMPTS
    assert sleeps == [deploy_web.CONFIG_CHECK_INTERVAL_SECONDS] * (len(fake.urls) - 1)


def test_t463_the_check_waits_for_the_new_build_to_serve(
    monkeypatch: pytest.MonkeyPatch, commands: FakeCommands, dist: Path, sleeps: list[float]
) -> None:
    def unreachable() -> Any:
        raise TimeoutError

    earlier = {**CONFIG, "apiBaseUrl": "https://app-docchat-api-dev-old000.azurewebsites.net"}
    fake = FakeSite(unreachable, lambda: _answer(body=earlier), _answer)
    monkeypatch.setattr(deploy_web, "http_get", fake)

    assert deploy_web.main(["--env", "dev", "--dist", str(dist)]) == 0

    assert len(fake.urls) == 3
    assert sleeps == [10.0, 10.0]


def test_t463_an_unreachable_site_fails_the_check(
    monkeypatch: pytest.MonkeyPatch,
    commands: FakeCommands,
    dist: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def refused(url: str) -> Any:
        raise ConnectionRefusedError(url)

    monkeypatch.setattr(deploy_web, "http_get", refused)

    assert deploy_web.main(["--env", "dev", "--dist", str(dist)]) == 2

    failure = _failure(capsys)
    assert failure["code"] == "web_config_check_failed"
    assert failure["detail"] == "GET /config.json failed: ConnectionRefusedError"


def test_an_unknown_host_name_fails_the_check(
    commands: FakeCommands, dist: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    commands.host = ""

    assert deploy_web.main(["--env", "dev", "--dist", str(dist)]) == 2

    assert _failure(capsys)["code"] == "web_config_check_failed"


# --- the HTTP seam ------------------------------------------------------------------------------


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path == "/moved":
            self.send_response(302)
            self.send_header("Location", "/config.json")
            self.end_headers()
            return
        body = json.dumps(CONFIG).encode()
        self.send_response(200)
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    @override
    def log_message(self, format: str, *args: Any) -> None:
        del format, args


@pytest.fixture
def server() -> Iterator[str]:
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join()


def test_http_get_reads_the_answer_and_never_follows_a_redirect(server: str) -> None:
    answer = deploy_web.http_get(f"{server}/config.json")
    assert answer.status == 200
    assert json.loads(answer.body) == CONFIG
    assert answer.headers["Content-Security-Policy"] == CSP

    moved = deploy_web.http_get(f"{server}/moved")
    assert moved.status == 302
    assert moved.body == b""
