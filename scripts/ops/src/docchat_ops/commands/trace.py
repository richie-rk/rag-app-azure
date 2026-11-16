"""`trace show <trace ID>` (`86-operations.md` section 2.8.9; `85-observability.md` section 2.11):
the records of the trace behind a user's reference ID, read from the environment's Log Analytics
workspace through the Logs query API v1.

Any operator runs it through Log Analytics Reader on the workspace; it needs no just-in-time level
and writes no audit row. It prints each record's time, type, name, duration, success, result code
and role name, and from `Properties` only four identifier attributes, so neither a message nor any
other property reaches the screen.
"""

from __future__ import annotations

import argparse
import json
import re
from typing import TYPE_CHECKING, Final

import httpx

from docchat_core.clients.tls import TLS_CONTEXT
from docchat_ops.framework.auth import LOGS_SCOPE, acquire_token
from docchat_ops.framework.exitcodes import (
    SIGN_IN,
    AccessDenied,
    NotFound,
    UnexpectedError,
    Unreachable,
)

if TYPE_CHECKING:
    from pydantic import JsonValue

    from docchat_ops.cli import Run

QUERY_URL: Final = "https://api.loganalytics.io/v1/workspaces/{workspace_id}/query"
TIMESPAN: Final = "P30D"
TIMEOUT: Final = httpx.Timeout(30.0, connect=5.0)
TRACE_ID_PATTERN: Final = r"^[0-9a-f]{32}$"
PLACEHOLDER: Final = "<trace ID>"

REFERENCE_QUERY: Final = (
    "union AppRequests, AppDependencies, AppTraces, AppEvents\n"
    '| where TimeGenerated > ago(30d) and OperationId == "<trace ID>"\n'
    "| project TimeGenerated, Type, Name, Message, DurationMs, ResultCode, Success, AppRoleName, "
    "Properties\n"
    "| order by TimeGenerated asc"
)
"""The reference-ID query of `85-observability.md` section 2.11."""

LINK_QUERY: Final = """AppDependencies
| where TimeGenerated > ago(30d)
    and Name in ("docchat.ingestion.dispatcher.sweep", "docchat.ingestion.dispatcher.claim")
    and tostring(Properties["_MS.links"]) has "<trace ID>"
| project TimeGenerated, Name, DurationMs, Properties"""
"""The dispatcher-link query of the same section: the dispatcher's work for a run links to the
run's trace instead of joining it."""

PROPERTY_KEYS: Final = ("docchat.error_code", "docchat.session_id", "docchat.turn_id", "enduser.id")
COLUMNS: Final = (
    "time",
    "type",
    "name",
    "durationMs",
    "success",
    "resultCode",
    "roleName",
    "properties",
)
LINK_TYPE: Final = "AppDependencies"

_TRACE_ID_RE: Final = re.compile(TRACE_ID_PATTERN)


def trace_id(value: str) -> str:
    """The positional argument: 32 lower-case hexadecimal characters (exit 2 otherwise)."""
    if not _TRACE_ID_RE.fullmatch(value):
        raise argparse.ArgumentTypeError("a trace ID is 32 lower-case hexadecimal characters")
    return value


def show_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("trace_id", type=trace_id, metavar="<trace ID>", help="the reference ID")


def _scalar(value: object) -> JsonValue:
    return value if isinstance(value, (str, int, float, bool)) or value is None else str(value)


def _properties(value: object) -> dict[str, JsonValue]:
    # A dynamic column arrives as JSON text.
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return {}
    if not isinstance(value, dict):
        return {}
    return {
        key: _scalar(value[key])
        for key in PROPERTY_KEYS
        if key in value and isinstance(value[key], (str, int, float, bool))
    }


def record(row: dict[str, object], default_type: str | None = None) -> dict[str, JsonValue]:
    """The printed form of one result row."""
    return {
        "time": _scalar(row.get("TimeGenerated")),
        "type": _scalar(row.get("Type", default_type)),
        "name": _scalar(row.get("Name")),
        "durationMs": _scalar(row.get("DurationMs")),
        "success": _scalar(row.get("Success")),
        "resultCode": _scalar(row.get("ResultCode")),
        "roleName": _scalar(row.get("AppRoleName")),
        "properties": _properties(row.get("Properties")),
    }


def _rows(body: object) -> list[dict[str, object]]:
    if not isinstance(body, dict):
        raise UnexpectedError("the Logs query API answered no table", "run the command again")
    tables = body.get("tables")
    if not isinstance(tables, list) or not tables or not isinstance(tables[0], dict):
        raise UnexpectedError("the Logs query API answered no table", "run the command again")
    table = tables[0]
    columns = [column.get("name") for column in table.get("columns", [])]
    return [dict(zip(columns, row, strict=False)) for row in table.get("rows", [])]


async def query_logs(
    client: httpx.AsyncClient, token: str, workspace_id: str, kql: str, environment: str
) -> list[dict[str, object]]:
    """One query's rows as dictionaries keyed by column name."""
    try:
        response = await client.post(
            QUERY_URL.format(workspace_id=workspace_id),
            headers={"Authorization": f"Bearer {token}"},
            json={"query": kql, "timespan": TIMESPAN},
        )
    except httpx.TransportError:
        raise Unreachable(
            "the Logs query API didn't answer", "check the network, then run the command again"
        ) from None
    status = response.status_code
    if status in {401, 403}:
        raise AccessDenied(
            f"the Logs query API answered {status}",
            "check Log Analytics Reader on the workspace through sg-docchat-operators, then "
            + SIGN_IN,
        )
    if status == 404:  # noqa: PLR2004 - the HTTP status
        raise NotFound(
            "the Logs query API knows no such workspace",
            f"run azd env refresh -e {environment}",
        )
    if status == 429 or status >= 500:  # noqa: PLR2004 - the HTTP statuses
        raise Unreachable(f"the Logs query API answered {status}", "run the command again")
    if status != 200:  # noqa: PLR2004 - the HTTP status
        raise UnexpectedError(f"the Logs query API answered {status}", "report the correlation ID")
    try:
        return _rows(response.json())
    except json.JSONDecodeError:
        raise UnexpectedError(
            "the Logs query API answered no JSON", "run the command again"
        ) from None


async def show(run: Run, args: argparse.Namespace) -> None:
    wanted: str = args.trace_id
    workspace_id = run.azd().require_uuid("DOCCHAT_LOG_ANALYTICS_WORKSPACE_ID")
    async with run.toolkit.credential() as credential:
        token = await acquire_token(credential, LOGS_SCOPE, "the Logs query API")
    async with httpx.AsyncClient(
        timeout=TIMEOUT,
        follow_redirects=False,
        transport=run.toolkit.http_transport,
        verify=TLS_CONTEXT,
    ) as client:
        direct = await query_logs(
            client,
            token,
            workspace_id,
            REFERENCE_QUERY.replace(PLACEHOLDER, wanted),
            run.environment,
        )
        linked = await query_logs(
            client, token, workspace_id, LINK_QUERY.replace(PLACEHOLDER, wanted), run.environment
        )
    records = [record(row) for row in direct] + [record(row, LINK_TYPE) for row in linked]
    if not records:
        raise NotFound(
            f"no record of trace {wanted} in the last 30 days",
            "check the reference ID and the environment",
        )
    records.sort(key=lambda item: str(item["time"]))
    printed: list[JsonValue] = list(records)
    run.report(
        "found",
        {"traceId": wanted, "records": printed},
        rows="records",
        columns=COLUMNS,
    )
