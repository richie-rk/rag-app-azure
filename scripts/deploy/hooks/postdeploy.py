"""The azd `postdeploy` hook: image and health verification, then the release tag (section 9.3).

The deploy job runs `azd deploy api` and then `azd deploy ingestion` (section 10.7 step 7), and
azd runs this project hook after each of them. The site azd changed last is the one this run
follows, read from the sites' `lastModifiedTimeUtc`:

- after `azd deploy api`: the API site must run `DOCKER|<DOCCHAT_API_IMAGE>` and its
  `HealthCheckStatus` metric must read 100 within 10 minutes;
- after `azd deploy ingestion`: both sites must run their release references, an
  `ingestion_worker_started` event of this release must appear within 10 minutes, and then the
  API site is tagged `docchat-release-sha=<DOCCHAT_RELEASE_SHA>`.

Any failure exits 2 and the tag isn't written.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

ENVIRONMENTS = ("dev", "prod", "load")
POLL_SECONDS = 30.0
WAIT_LIMIT_SECONDS = 600.0
HEALTHY = 100.0
COMMIT = re.compile(r"^[0-9a-f]{40}$")
WORKSPACE_ID = re.compile(r"^[0-9a-fA-F-]{36}$")
LOGS_QUERY_URL = "https://api.loganalytics.io/v1/workspaces/{workspace}/query"
COMMAND_TIMEOUT_SECONDS = 300.0
INGESTION_STARTED_QUERY = """AppEvents
| where TimeGenerated > datetime({since})
    and AppRoleName == 'docchat-ingestion'
    and Name == 'ingestion_worker_started'
    and AppVersion == '{release}'
| take 1"""

MESSAGES = {
    "deploy_verification_failed": (
        "The deployed image didn't match, or the app didn't become healthy within 10 minutes."
    ),
    "health_check_failed": (
        "The deployed image didn't match, or the app didn't become healthy within 10 minutes."
    ),
    "ingestion_start_failed": (
        "No ingestion worker reported starting on this release within 10 minutes."
    ),
}


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


class HookError(Exception):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class Site:
    service: str
    resource_id: str
    modified: datetime


# --- seams to the outside world -------------------------------------------------------------


def run_command(args: Sequence[str]) -> CommandResult:
    executable = shutil.which(args[0]) or args[0]
    try:
        # Fixed command words plus values this hook validated; no shell is involved.
        completed = subprocess.run(  # noqa: S603
            [executable, *args[1:]],
            capture_output=True,
            text=True,
            check=False,
            timeout=COMMAND_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return CommandResult(127, "", f"{args[0]}: {type(error).__name__}")
    return CommandResult(completed.returncode, completed.stdout, completed.stderr)


def sleep(seconds: float) -> None:
    time.sleep(seconds)


def monotonic() -> float:
    return time.monotonic()


def now() -> datetime:
    return datetime.now(UTC)


# --- helpers --------------------------------------------------------------------------------


def report_failure(code: str, detail: str) -> None:
    line = json.dumps({"code": code, "message": MESSAGES[code], "detail": detail})
    sys.stderr.write(line + "\n")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        # The exit code carries the failure; a summary that can't be written mustn't mask it.
        with contextlib.suppress(OSError), Path(summary).open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")


def az(code: str, *args: str) -> str:
    result = run_command(["az", *args])
    if result.returncode != 0:
        last = (result.stderr.strip().splitlines() or ["no error output"])[-1][:300]
        raise HookError(code, f"az {' '.join(args[:3])} failed: {last}")
    return result.stdout


def az_json(code: str, *args: str) -> Any:
    output = az(code, *args, "--output", "json")
    try:
        return json.loads(output) if output.strip() else None
    except json.JSONDecodeError as error:
        raise HookError(code, f"az {' '.join(args[:3])} answered no JSON") from error


def parse_utc(text: str) -> datetime:
    """App Service writes `2026-10-30T12:34:56.1234567`, UTC without a zone and with 7 digits."""
    return datetime.fromisoformat(text.strip().strip('"')[:19]).replace(tzinfo=UTC)


def iso(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def poll(check: Callable[[], bool]) -> bool:
    """Every 30 s, for at most 10 minutes."""
    started = monotonic()
    while True:
        if check():
            return True
        if monotonic() - started + POLL_SECONDS > WAIT_LIMIT_SECONDS:
            return False
        sleep(POLL_SECONDS)


# --- the steps ------------------------------------------------------------------------------


def find_site(env: str, service: str) -> Site:
    code = "deploy_verification_failed"
    sites = az_json(
        code, "resource", "list", "--resource-group", f"rg-docchat-{env}",
        "--tag", f"azd-service-name={service}", "--query", "[].id",
    )  # fmt: skip
    if not sites:
        raise HookError(code, f"no site tagged azd-service-name={service}")
    resource_id = str(sites[0])
    modified = az(
        code, "resource", "show", "--ids", resource_id,
        "--query", "properties.lastModifiedTimeUtc", "--output", "tsv",
    )  # fmt: skip
    try:
        return Site(service, resource_id, parse_utc(modified))
    except ValueError as error:
        raise HookError(code, f"the {service} site has no lastModifiedTimeUtc") from error


def verify_image(site: Site, expected: str) -> None:
    code = "deploy_verification_failed"
    live = az(
        code, "webapp", "config", "show", "--ids", site.resource_id,
        "--query", "linuxFxVersion", "--output", "tsv",
    ).strip()  # fmt: skip
    if live != f"DOCKER|{expected}":
        raise HookError(code, f"the {site.service} site runs {live[:200]!r}, not the release")


def health_reads_100(site: Site) -> bool:
    data = az_json(
        "health_check_failed", "monitor", "metrics", "list", "--resource", site.resource_id,
        "--metric", "HealthCheckStatus", "--aggregation", "Average", "--interval", "PT1M",
        "--start-time", iso(site.modified), "--end-time", iso(now()),
    )  # fmt: skip
    points = [
        point
        for metric in (data or {}).get("value", [])
        for series in metric.get("timeseries", [])
        for point in series.get("data", [])
        if point.get("average") is not None
    ]
    return bool(points) and float(points[-1]["average"]) == HEALTHY


def wait_healthy(site: Site) -> None:
    if not poll(lambda: health_reads_100(site)):
        raise HookError("health_check_failed", "HealthCheckStatus didn't read 100 in 10 minutes")


def workspace_id(env: str) -> str:
    value = os.environ.get("DOCCHAT_LOG_ANALYTICS_WORKSPACE_ID", "")
    if not value:
        # An images-only run provisions nothing, so its azd environment holds no outputs.
        value = az(
            "ingestion_start_failed", "resource", "show", "--resource-group", f"rg-docchat-{env}",
            "--name", f"log-docchat-{env}",
            "--resource-type", "Microsoft.OperationalInsights/workspaces",
            "--query", "properties.customerId", "--output", "tsv",
        ).strip()  # fmt: skip
    if not WORKSPACE_ID.fullmatch(value):
        raise HookError("ingestion_start_failed", "the workspace customer ID isn't a GUID")
    return value


def ingestion_started(workspace: str, since: datetime, release: str) -> bool:
    query = INGESTION_STARTED_QUERY.format(since=iso(since), release=release)
    with tempfile.TemporaryDirectory() as directory:
        body = Path(directory) / "query.json"
        body.write_text(json.dumps({"query": query, "timespan": "PT1H"}), encoding="utf-8")
        data = az_json(
            "ingestion_start_failed", "rest", "--method", "post",
            "--uri", LOGS_QUERY_URL.format(workspace=workspace),
            "--resource", "https://api.loganalytics.io",
            "--headers", "Content-Type=application/json", "--body", f"@{body}",
        )  # fmt: skip
    tables = (data or {}).get("tables", [])
    return any(table.get("rows") for table in tables)


def wait_for_ingestion(workspace: str, site: Site, release: str) -> None:
    if not poll(lambda: ingestion_started(workspace, site.modified, release)):
        raise HookError(
            "ingestion_start_failed", "no ingestion_worker_started of this release in 10 minutes"
        )


def tag_release(site: Site, release: str) -> None:
    az(
        "deploy_verification_failed", "tag", "update", "--resource-id", site.resource_id,
        "--operation", "Merge", "--tags", f"docchat-release-sha={release}", "--output", "none",
    )  # fmt: skip


def verify(env: str) -> str:
    if env not in ENVIRONMENTS:
        raise HookError("deploy_verification_failed", "AZURE_ENV_NAME isn't dev, prod or load")
    release = os.environ.get("DOCCHAT_RELEASE_SHA", "")
    if not COMMIT.fullmatch(release):
        raise HookError(
            "deploy_verification_failed", "DOCCHAT_RELEASE_SHA isn't a 40-hexadecimal commit"
        )
    api = find_site(env, "api")
    ingestion = find_site(env, "ingestion")
    verify_image(api, os.environ.get("DOCCHAT_API_IMAGE", ""))
    if ingestion.modified <= api.modified:
        # This run follows `azd deploy api`; `azd deploy ingestion` comes next.
        wait_healthy(api)
        return "api verified; ingestion pending"
    verify_image(ingestion, os.environ.get("DOCCHAT_INGESTION_IMAGE", ""))
    wait_for_ingestion(workspace_id(env), ingestion, release)
    tag_release(api, release)
    return f"release {release} verified and tagged"


def main() -> int:
    env = os.environ.get("AZURE_ENV_NAME", "")
    try:
        outcome = verify(env)
    except HookError as error:
        report_failure(error.code, error.detail)
        return 2
    sys.stdout.write(json.dumps({"hook": "postdeploy", "env": env, "outcome": outcome}) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
