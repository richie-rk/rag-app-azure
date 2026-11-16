"""The two dispatch-only modes of `deploy-environment.yaml` (`83-infrastructure.md` section 10.13).

    uv run --frozen python scripts/deploy/ingestion_modes.py restart-ingestion --env <dev|prod>
    uv run --frozen python scripts/deploy/ingestion_modes.py reset-task-hub --env <dev|prod>

Both run as the environment's deploy identity, whose Contributor covers every call, so operators,
who hold only Reader, change the ingestion app only through these modes (ADR 0054).

- `restart-ingestion` (runbook R18 step 2): records the UTC time, finds the function app by its tag
  `azd-service-name=ingestion`, restarts it (`ingestion_restart_failed` when the platform refuses,
  with nothing else changed), then polls the Logs query API every 30 s, for at most 10 minutes, for
  an `ingestion_worker_started` event later than the restart whose `AppVersion` is the API site's
  `docchat-release-sha` tag (`ingestion_start_failed` when none arrives).
- `reset-task-hub` (runbook R33, the last resort): stops the app, deletes through Resource Manager
  every container, queue and table of the host account whose name starts with the hub name
  `docchat<env>` (case-insensitive; the host's own `azure-webjobs-*` containers stay), and starts
  the app. Any failure after the stop leaves the app stopped and fails with
  `task_hub_reset_failed`, so no worker runs against a half-deleted hub; a second run finishes the
  reset.

Each writes its outcome to the job summary; a failure prints one JSON line `{"code", "message",
"detail"}` to standard error (section 10.18) and exits 2.
"""

from __future__ import annotations

import argparse
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

ENVIRONMENTS = ("dev", "prod")
POLL_SECONDS = 30.0
WAIT_LIMIT_SECONDS = 600.0
COMMAND_TIMEOUT_SECONDS = 300.0
COMMIT = re.compile(r"^[0-9a-f]{40}$")
WORKSPACE_ID = re.compile(r"^[0-9a-fA-F-]{36}$")
ARM = "https://management.azure.com"
STORAGE_API = "2025-06-01"
LOGS_QUERY_URL = "https://api.loganalytics.io/v1/workspaces/{workspace}/query"
# The query of section 9.3: the newest worker start of this release after the given time.
INGESTION_STARTED_QUERY = """AppEvents
| where TimeGenerated > datetime({since})
    and AppRoleName == 'docchat-ingestion'
    and Name == 'ingestion_worker_started'
    and AppVersion == '{release}'
| take 1"""
# The host account's three services, each listed and pruned through Resource Manager.
HUB_SERVICES = (
    "blobServices/default/containers",
    "queueServices/default/queues",
    "tableServices/default/tables",
)

MESSAGES = {
    "ingestion_restart_failed": (
        "The ingestion app didn't accept the restart; nothing else changed."
    ),
    "ingestion_start_failed": (
        "No ingestion worker reported starting on this release within 10 minutes."
    ),
    "task_hub_reset_failed": (
        "The task hub's storage wasn't fully deleted; the ingestion app stays stopped until the "
        "reset runs to completion."
    ),
}


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


class ModeError(Exception):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class StartEvent:
    time: datetime
    app_version: str


# --- seams to the outside world -------------------------------------------------------------


def run_command(args: Sequence[str]) -> CommandResult:
    executable = shutil.which(args[0]) or args[0]
    try:
        # Fixed command words plus values this script validated; no shell is involved.
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


def write_summary(lines: Sequence[str]) -> None:
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        # The exit code carries the outcome; a summary that can't be written mustn't mask it.
        with contextlib.suppress(OSError), Path(summary).open("a", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")


def report_failure(code: str, detail: str) -> None:
    line = json.dumps({"code": code, "message": MESSAGES[code], "detail": detail[:500]})
    sys.stderr.write(line + "\n")
    write_summary([line])


def az(code: str, *args: str) -> str:
    result = run_command(["az", *args])
    if result.returncode != 0:
        last = (result.stderr.strip().splitlines() or ["no error output"])[-1][:300]
        raise ModeError(code, f"az {' '.join(args[:3])} failed: {last}")
    return result.stdout


def az_json(code: str, *args: str) -> Any:
    output = az(code, *args, "--output", "json")
    try:
        return json.loads(output) if output.strip() else None
    except json.JSONDecodeError as error:
        raise ModeError(code, f"az {' '.join(args[:3])} answered no JSON") from error


def iso(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def parse_time(text: str) -> datetime:
    """A Logs API timestamp such as `2026-10-31T12:00:05.1234567Z`, read to the microsecond."""
    value = text.strip().removesuffix("Z")
    whole, _, fraction = value.partition(".")
    moment = datetime.fromisoformat(whole).replace(tzinfo=UTC)
    return moment.replace(microsecond=int((fraction + "000000")[:6]) if fraction else 0)


def poll(check: Callable[[], StartEvent | None]) -> StartEvent | None:
    """Every 30 s, for at most 10 minutes."""
    started = monotonic()
    while True:
        found = check()
        if found is not None:
            return found
        if monotonic() - started + POLL_SECONDS > WAIT_LIMIT_SECONDS:
            return None
        sleep(POLL_SECONDS)


def resource_group(env: str) -> str:
    return f"rg-docchat-{env}"


def find_tagged(code: str, env: str, tag: str, query: str) -> str:
    value = az(
        code, "resource", "list", "--resource-group", resource_group(env), "--tag", tag,
        "--query", query, "--output", "tsv",
    ).strip()  # fmt: skip
    if not value:
        raise ModeError(code, f"no resource in {resource_group(env)} tagged {tag}")
    return value


def find_ingestion_app(code: str, env: str) -> str:
    return find_tagged(code, env, "azd-service-name=ingestion", "[0].name")


# --- restart-ingestion ----------------------------------------------------------------------


def release_sha(env: str) -> str:
    code = "ingestion_start_failed"
    release = find_tagged(code, env, "azd-service-name=api", '[0].tags."docchat-release-sha"')
    if not COMMIT.fullmatch(release):
        raise ModeError(
            code, "the API site's docchat-release-sha tag isn't a 40-hexadecimal commit"
        )
    return release


def workspace_id(env: str) -> str:
    value = az(
        "ingestion_start_failed", "resource", "show", "--resource-group", resource_group(env),
        "--name", f"log-docchat-{env}",
        "--resource-type", "Microsoft.OperationalInsights/workspaces",
        "--query", "properties.customerId", "--output", "tsv",
    ).strip()  # fmt: skip
    if not WORKSPACE_ID.fullmatch(value):
        raise ModeError("ingestion_start_failed", "the workspace customer ID isn't a GUID")
    return value


def start_events(workspace: str, since: datetime, release: str) -> list[StartEvent]:
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
    events: list[StartEvent] = []
    for table in (data or {}).get("tables", []):
        names = [column.get("name") for column in table.get("columns", [])]
        if "TimeGenerated" not in names or "AppVersion" not in names:
            continue
        for row in table.get("rows", []):
            record = dict(zip(names, row, strict=False))
            with contextlib.suppress(TypeError, ValueError):
                events.append(
                    StartEvent(parse_time(str(record["TimeGenerated"])), str(record["AppVersion"]))
                )
    return events


def later_start(events: Sequence[StartEvent], since: datetime, release: str) -> StartEvent | None:
    """The first start of this release after the restart; an earlier row never counts."""
    matching = [event for event in events if event.app_version == release and event.time > since]
    return min(matching, key=lambda event: event.time) if matching else None


def restart_ingestion(env: str) -> list[str]:
    restarted_at = now()
    app = find_ingestion_app("ingestion_restart_failed", env)
    az(
        "ingestion_restart_failed", "functionapp", "restart", "--name", app,
        "--resource-group", resource_group(env), "--output", "none",
    )  # fmt: skip
    release = release_sha(env)
    workspace = workspace_id(env)
    event = poll(
        lambda: later_start(start_events(workspace, restarted_at, release), restarted_at, release)
    )
    if event is None:
        detail = f"no ingestion_worker_started of release {release} after {iso(restarted_at)}"
        raise ModeError("ingestion_start_failed", f"{detail} within 10 minutes")
    return [
        f"## Ingestion restarted in {env}",
        "",
        "| Item | Value |",
        "|---|---|",
        f"| Function app | {app} |",
        f"| Restart (UTC) | {iso(restarted_at)} |",
        f"| `ingestion_worker_started` (UTC) | {iso(event.time)} |",
        f"| Release | {release} |",
    ]


# --- reset-task-hub -------------------------------------------------------------------------


def host_account_id(env: str) -> str:
    return find_tagged(
        "task_hub_reset_failed", env, "resource-usage=azure-functions",
        "[?type=='Microsoft.Storage/storageAccounts'] | [0].id",
    )  # fmt: skip


def list_names(account_id: str, service: str) -> list[str]:
    url: str | None = f"{ARM}{account_id}/{service}?api-version={STORAGE_API}"
    names: list[str] = []
    while url:
        page = az_json("task_hub_reset_failed", "rest", "--method", "get", "--url", url) or {}
        names += [str(item["name"]) for item in page.get("value", []) if "name" in item]
        url = page.get("nextLink")
    return names


def hub_names(names: Sequence[str], hub: str) -> list[str]:
    return [name for name in names if name.lower().startswith(hub.lower())]


def reset_task_hub(env: str) -> list[str]:
    code = "task_hub_reset_failed"
    hub = f"docchat{env}"
    app = find_ingestion_app(code, env)
    account_id = host_account_id(env)
    group = resource_group(env)
    az(code, "functionapp", "stop", "--name", app, "--resource-group", group, "--output", "none")
    deleted: list[str] = []
    try:
        for service in HUB_SERVICES:
            for name in hub_names(list_names(account_id, service), hub):
                az(
                    code, "rest", "--method", "delete",
                    "--url", f"{ARM}{account_id}/{service}/{name}?api-version={STORAGE_API}",
                )  # fmt: skip
                deleted.append(f"{service.rsplit('/', 1)[-1]}/{name}")
    except ModeError as error:
        # The app stays stopped: no worker may run against a half-deleted hub.
        done = ", ".join(deleted) or "nothing"
        raise ModeError(code, f"{error.detail}; deleted before the failure: {done}") from error
    az(code, "functionapp", "start", "--name", app, "--resource-group", group, "--output", "none")
    lines = [
        f"## Task hub {hub} reset in {env}",
        "",
        f"Function app `{app}` stopped, the hub's storage deleted, and the app started.",
        "",
        "| Deleted |",
        "|---|",
    ]
    return lines + [f"| {name} |" for name in deleted or ["nothing: the hub held no storage"]]


MODES: dict[str, Callable[[str], list[str]]] = {
    "restart-ingestion": restart_ingestion,
    "reset-task-hub": reset_task_hub,
}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ingestion_modes.py", description=__doc__)
    parser.add_argument("mode", choices=sorted(MODES))
    parser.add_argument("--env", required=True, choices=ENVIRONMENTS)
    args = parser.parse_args(argv)
    try:
        lines = MODES[args.mode](args.env)
    except ModeError as error:
        report_failure(error.code, error.detail)
        return 2
    write_summary(lines)
    sys.stdout.write(
        json.dumps({"mode": args.mode, "env": args.env, "outcome": "succeeded"}) + "\n"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
