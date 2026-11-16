"""The KQL of the load verdicts and the alert probe (`84-testing.md` section 7.6;
`85-observability.md` section 6.4).

Each function returns one query with the window's bounds as `datetime(...)` literals. The text of
section 7.6 is kept as written: `s` and `e` are the scenario window's start and end.
"""

from __future__ import annotations

from datetime import datetime
from typing import Final

from docchat_testing.logquery import kql_datetime

API_ROLE: Final = "docchat-api"
TURN_REQUEST: Final = "POST /v1/chat/turns"


def _window(start: datetime, end: datetime) -> str:
    return f"let s = {kql_datetime(start)}; let e = {kql_datetime(end)};\n"


def telemetry_arrived(end: datetime) -> str:
    """Rows of the window's last minute: they have arrived when the count is above 0."""
    return (
        f"let e = {kql_datetime(end)};\n"
        "union AppRequests, AppDependencies, AppMetrics\n"
        "| where TimeGenerated between ((e - 1m) .. e)\n"
        "| count"
    )


def c_db(start: datetime, end: datetime) -> str:
    """`C-db`: chat-path Cosmos DB requests that stayed throttled after the SDK's retries."""
    return _window(start, end) + (
        "AppMetrics\n"
        "| where TimeGenerated between (s .. e)\n"
        '| where (Name == "docchat.api.problems" and tostring(Properties["code"]) == '
        '"dependency_unavailable")\n'
        '     or (Name == "docchat.chat.turns" and tostring(Properties["error_code"]) in '
        '("conversation_unavailable", "turn_save_failed"))\n'
        '     or Name == "docchat.usage.record_failures"\n'
        "| summarize n = sum(Sum)"
    )


def c_db_throttled(start: datetime, end: datetime) -> str:
    """The throttling the SDK absorbed, per container, which `C-db` reports."""
    return _window(start, end) + (
        "CDBDataPlaneRequests\n"
        '| where TimeGenerated between (s .. e) and DatabaseName == "docchat"\n'
        "| summarize throttled = countif(StatusCode == 429) by CollectionName"
    )


def c_cap(run_start: datetime) -> str:
    """`C-cap`: the workspace reached a daily cap since the run started."""
    return (
        f"_LogOperation\n| where TimeGenerated between ({kql_datetime(run_start)} .. now())\n"
        '| where Category =~ "Ingestion"\n'
        '| where Detail contains "OverQuota"\n'
        "| count"
    )


def s1_concurrency(start: datetime, end: datetime) -> str:
    """S1-e: the smallest per-minute peak of concurrent turn requests over minutes 2 to 9."""
    return _window(start, end) + (
        "let r = AppRequests\n"
        f'  | where TimeGenerated between (s .. e) and AppRoleName == "{API_ROLE}" and Name == '
        f'"{TURN_REQUEST}"\n'
        "  | project st = TimeGenerated, en = TimeGenerated + DurationMs * 1ms;\n"
        "union (r | project t = st, d = 1), (r | project t = en, d = -1)\n"
        "| order by t asc\n"
        "| extend concurrent = row_cumsum(d)\n"
        "| summarize peak = max(concurrent) by minute = bin(t, 1m)\n"
        "| where minute between ((s + 2m) .. (s + 9m))\n"
        "| summarize minPeak = min(peak)"
    )


def s1_server_errors(start: datetime, end: datetime) -> str:
    """S1-f: `5xx` answers of the API."""
    return _window(start, end) + (
        f'AppRequests | where TimeGenerated between (s .. e) and AppRoleName == "{API_ROLE}" '
        "and toint(ResultCode) >= 500 | summarize n = sum(ItemCount)"
    )


def s1_instances(start: datetime, end: datetime) -> str:
    """S1-g: the instances that served the run."""
    return _window(start, end) + (
        f'AppRequests | where TimeGenerated between (s .. e) and AppRoleName == "{API_ROLE}" '
        "| summarize instances = dcount(AppRoleInstance)"
    )


def request_units(start: datetime, end: datetime) -> str:
    """S1-h (and S3's calibration): each container's charge per completed turn, its highest
    1-minute consumption per second and the throttled requests the SDK absorbed."""
    return _window(start, end) + (
        "let turns = toscalar(AppRequests\n"
        f'  | where TimeGenerated between (s .. e) and AppRoleName == "{API_ROLE}" and Name == '
        f'"{TURN_REQUEST}" and ResultCode == "200"\n'
        "  | summarize sum(ItemCount));\n"
        "CDBDataPlaneRequests\n"
        '| where TimeGenerated between (s .. e) and DatabaseName == "docchat"\n'
        "| summarize ru = sum(RequestCharge), throttled = countif(StatusCode == 429) by "
        "CollectionName, minute = bin(TimeGenerated, 1m)\n"
        "| summarize ruPerTurn = sum(ru) / turns, peakRuPerSecond = max(ru) / 60.0, "
        "throttled = sum(throttled) by CollectionName"
    )


def s1_turn_charges(start: datetime, end: datetime) -> str:
    """S1-i: the p95 over completed turns of the request charge summed over each turn's trace."""
    return _window(start, end) + (
        "let done = AppDependencies\n"
        f'  | where TimeGenerated between (s .. e) and AppRoleName == "{API_ROLE}"\n'
        '      and Name == "docchat.conversations.finalize_turn" and '
        'tostring(Properties["docchat.turn.status"]) == "completed"\n'
        "  | distinct OperationId;\n"
        "AppDependencies\n"
        f'| where TimeGenerated between (s .. e + 5m) and AppRoleName == "{API_ROLE}" and '
        'isnotnull(Properties["docchat.cosmos.request_charge"])\n'
        "| join kind=inner done on OperationId\n"
        '| summarize ru = sum(todouble(Properties["docchat.cosmos.request_charge"])) by '
        "OperationId\n"
        "| summarize p95 = percentile(ru, 95), turns = count()"
    )


def s1_missing_extractions(start: datetime, end: datetime) -> str:
    """S1-k: completed turns of the window without their memory extraction within 5 minutes."""
    return _window(start, end) + (
        "let started = AppDependencies\n"
        f'  | where TimeGenerated between (s .. e) and AppRoleName == "{API_ROLE}"\n'
        '      and Name == "docchat.conversations.begin_turn" and '
        'tostring(Properties["docchat.outcome"]) == "started"\n'
        '  | project OperationId, turn = tostring(Properties["docchat.turn_id"]);\n'
        "let done = AppDependencies\n"
        "  | where TimeGenerated between (s .. e) and "
        'Name == "docchat.conversations.finalize_turn"\n'
        '      and tostring(Properties["docchat.turn.status"]) == "completed"\n'
        "  | distinct OperationId;\n"
        "let extracted = AppDependencies\n"
        '  | where TimeGenerated between (s .. e + 5m) and Name == "docchat.memory.extract"\n'
        '  | distinct turn = tostring(Properties["docchat.turn_id"]);\n'
        "started | join kind=inner done on OperationId | join kind=leftanti extracted on turn | "
        "count"
    )


def s2_requests(start: datetime, end: datetime) -> str:
    """S2: every API request of the window with the instance that served it."""
    return _window(start, end) + (
        f'AppRequests | where TimeGenerated between (s .. e) and AppRoleName == "{API_ROLE}"\n'
        "| project OperationId, AppRoleInstance, TimeGenerated, Success, ResultCode"
    )


def s2_removed_instance(start: datetime, end: datetime, removed_at: datetime) -> str:
    """S2: the instance with requests in [T_r - 120 s, T_r] and none in [T_r + 60 s, e]."""
    return _window(start, end) + (
        f"let tr = {kql_datetime(removed_at)};\n"
        f'AppRequests | where TimeGenerated between (s .. e) and AppRoleName == "{API_ROLE}"\n'
        "| summarize before = countif(TimeGenerated between ((tr - 120s) .. tr)), "
        "after = countif(TimeGenerated between ((tr + 60s) .. e)) by AppRoleInstance\n"
        "| where before > 0 and after == 0\n"
        "| project AppRoleInstance"
    )


def s2_surviving_failures(start: datetime, end: datetime, removed: str) -> str:
    """S2-b: failed or `5xx` requests on the surviving instances."""
    return _window(start, end) + (
        f'AppRequests | where TimeGenerated between (s .. e) and AppRoleName == "{API_ROLE}" '
        f'and AppRoleInstance != "{removed}" and (Success == false or toint(ResultCode) >= 500) '
        "| count"
    )


def screen_calls(start: datetime, end: datetime) -> str:
    """S3's calibration: the mean Prompt Shields calls per turn."""
    return (
        "AppDependencies | where TimeGenerated between "
        f"({kql_datetime(start)} .. {kql_datetime(end)}) and AppRoleName == "
        f'"{API_ROLE}" and Name == "docchat.chat.screen" '
        '| summarize s = avg(todouble(Properties["docchat.calls"]))'
    )


def s4_throttled(start: datetime, end: datetime) -> str:
    """S4-c: the ingestion model calls the stub throttled and the app retried."""
    return (
        f"AppMetrics | where TimeGenerated between ({kql_datetime(start)} .. "
        f'{kql_datetime(end)}) and Name == "docchat.ingestion.throttled" '
        "| summarize n = sum(Sum)"
    )


def a41(threshold_ms: int) -> str:
    """Alert A41 (`85-observability.md` section 6.4) with its threshold."""
    return (
        f'AppDependencies | where AppRoleName == "{API_ROLE}" and Name == "docchat.chat.generate" '
        '| extend firstChunk = todouble(Properties["docchat.first_chunk_ms"]) '
        "| where isnotnull(firstChunk) "
        "| summarize calls = count(), p95 = percentile(firstChunk, 95) by deployment = "
        'tostring(Properties["docchat.deployment"]) '
        f"| where calls >= 20 and p95 > {threshold_ms}"
    )


def a42(threshold_ms: int) -> str:
    """Alert A42 (`85-observability.md` section 6.4) with its threshold."""
    return (
        'AppMetrics | where Name == "docchat.openai.duration" and '
        'tostring(Properties["outcome"]) == "ok" and tostring(Properties["call"]) != "answer" '
        "| summarize calls = sum(ItemCount), meanMs = sum(Sum) / sum(ItemCount) by deployment = "
        'tostring(Properties["deployment"]), call = tostring(Properties["call"]) '
        f"| where calls >= 20 and meanMs > {threshold_ms}"
    )
