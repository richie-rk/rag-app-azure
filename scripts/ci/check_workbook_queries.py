"""Run every workbook tile query and alert query against the workspace (85-observability.md 5.11).

Loads every `*.workbook.json` of `infra/modules/monitoring/workbooks/`, substitutes the
placeholders with the deployed environment's values, replaces `{TimeRange}` with `> ago(1h)`, and
runs each KQL tile query through the Logs query API v1 against `log-docchat-<env>`, 30 s timeout per
query; any query error fails the check and names the workbook and the tile. It then runs the KQL of
every log search alert deployed in `rg-docchat-<env>` (section 6.4, read back through Azure
Resource Manager so the Bicep values are already substituted), with the rule's `windowSize` as the
query's time span, so a rule whose query no longer parses is found before it silently never fires.
A query that returns no rows passes: a quiet hour is valid.

The environment's values come from the azd environment file `.azure/<env>/.env`
(83-infrastructure.md section 8.8): `{workspaceId}` the workspace resource ID, `{apiSiteId}` and
`{ingestionSiteId}` the two sites, `{apiPlanId}` `asp-docchat-api-<env>`, `{autoscaleId}`
`autoscale-docchat-api-<env>`, `{budgetId}` `budget-docchat-<env>` and `{apiPrincipalId}` the API
identity's principal ID. The budget tile is an Azure Resource Manager query, not KQL, and is
skipped. The token for `https://api.loganalytics.io` and the alert rules come from the signed-in
Azure CLI (`az account get-access-token`, `az rest`).

Usage: uv run --frozen python scripts/ci/check_workbook_queries.py --env <dev|prod|load>
Exit codes: 0 every query ran; 1 a query failed, or the inputs or the CLI can't be read.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Sequence

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
WORKBOOKS = "infra/modules/monitoring/workbooks"
TIME_RANGE = "{TimeRange}"
TIME_FILTER = "> ago(1h)"
QUERY_TYPE_LOGS = 0
QUERY_ITEM = 3
LOGS_API = "https://api.loganalytics.io/v1/workspaces/{workspace}/query"
LOGS_RESOURCE = "https://api.loganalytics.io"
ARM = "https://management.azure.com"
RULES_API_VERSION = "2026-03-01"
QUERY_TIMEOUT_SECONDS = 30
TRANSIENT_STATUSES = frozenset({429, 500, 502, 503, 504})
QUERY_ATTEMPTS = 3
QUERY_RETRY_SECONDS = 5.0
MIN_QUOTED_LENGTH = 2
REQUIRED_SETTINGS = (
    "AZURE_SUBSCRIPTION_ID",
    "AZURE_RESOURCE_GROUP",
    "DOCCHAT_LOG_ANALYTICS_WORKSPACE_ID",
    "DOCCHAT_LOG_ANALYTICS_RESOURCE_ID",
    "DOCCHAT_API_APP_NAME",
    "DOCCHAT_INGESTION_APP_NAME",
    "DOCCHAT_API_IDENTITY_PRINCIPAL_ID",
)


class CheckInputError(RuntimeError):
    """An input of the check (the azd environment, a workbook, the CLI) can't be read."""


class QueryError(RuntimeError):
    """The Logs query API refused or failed a query."""


@dataclass(frozen=True)
class Query:
    source: str
    name: str
    text: str
    timespan: str | None = None


# --- Inputs --------------------------------------------------------------------------------------


def read_azd_env(path: Path) -> dict[str, str]:
    """Read an azd `.env` file: `NAME="value"` lines, as azd writes them."""
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        name, _, value = stripped.partition("=")
        value = value.strip()
        if len(value) >= MIN_QUOTED_LENGTH and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        values[name.strip()] = value
    return values


def placeholders(settings: dict[str, str], env: str) -> dict[str, str]:
    """The workbook placeholders of 85-observability.md section 5.1 for one environment."""
    missing = [name for name in REQUIRED_SETTINGS if not settings.get(name)]
    if missing:
        raise CheckInputError(f"the azd environment lacks {', '.join(missing)}")
    group = (
        f"/subscriptions/{settings['AZURE_SUBSCRIPTION_ID']}"
        f"/resourceGroups/{settings['AZURE_RESOURCE_GROUP']}/providers"
    )
    return {
        "{workspaceId}": settings["DOCCHAT_LOG_ANALYTICS_RESOURCE_ID"],
        "{apiSiteId}": f"{group}/Microsoft.Web/sites/{settings['DOCCHAT_API_APP_NAME']}",
        "{ingestionSiteId}": (
            f"{group}/Microsoft.Web/sites/{settings['DOCCHAT_INGESTION_APP_NAME']}"
        ),
        "{apiPlanId}": f"{group}/Microsoft.Web/serverfarms/asp-docchat-api-{env}",
        "{autoscaleId}": (
            f"{group}/Microsoft.Insights/autoscalesettings/autoscale-docchat-api-{env}"
        ),
        "{budgetId}": f"{group}/Microsoft.Consumption/budgets/budget-docchat-{env}",
        "{apiPrincipalId}": settings["DOCCHAT_API_IDENTITY_PRINCIPAL_ID"],
    }


def _tiles(items: Any, prefix: str = "") -> Iterator[tuple[str, dict[str, Any]]]:
    """Yield (tile name, content) for every query item, groups included."""
    if not isinstance(items, list):
        return
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        content = item.get("content")
        if not isinstance(content, dict):
            continue
        name = str(item.get("name") or content.get("title") or f"item {index}")
        if item.get("type") == QUERY_ITEM and isinstance(content.get("query"), str):
            yield f"{prefix}{name}", content
        yield from _tiles(content.get("items"), f"{prefix}{name} / ")


def workbook_queries(directory: Path, values: dict[str, str]) -> list[Query]:
    """Every KQL tile query of every workbook, placeholders substituted."""
    files = sorted(directory.glob("*.workbook.json"))
    if not files:
        raise CheckInputError(f"no *.workbook.json in {directory}")
    queries: list[Query] = []
    for path in files:
        try:
            workbook = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as error:
            raise CheckInputError(f"{path.name} doesn't parse: {error}") from error
        for name, content in _tiles(workbook.get("items") if isinstance(workbook, dict) else []):
            if content.get("queryType", QUERY_TYPE_LOGS) != QUERY_TYPE_LOGS:
                continue  # an Azure Resource Manager tile, such as the budget status
            text = content["query"].replace(TIME_RANGE, TIME_FILTER)
            for placeholder, value in values.items():
                text = text.replace(placeholder, value)
            queries.append(Query(path.name, name, text))
    return queries


def alert_queries(rules: Sequence[Any]) -> list[Query]:
    """The query and window of every log search alert rule read back from Resource Manager."""
    queries: list[Query] = []
    for rule in rules:
        properties = rule.get("properties", {}) if isinstance(rule, dict) else {}
        criteria = properties.get("criteria", {}) if isinstance(properties, dict) else {}
        for criterion in criteria.get("allOf", []) if isinstance(criteria, dict) else []:
            if isinstance(criterion, dict) and isinstance(criterion.get("query"), str):
                window = properties.get("windowSize")
                queries.append(
                    Query(
                        "alert",
                        str(rule.get("name", "unnamed rule")),
                        criterion["query"],
                        window if isinstance(window, str) else None,
                    )
                )
    return queries


# --- Azure CLI and the Logs query API ------------------------------------------------------------


def az(*arguments: str) -> str:
    executable = shutil.which("az")
    if executable is None:
        raise CheckInputError("the Azure CLI isn't on PATH")
    # The arguments are fixed subcommands and resource IDs built from the azd environment.
    completed = subprocess.run(  # noqa: S603
        [executable, *arguments], check=False, capture_output=True, text=True
    )
    if completed.returncode != 0:
        raise CheckInputError(
            f"az {arguments[0]} {arguments[1]} failed: {completed.stderr.strip()}"
        )
    return completed.stdout


def get_token() -> str:
    """An access token for the Logs query API from the signed-in Azure CLI."""
    token = az(
        "account",
        "get-access-token",
        "--resource",
        LOGS_RESOURCE,
        "--query",
        "accessToken",
        "--output",
        "json",
    )
    return str(json.loads(token))


def list_alert_rules(subscription: str, group: str) -> list[Any]:
    """Every scheduled query rule in the environment's resource group, all pages."""
    url = (
        f"{ARM}/subscriptions/{subscription}/resourceGroups/{group}"
        f"/providers/Microsoft.Insights/scheduledQueryRules?api-version={RULES_API_VERSION}"
    )
    rules: list[Any] = []
    while url:
        page = json.loads(az("rest", "--method", "get", "--url", url, "--output", "json"))
        rules.extend(page.get("value", []))
        url = page.get("nextLink") or ""
        if url and not url.startswith(f"{ARM}/"):
            raise CheckInputError("an alert-rule page links outside Azure Resource Manager")
    return rules


def run_query(workspace: str, token: str, query: Query) -> None:
    """Run one query; raise QueryError with the API's code and message when it fails."""
    body: dict[str, str] = {"query": query.text}
    if query.timespan:
        body["timespan"] = query.timespan
    request = urllib.request.Request(  # noqa: S310 - the fixed https Logs API base
        LOGS_API.format(workspace=workspace),
        data=json.dumps(body).encode("utf-8"),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    for attempt in range(QUERY_ATTEMPTS):
        if attempt:
            time.sleep(QUERY_RETRY_SECONDS)
        try:
            with urllib.request.urlopen(request, timeout=QUERY_TIMEOUT_SECONDS) as response:  # noqa: S310
                answer = json.load(response)
        except urllib.error.HTTPError as error:
            if error.code in TRANSIENT_STATUSES and attempt + 1 < QUERY_ATTEMPTS:
                continue
            raise QueryError(_api_error(error.read(), f"HTTP {error.code}")) from error
        except (urllib.error.URLError, TimeoutError) as error:
            if attempt + 1 < QUERY_ATTEMPTS:
                continue
            raise QueryError(str(error)) from error
        if isinstance(answer, dict) and answer.get("error"):
            raise QueryError(_api_error(json.dumps(answer).encode("utf-8"), "partial error"))
        return


def _api_error(raw: bytes, fallback: str) -> str:
    try:
        error = json.loads(raw).get("error", {})
    except (json.JSONDecodeError, AttributeError):
        return fallback
    if not isinstance(error, dict):
        return fallback
    inner = error.get("innererror") or {}
    detail = inner.get("message") if isinstance(inner, dict) else None
    message = str(error.get("message") or fallback)
    return f"{error.get('code', 'error')}: {message}" + (f" ({detail})" if detail else "")


# --- The check -----------------------------------------------------------------------------------


def check(queries: Sequence[Query], run: Callable[[Query], None]) -> list[str]:
    """Run every query and return one failure line per query error, naming its place."""
    failures: list[str] = []
    for query in queries:
        try:
            run(query)
        except QueryError as error:
            place = (
                f"alert {query.name}"
                if query.source == "alert"
                else f"{query.source} / {query.name}"
            )
            failures.append(f"{place}: {error}")
    return failures


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run every workbook and alert query.")
    parser.add_argument("--env", required=True, choices=("dev", "prod", "load"))
    parser.add_argument("--root", type=Path, default=REPOSITORY_ROOT)
    parser.add_argument("--azd-env-file", type=Path)
    args = parser.parse_args(argv)
    root: Path = args.root
    env_file: Path = args.azd_env_file or root / ".azure" / args.env / ".env"
    try:
        settings = read_azd_env(env_file)
        values = placeholders(settings, args.env)
        queries = workbook_queries(root / WORKBOOKS, values)
        queries += alert_queries(
            list_alert_rules(settings["AZURE_SUBSCRIPTION_ID"], settings["AZURE_RESOURCE_GROUP"])
        )
        token = get_token()
    except (OSError, CheckInputError, json.JSONDecodeError) as error:
        sys.stdout.write(f"check_workbook_queries: {error}\n")
        return 1
    workspace = settings["DOCCHAT_LOG_ANALYTICS_WORKSPACE_ID"]
    failures = check(queries, lambda query: run_query(workspace, token, query))
    for failure in failures:
        sys.stdout.write(f"::error::{failure}\n")
    summary = f"{len(queries) - len(failures)} of {len(queries)} queries ran without an error\n"
    sys.stdout.write(summary)
    step_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if step_summary:
        with Path(step_summary).open("a", encoding="utf-8") as handle:
            handle.write(f"### Workbook and alert queries ({args.env})\n\n{summary}")
            handle.writelines(f"- {failure}\n" for failure in failures)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
