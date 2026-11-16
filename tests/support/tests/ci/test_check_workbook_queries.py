"""`check_workbook_queries.py` with recorded inputs (85-observability.md section 5.11).

The token, the alert-rule listing and the Logs query API are replaced, so no test reaches Azure.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

# scripts/ci is no package (its checks run as files), so its folder goes on the import path.
SCRIPTS_CI = str(Path(__file__).resolve().parents[4] / "scripts" / "ci")
if SCRIPTS_CI not in sys.path:
    sys.path.insert(0, SCRIPTS_CI)

import check_workbook_queries  # noqa: E402

AZD_ENV = """\
AZURE_ENV_NAME="dev"
AZURE_SUBSCRIPTION_ID="11111111-1111-1111-1111-111111111111"
AZURE_RESOURCE_GROUP="rg-docchat-dev"
DOCCHAT_LOG_ANALYTICS_WORKSPACE_ID="22222222-2222-2222-2222-222222222222"
DOCCHAT_LOG_ANALYTICS_RESOURCE_ID="/subscriptions/1/resourceGroups/rg-docchat-dev/providers/Microsoft.OperationalInsights/workspaces/log-docchat-dev"
DOCCHAT_API_APP_NAME="app-docchat-api-dev-abc123"
DOCCHAT_INGESTION_APP_NAME="func-docchat-ingest-dev-abc123"
DOCCHAT_API_IDENTITY_PRINCIPAL_ID="33333333-3333-3333-3333-333333333333"
"""

AVAILABILITY = {
    "version": "Notebook/1.0",
    "items": [
        {"type": 9, "name": "parameters", "content": {"version": "KqlParameterItem/1.0"}},
        {
            "type": 3,
            "name": "availability-pct",
            "content": {
                "version": "KqlItem/1.0",
                "query": 'AppRequests | where TimeGenerated {TimeRange} and Name != "GET /healthz"',
                "queryType": 0,
            },
        },
        {
            "type": 12,
            "name": "platform",
            "content": {
                "version": "NotebookGroup/1.0",
                "items": [
                    {
                        "type": 3,
                        "name": "healthy-instances",
                        "content": {
                            "query": "AzureMetrics | where TimeGenerated {TimeRange} and "
                            'ResourceId =~ "{apiSiteId}"',
                        },
                    }
                ],
            },
        },
    ],
}
ARM_QUERY = '{"version": "ARMEndpoint/1.0", "path": "{budgetId}"}'
BUDGET = {
    "version": "Notebook/1.0",
    "items": [
        {
            "type": 3,
            "name": "budget-status",
            "content": {
                "query": ARM_QUERY,
                "queryType": 12,
            },
        },
        {
            "type": 3,
            "name": "model-cost",
            "content": {
                "query": "CDBDataPlaneRequests | where TimeGenerated {TimeRange} and "
                'AadPrincipalId != "{apiPrincipalId}" | where ResourceId =~ "{apiPlanId}"',
                "queryType": 0,
            },
        },
    ],
}
RULES = [
    {
        "name": "docchat-dev-api-5xx-high",
        "properties": {
            "windowSize": "PT5M",
            "criteria": {"allOf": [{"query": "AppRequests | summarize n = count()"}]},
        },
    },
    {"name": "docchat-dev-health", "properties": {"criteria": {"allOf": [{"metricName": "x"}]}}},
]


@pytest.fixture
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    workbooks = tmp_path / "infra" / "modules" / "monitoring" / "workbooks"
    workbooks.mkdir(parents=True)
    (workbooks / "availability.workbook.json").write_text(json.dumps(AVAILABILITY), "utf-8")
    (workbooks / "budget.workbook.json").write_text(json.dumps(BUDGET), "utf-8")
    environment = tmp_path / ".azure" / "dev"
    environment.mkdir(parents=True)
    (environment / ".env").write_text(AZD_ENV, encoding="utf-8")
    return tmp_path


class RecordedLogs:
    """The Logs query API: records each query and fails those whose text holds `fail_on`."""

    def __init__(self, fail_on: str | None = None) -> None:
        self.fail_on = fail_on
        self.queries: list[tuple[str, str, Any]] = []

    def __call__(self, workspace: str, token: str, query: Any) -> None:
        self.queries.append((workspace, token, query))
        if self.fail_on and self.fail_on in query.text:
            raise check_workbook_queries.QueryError("BadArgumentError: syntax error")


def run(root: Path, monkeypatch: pytest.MonkeyPatch, logs: RecordedLogs) -> int:
    listed: list[tuple[str, str]] = []

    def list_alert_rules(subscription: str, group: str) -> list[Any]:
        listed.append((subscription, group))
        return RULES

    monkeypatch.setattr(check_workbook_queries, "get_token", lambda: "token-value")
    monkeypatch.setattr(check_workbook_queries, "list_alert_rules", list_alert_rules)
    monkeypatch.setattr(check_workbook_queries, "run_query", logs)
    code = check_workbook_queries.main(["--env", "dev", "--root", str(root)])
    assert listed in ([], [("11111111-1111-1111-1111-111111111111", "rg-docchat-dev")])
    return code


def test_every_kql_tile_and_alert_query_runs(
    root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    logs = RecordedLogs()

    assert run(root, monkeypatch, logs) == 0
    sent = [(query.source, query.name, query.text, query.timespan) for _, _, query in logs.queries]
    group = "/subscriptions/11111111-1111-1111-1111-111111111111/resourceGroups/rg-docchat-dev"
    assert sent == [
        (
            "availability.workbook.json",
            "availability-pct",
            'AppRequests | where TimeGenerated > ago(1h) and Name != "GET /healthz"',
            None,
        ),
        (
            "availability.workbook.json",
            "platform / healthy-instances",
            (
                "AzureMetrics | where TimeGenerated > ago(1h) and ResourceId =~ "
                f'"{group}/providers/Microsoft.Web/sites/app-docchat-api-dev-abc123"'
            ),
            None,
        ),
        (
            "budget.workbook.json",
            "model-cost",
            (
                "CDBDataPlaneRequests | where TimeGenerated > ago(1h) and AadPrincipalId != "
                '"33333333-3333-3333-3333-333333333333" | where ResourceId =~ '
                f'"{group}/providers/Microsoft.Web/serverfarms/asp-docchat-api-dev"'
            ),
            None,
        ),
        ("alert", "docchat-dev-api-5xx-high", "AppRequests | summarize n = count()", "PT5M"),
    ]
    assert {(workspace, token) for workspace, token, _ in logs.queries} == {
        ("22222222-2222-2222-2222-222222222222", "token-value")
    }
    assert "4 of 4 queries ran without an error" in capsys.readouterr().out


def test_a_failing_tile_query_fails_naming_the_workbook_and_tile(
    root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(root, monkeypatch, RecordedLogs(fail_on="AzureMetrics")) == 1
    output = capsys.readouterr().out
    assert (
        "::error::availability.workbook.json / platform / healthy-instances: "
        "BadArgumentError: syntax error"
    ) in output
    assert "3 of 4 queries ran without an error" in output


def test_a_failing_alert_query_fails_naming_the_rule(
    root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(root, monkeypatch, RecordedLogs(fail_on="summarize n = count()")) == 1
    assert "::error::alert docchat-dev-api-5xx-high: BadArgumentError" in capsys.readouterr().out


def test_a_missing_azd_setting_stops_the_check(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env_file = root / ".azure" / "dev" / ".env"
    env_file.write_text(AZD_ENV.replace("DOCCHAT_API_APP_NAME", "OTHER"), encoding="utf-8")
    logs = RecordedLogs()

    assert run(root, monkeypatch, logs) == 1
    assert logs.queries == []


def test_a_workbook_that_does_not_parse_stops_the_check(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workbooks = root / "infra" / "modules" / "monitoring" / "workbooks"
    (workbooks / "broken.workbook.json").write_text("{", encoding="utf-8")

    assert run(root, monkeypatch, RecordedLogs()) == 1


def test_the_logs_api_error_message_is_kept() -> None:
    raw = json.dumps(
        {
            "error": {
                "code": "BadArgumentError",
                "message": "The request had some invalid properties",
                "innererror": {"message": "Query could not be parsed at 'whre'"},
            }
        }
    ).encode("utf-8")

    assert check_workbook_queries._api_error(raw, "HTTP 400") == (
        "BadArgumentError: The request had some invalid properties "
        "(Query could not be parsed at 'whre')"
    )
