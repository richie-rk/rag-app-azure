"""The Log Analytics query API client of the deployed and load suites, and the chat telemetry
check (`84-testing.md` sections 1.4, 6.3 and 10.1; T-282).

`query_logs(workspace_id, kql, timespan)` posts one KQL query to
`https://api.loganalytics.io/v1/workspaces/<id>/query` with a token for
`https://api.loganalytics.io/.default` from the caller's Azure CLI sign-in, and returns the rows
of the result's first table as dictionaries.

`python -m docchat_testing.logquery chat-telemetry --workspace-id <id> --since <UTC time>` is the
person-run check T-282: every 60 s, for at most 15 minutes, it looks for an `AppDependencies` row
of `AppRoleName` `docchat-api` and `Name` `docchat.chat.turn` carrying
`Properties["docchat.ttft_ms"]`, and for `AppMetrics` rows named `docchat.openai.tokens` and
`docchat.openai.estimated_cost`, each with `TimeGenerated` after `--since` and ingested at most 15
minutes after it. It exits 0 when all three are found, and 1 with `telemetry_missing`, naming
each item not found, otherwise.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, Final
from uuid import UUID

import httpx
from azure.identity.aio import AzureCliCredential

from docchat_core.clients.tls import TLS_CONTEXT

QUERY_URL: Final = "https://api.loganalytics.io/v1/workspaces/{workspace_id}/query"
SCOPE: Final = "https://api.loganalytics.io/.default"
TIMEOUT_SECONDS: Final = 30.0

POLL_SECONDS: Final = 60.0
POLL_LIMIT: Final = timedelta(minutes=15)
INGESTION_DELAY_LIMIT: Final = "15m"

TURN_ITEM: Final = "docchat.chat.turn"
TOKENS_ITEM: Final = "docchat.openai.tokens"
COST_ITEM: Final = "docchat.openai.estimated_cost"
CHAT_TELEMETRY_ITEMS: Final = (TURN_ITEM, TOKENS_ITEM, COST_ITEM)
MISSING_CODE: Final = "telemetry_missing"
EXIT_FOUND: Final = 0
EXIT_MISSING: Final = 1

type Rows = list[dict[str, Any]]
type QueryRunner = Callable[[str, str, str], Awaitable[Rows]]


class LogQueryError(Exception):
    """The query API refused or failed a query; the message holds its status and error code."""


def timespan_of(value: str | timedelta) -> str:
    """An ISO 8601 duration or interval for the API's `timespan`."""
    if isinstance(value, timedelta):
        return f"PT{max(int(value.total_seconds()), 1)}S"
    return value


def rows_of(body: Mapping[str, Any]) -> Rows:
    """The first table's rows, each a dictionary keyed by column name."""
    tables = body.get("tables")
    if not isinstance(tables, list) or not tables:
        raise LogQueryError("the query answer has no table")
    first = tables[0]
    columns = [str(column["name"]) for column in first.get("columns", [])]
    return [dict(zip(columns, row, strict=True)) for row in first.get("rows", [])]


async def query_logs(
    workspace_id: str,
    kql: str,
    timespan: str | timedelta,
    *,
    client: httpx.AsyncClient | None = None,
    token: Callable[[], Awaitable[str]] | None = None,
) -> Rows:
    """Run `kql` over `timespan` in the workspace and return the first table's rows."""
    url = QUERY_URL.format(workspace_id=UUID(workspace_id))
    owned_client = client is None
    http = (
        httpx.AsyncClient(
            timeout=httpx.Timeout(TIMEOUT_SECONDS), follow_redirects=False, verify=TLS_CONTEXT
        )
        if client is None
        else client
    )
    try:
        if token is not None:
            bearer = await token()
        else:
            async with AzureCliCredential() as credential:
                bearer = (await credential.get_token(SCOPE)).token
        response = await http.post(
            url,
            json={"query": kql, "timespan": timespan_of(timespan)},
            headers={"Authorization": f"Bearer {bearer}"},
        )
    finally:
        if owned_client:
            await http.aclose()
    if not response.is_success:
        try:
            code = str(response.json().get("error", {}).get("code", ""))
        except (ValueError, AttributeError):
            code = ""
        raise LogQueryError(f"the query API answered {response.status_code} {code}".rstrip())
    return rows_of(response.json())


def kql_datetime(moment: datetime) -> str:
    """`datetime(<ISO 8601 UTC>)` for a KQL literal."""
    return f"datetime({moment.astimezone(UTC).strftime('%Y-%m-%dT%H:%M:%S.%fZ')})"


def chat_telemetry_query(since: datetime) -> str:
    """One row per item with the count of its records after `since` that arrived in time."""
    return f"""let since = {kql_datetime(since)};
let turns = AppDependencies
  | where TimeGenerated > since and AppRoleName == "docchat-api" and Name == "{TURN_ITEM}"
  | where isnotempty(tostring(Properties["docchat.ttft_ms"]))
  | where ingestion_time() - TimeGenerated <= {INGESTION_DELAY_LIMIT}
  | summarize found = count()
  | extend item = "{TURN_ITEM}";
let metrics = AppMetrics
  | where TimeGenerated > since and Name in ("{TOKENS_ITEM}", "{COST_ITEM}")
  | where ingestion_time() - TimeGenerated <= {INGESTION_DELAY_LIMIT}
  | summarize found = count() by item = Name;
union turns, metrics
| project item, found"""


def found_items(rows: Rows) -> set[str]:
    return {
        str(row["item"]) for row in rows if row.get("item") in CHAT_TELEMETRY_ITEMS and row["found"]
    }


async def check_chat_telemetry(
    workspace_id: str,
    since: datetime,
    *,
    run_query: QueryRunner | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    monotonic: Callable[[], float] = time.monotonic,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> list[str]:
    """Poll until the three items are found or 15 minutes pass; return the items not found."""

    async def default_query(workspace: str, kql: str, timespan: str) -> Rows:
        return await query_logs(workspace, kql, timespan)

    query = default_query if run_query is None else run_query
    started = monotonic()
    kql = chat_telemetry_query(since)
    found: set[str] = set()
    while True:
        timespan = f"{since.astimezone(UTC).isoformat()}/{now().astimezone(UTC).isoformat()}"
        found |= found_items(await query(workspace_id, kql, timespan))
        missing = [item for item in CHAT_TELEMETRY_ITEMS if item not in found]
        if not missing or monotonic() - started >= POLL_LIMIT.total_seconds():
            return missing
        await sleep(POLL_SECONDS)


def parse_since(value: str) -> datetime:
    """`--since`: an ISO 8601 time; one without an offset is read as UTC."""
    moment = datetime.fromisoformat(value)
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m docchat_testing.logquery")
    commands = parser.add_subparsers(dest="command", required=True)
    chat = commands.add_parser("chat-telemetry", help="the chat telemetry check (T-282)")
    chat.add_argument("--workspace-id", required=True, type=lambda value: str(UUID(value)))
    chat.add_argument("--since", required=True, type=parse_since)
    return parser


def main(argv: Sequence[str] | None = None, *, run_query: QueryRunner | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    missing = asyncio.run(
        check_chat_telemetry(arguments.workspace_id, arguments.since, run_query=run_query)
    )
    if missing:
        sys.stderr.write(
            json.dumps(
                {
                    "code": MISSING_CODE,
                    "message": "Chat telemetry didn't reach the workspace within 15 minutes.",
                    "detail": missing,
                }
            )
            + "\n"
        )
        return EXIT_MISSING
    sys.stdout.write(json.dumps({"found": list(CHAT_TELEMETRY_ITEMS)}) + "\n")
    return EXIT_FOUND


if __name__ == "__main__":
    sys.exit(main())
