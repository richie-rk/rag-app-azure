"""Web deployment with the pinned Static Web Apps CLI (`83-infrastructure.md` section 10.10).

    uv run --frozen python scripts/deploy/deploy_web.py --env <env> --dist <dir> \
        [--swa-cli <archive or installed CLI folder>]

1. copies the environment-free `web-dist` build to a fresh directory;
2. runs `node apps/web/scripts/write-env-config.mjs --dist <dir>` with exactly `AZURE_TENANT_ID`,
   `WEB_CLIENT_ID`, `API_BASE_URL` and `API_SCOPE` from `azd env get-values`, which writes
   `config.json` and `staticwebapp.config.json`;
3. reads the deployment token, masks it at once and keeps it only in the CLI's environment;
4. runs `npx --no-install swa deploy` from the pinned CLI (the `swa-cli` archive, or a folder after
   the operator's own `npm ci --ignore-scripts`, by default `scripts/deploy/swa-cli`) with a child
   environment of exactly `PATH`, `HOME`, `SWA_CLI_DEPLOYMENT_TOKEN` and
   `SWA_CLI_DEPLOY_BINARY_VERSION=stable`, and writes the deploy binary's build ID and SHA-256
   checksum to the job summary;
5. checks that `/config.json` answers `200` with exactly the four values of the environment's
   outputs (T-463) and that the `Content-Security-Policy`'s `connect-src` names the API origin.

A failure before or during the upload exits 2 with `swa_deploy_failed`, a failed step 5 with
`web_config_check_failed`; the previous build keeps serving until an upload succeeds.
"""

from __future__ import annotations

import argparse
import contextlib
import http.client
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import IO, TYPE_CHECKING, Any, override

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from email.message import Message

REPO_ROOT = Path(__file__).resolve().parents[2]
ENVIRONMENTS = ("dev", "prod", "load")
GENERATOR = Path("apps") / "web" / "scripts" / "write-env-config.mjs"
GENERATED_FILES = ("config.json", "staticwebapp.config.json")
SWA_CLI_DIR = REPO_ROOT / "scripts" / "deploy" / "swa-cli"
CLI_PACKAGE = "@azure/static-web-apps-cli"
GENERATOR_INPUTS = ("AZURE_TENANT_ID", "WEB_CLIENT_ID", "API_BASE_URL", "API_SCOPE")
# `config.json`'s members in their fixed order (`10-web-client.md` section 2.1) and the output each
# one is generated from.
CONFIG_MEMBERS = (
    ("tenantId", "AZURE_TENANT_ID"),
    ("clientId", "WEB_CLIENT_ID"),
    ("apiBaseUrl", "API_BASE_URL"),
    ("apiScope", "API_SCOPE"),
)
# Where the CLI records the deploy binary it downloaded: `metadata.buildId` and `checksum`.
DEPLOY_METADATA = Path(".swa") / "deploy" / "StaticSitesClient.json"
COMMAND_TIMEOUT_SECONDS = 1800.0
HTTP_TIMEOUT_SECONDS = 30.0
# The upload has finished when the CLI exits, but the site's edges may serve the earlier build for
# a short while, so the check gets about a minute before it fails.
CONFIG_CHECK_ATTEMPTS = 6
CONFIG_CHECK_INTERVAL_SECONDS = 10.0
EXACT_VERSION = re.compile(r"\d+\.\d+\.\d+")
CHECKSUM = re.compile(r"checksum\W{0,3}([0-9a-fA-F]{64})\b", re.IGNORECASE)
CHECKSUM_VALUE = re.compile(r"[0-9a-fA-F]{64}")
BUILD_ID = re.compile(r"StaticSitesClient(?:\.exe)?@([0-9][A-Za-z0-9._-]*)")
BUILD_ID_VALUE = re.compile(r"[A-Za-z0-9._-]{1,64}")

MESSAGES = {
    "swa_deploy_failed": (
        "The web upload or its post-upload check failed; the previous web build still serves."
    ),
    "web_config_check_failed": (
        "The web upload or its post-upload check failed; the previous web build still serves."
    ),
}


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


@dataclass(frozen=True)
class HttpAnswer:
    status: int
    headers: Mapping[str, str]
    body: bytes


class DeployError(Exception):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


# --- seams to the outside world -------------------------------------------------------------


def run_command(
    args: Sequence[str], *, env: Mapping[str, str] | None = None, cwd: Path | None = None
) -> CommandResult:
    executable = shutil.which(args[0], path=(env or os.environ).get("PATH")) or args[0]
    try:
        # Fixed command words and paths; no shell. `env`, when given, is the whole environment.
        completed = subprocess.run(  # noqa: S603
            [executable, *args[1:]],
            capture_output=True,
            text=True,
            check=False,
            timeout=COMMAND_TIMEOUT_SECONDS,
            env=dict(env) if env is not None else None,
            cwd=cwd or REPO_ROOT,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return CommandResult(127, "", f"{args[0]}: {type(error).__name__}")
    return CommandResult(completed.returncode, completed.stdout, completed.stderr)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """The site serves `/config.json` itself; a redirect answers with its own status."""

    @override
    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: Message,
        newurl: str,
    ) -> urllib.request.Request | None:
        return None


def http_get(url: str) -> HttpAnswer:
    # The URL is https://<the Static Web App's default host name>/config.json.
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(url, timeout=HTTP_TIMEOUT_SECONDS) as response:
            return HttpAnswer(int(response.status), dict(response.headers), response.read())
    except urllib.error.HTTPError as error:
        return HttpAnswer(int(error.code), dict(error.headers), b"")


def sleep(seconds: float) -> None:
    time.sleep(seconds)


# --- helpers --------------------------------------------------------------------------------


def write_out(text: str) -> None:
    sys.stdout.write(text + "\n")


def append_summary(lines: list[str]) -> None:
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        # The exit code carries the result; a summary that can't be written mustn't mask it.
        with contextlib.suppress(OSError), Path(summary).open("a", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")


def report_failure(code: str, detail: str) -> None:
    line = json.dumps({"code": code, "message": MESSAGES[code], "detail": detail})
    sys.stderr.write(line + "\n")
    append_summary([line])


def last_line(text: str) -> str:
    return (text.strip().splitlines() or ["no error output"])[-1][:300]


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


# --- the steps ------------------------------------------------------------------------------


def copy_dist(dist: Path, work: Path) -> Path:
    if not (dist / "index.html").is_file():
        raise DeployError("swa_deploy_failed", f"{dist} holds no web build")
    site = work / "site"
    shutil.copytree(dist, site)
    return site


def azd_values(env: str) -> dict[str, str]:
    result = run_command(["azd", "env", "get-values", "--environment", env, "--output", "json"])
    if result.returncode != 0:
        raise DeployError("swa_deploy_failed", f"azd env get-values: {last_line(result.stderr)}")
    try:
        values = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise DeployError("swa_deploy_failed", "azd env get-values answered no JSON") from error
    if not isinstance(values, dict):
        raise DeployError("swa_deploy_failed", "azd env get-values answered no JSON object")
    return {str(key): str(value) for key, value in values.items()}


def static_site(env: str, values: dict[str, str]) -> str:
    name = values.get("DOCCHAT_STATIC_WEB_APP_NAME", "")
    if name != f"stapp-docchat-{env}":
        raise DeployError(
            "swa_deploy_failed", f"DOCCHAT_STATIC_WEB_APP_NAME isn't stapp-docchat-{env}"
        )
    return name


def write_config(site: Path, values: dict[str, str]) -> None:
    missing = [name for name in GENERATOR_INPUTS if not values.get(name)]
    if missing:
        raise DeployError("swa_deploy_failed", "azd outputs lack " + ", ".join(missing))
    # Exactly the four values and nothing else, so no other variable reaches the generator.
    inputs = {name: values[name] for name in GENERATOR_INPUTS}
    node = shutil.which("node") or "node"
    result = run_command([node, str(GENERATOR), "--dist", str(site)], env=inputs)
    if result.returncode != 0:
        raise DeployError("swa_deploy_failed", f"write-env-config.mjs: {last_line(result.stderr)}")
    absent = [name for name in GENERATED_FILES if not (site / name).is_file()]
    if absent:
        raise DeployError("swa_deploy_failed", "write-env-config.mjs wrote no " + ", ".join(absent))


def cli_directory(archive: Path | None, work: Path) -> Path:
    """The pinned CLI: the `swa-cli` archive of the build job, or a local `npm ci` folder."""
    if archive is None:
        return check_cli(SWA_CLI_DIR)
    if archive.is_dir():
        # An operator's own folder after `npm ci --ignore-scripts` (ADR 0121 item 3).
        return check_cli(archive)
    target = work / "swa-cli"
    target.mkdir()
    try:
        with tarfile.open(archive, "r:gz") as bundle:
            bundle.extractall(target, filter="data")
    except (OSError, tarfile.TarError) as error:
        raise DeployError("swa_deploy_failed", f"{archive.name} can't be extracted") from error
    if (target / "package.json").is_file():
        return check_cli(target)
    nested = [path.parent for path in target.glob("*/package.json")]
    if len(nested) != 1:
        raise DeployError("swa_deploy_failed", f"{archive.name} holds no single CLI package")
    return check_cli(nested[0])


def check_cli(cli: Path) -> Path:
    """Only the exact version the CLI's package pins may receive the token."""
    manifest = read_json(cli / "package.json")
    dependencies = manifest.get("dependencies") if isinstance(manifest, dict) else None
    pinned = dependencies.get(CLI_PACKAGE) if isinstance(dependencies, dict) else None
    if not isinstance(pinned, str) or not EXACT_VERSION.fullmatch(pinned):
        raise DeployError(
            "swa_deploy_failed", f"{cli.name}/package.json pins no exact {CLI_PACKAGE}"
        )
    installed = read_json(cli / "node_modules" / CLI_PACKAGE / "package.json")
    version = installed.get("version") if isinstance(installed, dict) else None
    if version != pinned:
        raise DeployError(
            "swa_deploy_failed",
            f"{CLI_PACKAGE} {pinned} isn't installed in {cli.name}; "
            "run npm ci --ignore-scripts there",
        )
    return cli


def deployment_token(name: str, env: str) -> str:
    result = run_command(
        ["az", "staticwebapp", "secrets", "list", "--name", name,
         "--resource-group", f"rg-docchat-{env}", "--query", "properties.apiKey",
         "--output", "tsv"]
    )  # fmt: skip
    token = result.stdout.strip()
    if result.returncode != 0 or not token:
        raise DeployError("swa_deploy_failed", "the deployment token couldn't be read")
    if os.environ.get("GITHUB_ACTIONS"):
        # Masked before anything else runs, so no later log line can show it.
        write_out(f"::add-mask::{token}")
    return token


def deploy(site: Path, cli: Path, token: str) -> str:
    # Never the GitHub token-request variables, so the CLI and the binary it downloads can't mint
    # a token for an Azure identity.
    child = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        "SWA_CLI_DEPLOYMENT_TOKEN": token,
        "SWA_CLI_DEPLOY_BINARY_VERSION": "stable",
    }
    result = run_command(
        ["npx", "--no-install", "swa", "deploy", str(site), "--env", "production",
         "--no-use-keychain"],
        env=child,
        cwd=cli,
    )  # fmt: skip
    output = (result.stdout + "\n" + result.stderr).replace(token, "***")
    write_out(output.strip())
    if result.returncode != 0:
        raise DeployError("swa_deploy_failed", f"swa deploy exited {result.returncode}")
    return output


def deploy_binary(output: str, home: str) -> tuple[str | None, str | None]:
    """The build ID and SHA-256 of the binary the CLI ran, from its metadata file or its output."""
    build: str | None = None
    checksum: str | None = None
    saved = read_json(Path(home) / DEPLOY_METADATA) if home else None
    if isinstance(saved, dict):
        metadata = saved.get("metadata")
        value = metadata.get("buildId") if isinstance(metadata, dict) else None
        if isinstance(value, str) and BUILD_ID_VALUE.fullmatch(value):
            build = value
        value = saved.get("checksum")
        if isinstance(value, str) and CHECKSUM_VALUE.fullmatch(value):
            checksum = value.lower()
    if build is None and (found := BUILD_ID.search(output)):
        build = found.group(1)
    if checksum is None and (found := CHECKSUM.search(output)):
        checksum = found.group(1).lower()
    return build, checksum


def record_binary(output: str) -> None:
    build, checksum = deploy_binary(output, os.environ.get("HOME", ""))
    append_summary(
        [
            "### Static Web Apps deploy binary",
            "",
            f"- build ID: {build or 'not reported'}",
            f"- SHA-256: {checksum or 'not reported'}",
        ]
    )


def default_host(name: str, env: str) -> str:
    result = run_command(
        ["az", "staticwebapp", "show", "--name", name, "--resource-group", f"rg-docchat-{env}",
         "--query", "defaultHostname", "--output", "tsv"]
    )  # fmt: skip
    host = result.stdout.strip()
    if result.returncode != 0 or not re.fullmatch(r"[a-z0-9.-]+", host):
        raise DeployError("web_config_check_failed", "the Static Web App's host name is unknown")
    return host


def connect_sources(policy: str) -> list[str]:
    for directive in policy.split(";"):
        words = directive.split()
        if words and words[0].lower() == "connect-src":
            return words[1:]
    return []


def members_problem(body: bytes, expected: dict[str, str]) -> str | None:
    try:
        config: Any = json.loads(body)
    except ValueError:
        return "/config.json isn't JSON"
    if not isinstance(config, dict) or list(config) != list(expected):
        return "/config.json's members aren't exactly " + ", ".join(expected)
    differing = [member for member, value in expected.items() if config[member] != value]
    if differing:
        return "/config.json differs from the environment's outputs in " + ", ".join(differing)
    return None


def config_problem(answer: HttpAnswer, expected: dict[str, str], origin: str) -> str | None:
    if answer.status != 200:  # noqa: PLR2004
        return f"/config.json answered {answer.status}"
    problem = members_problem(answer.body, expected)
    if problem:
        return problem
    policy = next(
        (
            value
            for key, value in answer.headers.items()
            if key.lower() == "content-security-policy"
        ),
        "",
    )
    if origin not in connect_sources(policy):
        return "the Content-Security-Policy's connect-src doesn't name the API origin"
    return None


def check_config(host: str, values: dict[str, str]) -> None:
    expected = {member: values[output] for member, output in CONFIG_MEMBERS}
    parts = urllib.parse.urlsplit(values["API_BASE_URL"])
    origin = f"{parts.scheme}://{parts.netloc}"
    url = f"https://{host}/config.json"
    problem = "/config.json wasn't read"
    for attempt in range(CONFIG_CHECK_ATTEMPTS):
        if attempt:
            sleep(CONFIG_CHECK_INTERVAL_SECONDS)
        try:
            answer = http_get(url)
        except (OSError, http.client.HTTPException) as error:
            problem = f"GET /config.json failed: {type(error).__name__}"
            continue
        found = config_problem(answer, expected, origin)
        if found is None:
            return
        problem = found
    raise DeployError("web_config_check_failed", problem)


def run(env: str, dist: Path, archive: Path | None) -> None:
    with tempfile.TemporaryDirectory(prefix="docchat-web-") as directory:
        work = Path(directory)
        site = copy_dist(dist, work)
        values = azd_values(env)
        name = static_site(env, values)
        write_config(site, values)
        # The CLI is checked before the token is read, so a broken archive costs no token read.
        cli = cli_directory(archive, work)
        token = deployment_token(name, env)
        output = deploy(site, cli, token)
        record_binary(output)
    check_config(default_host(name, env), values)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="deploy_web.py", description="Deploy the web client.")
    parser.add_argument("--env", required=True, choices=ENVIRONMENTS)
    parser.add_argument("--dist", required=True, type=Path)
    parser.add_argument("--swa-cli", type=Path, default=None)
    args = parser.parse_args(argv)
    try:
        run(args.env, args.dist, args.swa_cli)
    except DeployError as error:
        report_failure(error.code, error.detail)
        return 2
    write_out(json.dumps({"web": "deployed", "env": args.env}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
