"""Teardown of the `load` environment (`83-infrastructure.md` sections 7.6 and 10.14).

Runs as `id-docchat-deploy-load` from `load-test.yaml`'s `teardown` job and `nightly.yaml`'s
`load-guard` job:

1. deletes the run's three registrations object by object: each service principal, each
   application (found by `uniqueName`), then every object permanently from Graph's deleted
   items; a `404` counts as deleted, any other failure exits 2 with `graph_delete_failed`
   after the remaining steps;
2. deletes the run identities' AcrPull assignments on the registry;
3. deploys `infra/teardown/empty.bicep` in complete mode into `rg-docchat-load`;
4. repeats step 3 up to 3 times, 5 minutes apart, while resources remain, then exits 2 with
   `teardown_incomplete`;
5. removes the run tags.

`rg-docchat-load` itself stays, with its role and policy assignments.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Sequence

REPO_ROOT = Path(__file__).resolve().parents[2]
GROUP = "rg-docchat-load"
GRAPH = "https://graph.microsoft.com/v1.0"
REGISTRATIONS = ("web", "api", "test")
RUN_IDENTITIES = ("id-docchat-api-load", "id-docchat-ingest-load", "id-docchat-stub-load")
ACR_PULL_ROLE = "7f951dda-4ed3-4680-a7ca-43fe172d538d"
RUN_TAGS = ("docchat-load-run", "docchat-load-created")
EXTRA_DEPLOYMENTS = 3
DEPLOYMENT_WAIT_SECONDS = 300.0
COMMAND_TIMEOUT_SECONDS = 3600.0
# Names that carry the run's resource token, `take(uniqueString(...), 6)` (section 1.4).
TOKEN_NAME = re.compile(
    r"^(?:app-docchat-api|func-docchat-ingest|app-docchat-stub|sql-docchat|cosmos-docchat"
    r"|kv-docchat|srch-docchat|cs-docchat|di-docchat)-load-([a-z0-9]{6})$"
)

MESSAGES = {
    "teardown_incomplete": (
        'Resources or registrations of the load run remain; follow the runbook "Run the load test".'
    ),
    "graph_delete_failed": (
        'Resources or registrations of the load run remain; follow the runbook "Run the load test".'
    ),
}


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


@dataclass
class Outcome:
    graph_left: list[str] = field(default_factory=list)
    resources_left: list[str] = field(default_factory=list)
    deployment_errors: list[str] = field(default_factory=list)
    other: list[str] = field(default_factory=list)


def run_command(args: Sequence[str]) -> CommandResult:
    executable = shutil.which(args[0]) or args[0]
    try:
        # Fixed command words plus identifiers read from Azure and Graph; no shell is involved.
        completed = subprocess.run(  # noqa: S603
            [executable, *args[1:]],
            capture_output=True,
            text=True,
            check=False,
            timeout=COMMAND_TIMEOUT_SECONDS,
            cwd=REPO_ROOT,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return CommandResult(127, "", f"{args[0]}: {type(error).__name__}")
    return CommandResult(completed.returncode, completed.stdout, completed.stderr)


def sleep(seconds: float) -> None:
    time.sleep(seconds)


def last_line(text: str) -> str:
    return (text.strip().splitlines() or ["no error output"])[-1][:300]


def is_not_found(result: CommandResult) -> bool:
    return re.search(r"\bNot Found\b", result.stderr) is not None


def az_json(*args: str) -> Any:
    result = run_command(["az", *args, "--output", "json"])
    if result.returncode != 0:
        raise RuntimeError(f"az {' '.join(args[:3])} failed: {last_line(result.stderr)}")
    return json.loads(result.stdout) if result.stdout.strip() else None


def summary(lines: list[str]) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        # The exit code carries the result; a summary that can't be written mustn't mask it.
        with contextlib.suppress(OSError), Path(path).open("a", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")


# --- step 1: the run's registrations --------------------------------------------------------


def run_tokens() -> list[str]:
    """The run's resource tokens, read from the names of what the run created."""
    tokens: set[str] = set()
    token = os.environ.get("DOCCHAT_RESOURCE_TOKEN", "")
    if re.fullmatch(r"[a-z0-9]{6}", token):
        tokens.add(token)
    names = az_json("resource", "list", "--resource-group", GROUP, "--query", "[].name") or []
    for name in names:
        match = TOKEN_NAME.match(str(name))
        if match:
            tokens.add(match.group(1))
    return sorted(tokens)


def graph_get(url: str) -> dict[str, Any] | None:
    result = run_command(["az", "rest", "--method", "get", "--url", url, "--output", "json"])
    if result.returncode != 0:
        if is_not_found(result):
            return None
        raise RuntimeError(last_line(result.stderr))
    data = json.loads(result.stdout)
    return data if isinstance(data, dict) else None


def graph_delete(url: str) -> None:
    result = run_command(["az", "rest", "--method", "delete", "--url", url])
    # A 404 counts as already deleted (section 7.6).
    if result.returncode != 0 and not is_not_found(result):
        raise RuntimeError(last_line(result.stderr))


def delete_registrations(tokens: list[str], outcome: Outcome) -> None:
    deleted: list[tuple[str, str]] = []
    for token in tokens:
        for role in REGISTRATIONS:
            unique_name = f"docchat-load-{token}-{role}"
            try:
                application = graph_get(f"{GRAPH}/applications(uniqueName='{unique_name}')")
            except RuntimeError as error:
                outcome.graph_left.append(f"application {unique_name}: {error}")
                continue
            if application is None:
                continue
            _delete_registration(unique_name, application, deleted, outcome)
    for label, object_id in deleted:
        try:
            graph_delete(f"{GRAPH}/directory/deletedItems/{object_id}")
        except RuntimeError as error:
            outcome.graph_left.append(f"deleted item {label} {object_id}: {error}")


def _delete_registration(
    unique_name: str,
    application: dict[str, Any],
    deleted: list[tuple[str, str]],
    outcome: Outcome,
) -> None:
    app_id, object_id = str(application.get("appId")), str(application.get("id"))
    # Graph's application delete doesn't state that it removes the service principal.
    try:
        principal = graph_get(f"{GRAPH}/servicePrincipals(appId='{app_id}')")
        if principal is not None:
            graph_delete(f"{GRAPH}/servicePrincipals/{principal['id']}")
            deleted.append((f"service principal of {unique_name}", str(principal["id"])))
    except (RuntimeError, KeyError) as error:
        outcome.graph_left.append(f"service principal of {unique_name}: {error}")
    try:
        graph_delete(f"{GRAPH}/applications/{object_id}")
        deleted.append((f"application {unique_name}", object_id))
    except RuntimeError as error:
        outcome.graph_left.append(f"application {unique_name}: {error}")


# --- steps 2 to 5 ---------------------------------------------------------------------------


def delete_acr_pull(outcome: Outcome) -> None:
    try:
        identities = az_json(
            "identity",
            "list",
            "--resource-group",
            GROUP,
            "--query",
            "[].{name: name, id: principalId}",
        )
        principals = {
            str(item["id"]) for item in identities or [] if item.get("name") in RUN_IDENTITIES
        }
        if not principals:
            return
        assignments = az_json(
            "role", "assignment", "list", "--all", "--fill-principal-name", "false",
            "--query", "[].{id: id, principalId: principalId, role: roleDefinitionId}",
        )  # fmt: skip
        ids = [
            str(item["id"])
            for item in assignments or []
            if item.get("principalId") in principals
            and str(item.get("role", "")).lower().endswith(ACR_PULL_ROLE)
        ]
        if ids:
            az_json("role", "assignment", "delete", "--ids", *ids)
    except (RuntimeError, KeyError) as error:
        outcome.other.append(f"AcrPull assignments: {error}")


def deploy_empty(outcome: Outcome, attempt: int) -> None:
    result = run_command(
        ["az", "deployment", "group", "create", "--resource-group", GROUP, "--mode", "Complete",
         "--name", f"docchat-load-teardown-{attempt}",
         "--template-file", "infra/teardown/empty.bicep", "--output", "none"]
    )  # fmt: skip
    if result.returncode != 0:
        outcome.deployment_errors.append(
            f"complete-mode deployment {attempt}: {last_line(result.stderr)}"
        )


def remaining_resources() -> list[str]:
    names = az_json("resource", "list", "--resource-group", GROUP, "--query", "[].name")
    return [str(name) for name in names or []]


def empty_group(outcome: Outcome) -> None:
    deploy_empty(outcome, 1)
    for attempt in range(1, EXTRA_DEPLOYMENTS + 1):
        left = remaining_resources()
        if not left:
            return
        sleep(DEPLOYMENT_WAIT_SECONDS)
        deploy_empty(outcome, attempt + 1)
    outcome.resources_left = remaining_resources()


def remove_run_tags() -> None:
    group = az_json("group", "show", "--name", GROUP, "--query", "{id: id, tags: tags}")
    tags = (group or {}).get("tags") or {}
    present = [f"{name}={tags[name]}" for name in RUN_TAGS if name in tags]
    if present:
        az_json(
            "tag", "update", "--resource-id", str(group["id"]), "--operation", "Delete",
            "--tags", *present,
        )  # fmt: skip


def report(code: str, outcome: Outcome) -> None:
    left = [*outcome.resources_left, *outcome.graph_left, *outcome.other]
    if outcome.resources_left:
        left += outcome.deployment_errors
    detail = "; ".join(left)[:1000]
    line = json.dumps({"code": code, "message": MESSAGES[code], "detail": detail})
    sys.stderr.write(line + "\n")
    summary(["### Load teardown left these behind", "", *(f"- {item}" for item in left), "", line])


def main() -> int:
    outcome = Outcome()
    try:
        tokens = run_tokens()
    except (RuntimeError, json.JSONDecodeError) as error:
        tokens = []
        outcome.graph_left.append(f"the run's resource token couldn't be read: {error}")
    delete_registrations(tokens, outcome)
    delete_acr_pull(outcome)
    try:
        empty_group(outcome)
    except (RuntimeError, json.JSONDecodeError) as error:
        outcome.resources_left.append(f"az resource list failed: {error}")
    if outcome.resources_left:
        # The run tags stay, so the nightly guard tries again.
        report("teardown_incomplete", outcome)
        return 2
    try:
        remove_run_tags()
    except (RuntimeError, json.JSONDecodeError) as error:
        outcome.other.append(f"run tags: {error}")
    if outcome.graph_left:
        report("graph_delete_failed", outcome)
        return 2
    if outcome.other:
        # An AcrPull assignment or a run tag left behind is part of the run that remains.
        report("teardown_incomplete", outcome)
        return 2
    sys.stdout.write(json.dumps({"teardown": "complete", "tokens": tokens}) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
