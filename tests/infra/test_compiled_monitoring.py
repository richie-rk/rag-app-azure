"""T-500, T-498, T-503 and the budget: the monitoring of the compiled template
(85-observability.md sections 5, 6.1, 6.3, 6.4 and 6.7; 83-infrastructure.md sections 12.7 and
12.8): the application alerts A01 to A43 and A58, 41 rules in dev, 44 in prod and none in load,
and the nine workbooks of section 5.1.

Every rule's slug, window, frequency, mute, severity, runbook and query is compared with the
catalogue of 85-observability.md sections 6.3 and 6.4 as `docs/spec` holds it, every catalogue
workbook's tiles with sections 5.2 to 5.10, and every rule's runbook with runbook R20's alert table
(T-503).
"""

from __future__ import annotations

import json
import re
from typing import Any

import pytest
from arm_template import RESOURCE_GROUP_ID_PREFIX, guid
from compiled_support import (
    ENVIRONMENTS,
    INFRA,
    REPO_ROOT,
    MainFactory,
    avm_call,
    env_file,
    of_type,
)

# The rules of 85-observability.md section 6.3. A01 and A02 are metric rules; A11 and A40 exist only
# in prod and A15 only where storage.malwareScanning is true.
METRIC_RULES = {"A01", "A02"}
PROD_RULES = {"A11", "A40"}
DECLARED_RULES = {
    "A03",
    "A04",
    "A05",
    "A06",
    "A07",
    "A08",
    "A09",
    "A10",
    "A12",
    "A13",
    "A14",
    "A16",
    "A17",
    "A18",
    "A19",
    "A20",
    "A21",
    "A22",
    "A23",
    "A24",
    "A25",
    "A26",
    "A27",
    "A28",
    "A29",
    "A30",
    "A31",
    "A32",
    "A33",
    "A34",
    "A35",
    "A36",
    "A37",
    "A38",
    "A39",
    "A41",
    "A42",
    "A43",
    "A58",
}
SCANNING_RULES = {"A15"}
WORKBOOKS = {
    "availability": "API availability and 5xx rate",
    "ttft": "Time to first token",
    "openai": "Azure OpenAI 429s and latency",
    "ingestion": "Ingestion failures and queue age",
    "quota": "Quota and rate-limit rejections",
    "capacity": "Instances, CPU, event loop, SQL and Cosmos DB",
    "budget": "Monthly budget and estimated model cost",
    "conversations": "Conversations",
    "access": "Access",
}
# The workbooks whose every tile is a KQL query of 85-observability.md section 5, by section.
CATALOGUE_WORKBOOKS = {
    "availability": "5.2",
    "ttft": "5.3",
    "openai": "5.4",
    "ingestion": "5.5",
    "quota": "5.6",
    "conversations": "5.9",
    "access": "5.10",
}
# Each tile kind of the catalogue as a workbook visualization.
VISUALIZATIONS = {
    "line": "linechart",
    "table": "table",
    "bar": "barchart",
    "column": "barchart",
    "stacked column": "barchart",
    "tile": "tiles",
}
# The three thresholds the environment files set (85-observability.md section 6.1).
THRESHOLDS = {
    "<alerts.ttftP95Ms>": "ttftP95Ms",
    "<alerts.openAiFirstChunkP95Ms>": "openAiFirstChunkP95Ms",
    "<alerts.openAiCallMeanMs>": "openAiCallMeanMs",
}
PLACEHOLDERS = {
    "workspaceId",
    "apiSiteId",
    "ingestionSiteId",
    "apiPlanId",
    "autoscaleId",
    "budgetId",
    "apiPrincipalId",
    "TimeRange",
}
ACTION_GROUP = "<ag-docchat-{environment}-ops.outputs.resourceId.value>"
OBSERVABILITY = REPO_ROOT / "docs" / "spec" / "85-observability.md"
OPERATIONS = REPO_ROOT / "docs" / "spec" / "86-operations.md"
RUNBOOK_MAP = REPO_ROOT / "docs" / "runbooks" / "R20-alert-response-index-and-runbook-maps.md"
MAX_INSTANCES = {"dev": 1, "prod": 6, "load": 6}
# Rules per environment as shipped (85-observability.md section 6.5; T-500).
RULE_COUNTS = {"dev": 41, "prod": 44, "load": 0}


def _catalogue() -> dict[str, dict[str, Any]]:
    """Section 6.3's rows and section 6.4's queries, by alert ID."""
    text = OBSERVABILITY.read_text(encoding="utf-8")
    # | ID | `slug` (note) | signal | window / frequency | mute | severity | runbook |
    row = (
        r"^\| (A\d\d) \| `([a-z0-9-]+)`[^|]* \| [^|]+ \| (\S+) / (\S+) \| (\S+) \| (\d) \|"
        r" ([^|]+) \|$"
    )
    rows = re.findall(row, text, re.MULTILINE)
    # A note in parentheses may precede the query, with code spans of its own (A29's).
    queries = dict(
        re.findall(r"^- \*\*(A\d+)\*\* (?:\((?:[^()`]|`[^`]*`)*\) )?`([^`]+)`", text, re.MULTILINE)
    )
    return {
        alert_id: {
            "slug": slug,
            "windowSize": window,
            "evaluationFrequency": frequency,
            "mute": None if mute == "none" else mute,
            "severity": int(severity),
            "runbook": runbook.strip(),
            "query": queries.get(alert_id),
        }
        for alert_id, slug, window, frequency, mute, severity, runbook in rows
    }


CATALOGUE = _catalogue()


def _rules_by_id(deployment: Any, rule_type: str = "scheduledQueryRules") -> dict[str, Any]:
    rules = of_type(deployment, f"Microsoft.Insights/{rule_type}")
    by_id = {rule.properties["description"].split(" ", 1)[0]: rule for rule in rules}
    assert len(by_id) == len(rules)
    return by_id


def _api_principal(environment: str) -> str:
    """The API identity's principal ID, known only at deployment: the identity module's output."""
    return f"<id-docchat-api-{environment}.outputs.principalId.value>"


def _expected_query(alert_id: str, environment: str) -> str:
    """Section 6.4's query with its Bicep values substituted: A34's plan and its ceiling, A29's API
    identity, and the thresholds of A06, A41 and A42 from the environment file."""
    query = str(CATALOGUE[alert_id]["query"])
    plan = (
        f"{RESOURCE_GROUP_ID_PREFIX}/rg-docchat-{environment}/providers/Microsoft.Web/serverfarms"
    )
    query = (
        query.replace("<API plan ID>", f"{plan}/asp-docchat-api-{environment}")
        .replace("<api.maxInstances>", str(MAX_INSTANCES[environment]))
        .replace("<API identity principal ID>", _api_principal(environment))
    )
    alerts = env_file(environment)["alerts"]
    for placeholder, field in THRESHOLDS.items():
        query = query.replace(placeholder, str(alerts[field]))
    return query


def test_catalogue_is_read() -> None:
    assert len(CATALOGUE) == 44
    log_rules = DECLARED_RULES | PROD_RULES | SCANNING_RULES
    # Every rule of the catalogue is declared.
    assert log_rules | METRIC_RULES == set(CATALOGUE)
    assert all(CATALOGUE[alert_id]["query"] for alert_id in log_rules)
    assert not [alert_id for alert_id in METRIC_RULES if CATALOGUE[alert_id]["query"]]


@pytest.mark.parametrize("environment", ENVIRONMENTS)
def test_t500_rule_counts(main_deployment: MainFactory, environment: str) -> None:
    deployment = main_deployment(environment)
    rules = _rules_by_id(deployment) | _rules_by_id(deployment, "metricAlerts")
    assert len(rules) == RULE_COUNTS[environment]


@pytest.mark.parametrize("environment", ENVIRONMENTS)
def test_t500_alert_rules(main_deployment: MainFactory, environment: str) -> None:
    rules = _rules_by_id(main_deployment(environment))
    if environment == "load":
        # Alerts are off in load, where the tests cause alert conditions on purpose.
        assert rules == {}
        return
    # Scanning is on in prod only; dev turns it on only while the malware-scanning suite runs.
    expected = DECLARED_RULES | (SCANNING_RULES | PROD_RULES if environment == "prod" else set())
    assert set(rules) == expected
    for alert_id, rule in rules.items():
        entry = CATALOGUE[alert_id]
        properties = rule.properties
        assert rule.name == f"docchat-{environment}-{entry['slug']}"
        assert rule.field("kind") == "LogAlert"
        assert properties["description"] == f"{alert_id} · {entry['runbook']}"
        assert properties["severity"] == entry["severity"]
        assert properties["windowSize"] == entry["windowSize"]
        assert properties["evaluationFrequency"] == entry["evaluationFrequency"]
        assert properties["enabled"] is True
        if entry["mute"] is None:
            # A state alert resolves itself.
            assert properties["autoMitigate"] is True
            assert "muteActionsDuration" not in properties
        else:
            # An event alert stays raised and quiet for its mute.
            assert properties["autoMitigate"] is False
            assert properties["muteActionsDuration"] == entry["mute"]
        assert [str(scope) for scope in properties["scopes"]] == [
            f"<log-docchat-{environment}.outputs.resourceId.value>"
        ]
        assert [str(group) for group in properties["actions"]["actionGroups"]] == [
            ACTION_GROUP.format(environment=environment)
        ]
        (criterion,) = properties["criteria"]["allOf"]
        assert (criterion["timeAggregation"], criterion["operator"], criterion["threshold"]) == (
            "Count",
            "GreaterThan",
            0,
        )
        assert criterion["failingPeriods"] == {
            "numberOfEvaluationPeriods": 1,
            "minFailingPeriodsToAlert": 1,
        }
        assert criterion["query"] == _expected_query(alert_id, environment)


@pytest.mark.parametrize("environment", ENVIRONMENTS)
def test_t500_metric_rules(main_deployment: MainFactory, environment: str) -> None:
    deployment = main_deployment(environment)
    rules = _rules_by_id(deployment, "metricAlerts")
    if environment == "load":
        assert rules == {}
        return
    assert set(rules) == METRIC_RULES
    token = deployment.outputs()["DOCCHAT_RESOURCE_TOKEN"]
    thresholds = {"A01": 1, "A02": 100}
    for alert_id, rule in rules.items():
        entry = CATALOGUE[alert_id]
        properties = rule.properties
        assert rule.api_version == "2026-01-01"
        assert rule.name == f"docchat-{environment}-{entry['slug']}"
        assert rule.field("location") == "global"
        assert properties["description"] == f"{alert_id} · {entry['runbook']}"
        assert properties["severity"] == entry["severity"]
        assert properties["windowSize"] == entry["windowSize"]
        assert properties["evaluationFrequency"] == entry["evaluationFrequency"]
        # Metric rules allow only these windows and frequencies (85-observability.md section 6.1).
        assert properties["windowSize"] in {
            "PT1M",
            "PT5M",
            "PT15M",
            "PT30M",
            "PT1H",
            "PT6H",
            "PT12H",
            "P1D",
        }
        assert properties["evaluationFrequency"] in {"PT1M", "PT5M", "PT15M", "PT30M", "PT1H"}
        assert properties["enabled"] is True
        assert properties["autoMitigate"] is True
        assert [str(scope) for scope in properties["scopes"]] == [
            f"<app-docchat-api-{environment}-{token}.outputs.resourceId.value>"
        ]
        assert [str(action["actionGroupId"]) for action in properties["actions"]] == [
            ACTION_GROUP.format(environment=environment)
        ]
        criteria = properties["criteria"]
        assert criteria["odata.type"] == (
            "Microsoft.Azure.Monitor.SingleResourceMultipleMetricCriteria"
        )
        (criterion,) = criteria["allOf"]
        assert (
            criterion["metricNamespace"],
            criterion["metricName"],
            criterion["timeAggregation"],
            criterion["operator"],
            criterion["threshold"],
        ) == (
            "Microsoft.Web/sites",
            "HealthCheckStatus",
            "Average",
            "LessThan",
            thresholds[alert_id],
        )


def _runbook_ids() -> dict[str, str]:
    """Runbook titles to IDs, from the index of 86-operations.md section 3.2."""
    text = OPERATIONS.read_text(encoding="utf-8")
    section = text.split("### 3.2 Runbook index", 1)[1].split("### 3.3", 1)[0]
    return {
        title: runbook
        for runbook, title in re.findall(r"^\| (R\d\d) \| ([^|]+?) \|", section, re.M)
    }


def _runbook_map() -> dict[str, list[str]]:
    """R20's alert table: each alert slug to the runbook IDs its row names, in order."""
    text = RUNBOOK_MAP.read_text(encoding="utf-8")
    table = text.split("## Alerts", 1)[1].split("\n## ", 1)[0]
    mapped: dict[str, list[str]] = {}
    for alerts, runbooks in re.findall(r"^\| ([^|]+) \| ([^|]+) \| [^|]+ \|$", table, re.M):
        for slug in re.findall(r"`([a-z0-9-]+)`", alerts):
            mapped[slug] = re.findall(r"R\d\d", runbooks)
    return mapped


@pytest.mark.parametrize("environment", ["dev", "prod"])
def test_t503_every_rule_has_its_runbook_row(
    main_deployment: MainFactory, environment: str
) -> None:
    deployment = main_deployment(environment)
    rules = _rules_by_id(deployment) | _rules_by_id(deployment, "metricAlerts")
    titles = _runbook_ids()
    mapped = _runbook_map()
    for alert_id, rule in rules.items():
        slug = str(rule.name).removeprefix(f"docchat-{environment}-")
        runbook_titles = rule.properties["description"].split(" · ", 1)[1].split(", then ")
        assert slug in mapped, f"{alert_id} {slug} has no row in R20"
        assert mapped[slug] == [titles[title] for title in runbook_titles], alert_id


def test_t500_prod_only_rules(main_deployment: MainFactory) -> None:
    rules = _rules_by_id(main_deployment("prod"))
    assert rules["A11"].name == "docchat-prod-prices-missing"
    assert rules["A40"].name == "docchat-prod-index-override-used"
    assert not PROD_RULES & set(_rules_by_id(main_deployment("dev")))


def test_t500_model_thresholds_come_from_the_environment_file(
    main_deployment: MainFactory,
) -> None:
    config = {
        "alerts": {
            "enabled": True,
            "ttftP95Ms": 15000,
            "openAiFirstChunkP95Ms": 7000,
            "openAiCallMeanMs": 6000,
        }
    }
    rules = _rules_by_id(main_deployment("prod", config=config))
    queries = {
        alert_id: rules[alert_id].properties["criteria"]["allOf"][0]["query"]
        for alert_id in ("A06", "A41", "A42")
    }
    assert queries["A06"].endswith("| where turns >= 10 and p95 > 15000")
    assert queries["A41"].endswith("| where calls >= 20 and p95 > 7000")
    assert queries["A42"].endswith("| where calls >= 20 and meanMs > 6000")


def test_t500_capacity_alert_reads_the_plan_ceiling(main_deployment: MainFactory) -> None:
    config = {"api": {"sku": "P1v3", "minInstances": 2, "maxInstances": 9, "zoneRedundant": False}}
    rules = _rules_by_id(main_deployment("prod", config=config))
    assert (
        rules["A34"]
        .properties["criteria"]["allOf"][0]["query"]
        .endswith("| where cpu > 85 and instances >= 9")
    )


def test_t500_scan_gate_alert_follows_the_scanning_switch(main_deployment: MainFactory) -> None:
    scanning = {"storage": {"malwareScanning": True, "malwareScanCapGbPerMonth": 1000}}
    rules = _rules_by_id(main_deployment("dev", config=scanning))
    assert set(rules) == DECLARED_RULES | SCANNING_RULES
    assert rules["A15"].name == "docchat-dev-uploads-scan-gate-closed"
    # Its query sets its own 48-hour filter inside the two-day window.
    assert "ago(48h)" in rules["A15"].properties["criteria"]["allOf"][0]["query"]
    assert len(rules) + len(METRIC_RULES) == RULE_COUNTS["dev"] + 1


@pytest.mark.parametrize("environment", ["dev", "prod"])
def test_t500_human_access_alert_excludes_only_the_api_identity(
    main_deployment: MainFactory, environment: str
) -> None:
    rule = _rules_by_id(main_deployment(environment))["A29"]
    assert rule.name == f"docchat-{environment}-human-conversation-access"
    query = rule.properties["criteria"]["allOf"][0]["query"]
    assert f'where AadPrincipalId != "{_api_principal(environment)}"' in query
    # Its own time filter is the rule's window, so each evaluation reports a window once.
    assert "where TimeGenerated > ago(15m)" in query
    assert rule.properties["windowSize"] == rule.properties["evaluationFrequency"] == "PT15M"
    assert rule.properties["muteActionsDuration"] == "PT15M"


def test_t500_no_rule_where_alerts_are_off(main_deployment: MainFactory) -> None:
    config = {
        "alerts": {
            "enabled": False,
            "ttftP95Ms": 20000,
            "openAiFirstChunkP95Ms": 10000,
            "openAiCallMeanMs": 8000,
        },
        "storage": {"malwareScanning": True, "malwareScanCapGbPerMonth": 1000},
    }
    assert _rules_by_id(main_deployment("prod", config=config)) == {}


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [text for item in value.values() for text in _strings(item)]
    if isinstance(value, list):
        return [text for item in value for text in _strings(item)]
    return []


def _queries(workbook: dict[str, Any]) -> list[dict[str, Any]]:
    return [item["content"] for item in workbook["items"] if item["type"] == 3]


@pytest.mark.parametrize("key", sorted(WORKBOOKS))
def test_t498_workbook_files(key: str) -> None:
    workbook = json.loads(
        (INFRA / "modules" / "monitoring" / "workbooks" / f"{key}.workbook.json").read_text()
    )
    parameters = [
        parameter
        for item in workbook["items"]
        if item["type"] == 9
        for parameter in item["content"]["parameters"]
    ]
    assert any(parameter["name"] == "TimeRange" for parameter in parameters)
    for query in _queries(workbook):
        if query["queryType"] == 0:
            assert " | where TimeGenerated {TimeRange}" in query["query"] or query[
                "query"
            ].startswith("let ")
            assert "{TimeRange}" in query["query"]
            assert query["timeContextFromParameter"] == "TimeRange"
    used = {name for text in _strings(workbook) for name in re.findall(r"\{([A-Za-z]+)\}", text)}
    assert used <= PLACEHOLDERS


@pytest.mark.parametrize(
    ("key", "count"),
    [
        ("availability", 7),
        ("ttft", 7),
        ("openai", 8),
        ("ingestion", 10),
        ("quota", 5),
        ("conversations", 9),
        ("access", 7),
    ],
)
def test_t498_tiles_equal_the_catalogue(key: str, count: int) -> None:
    text = OBSERVABILITY.read_text(encoding="utf-8")
    number = CATALOGUE_WORKBOOKS[key]
    section = text.split(f"### {number} `{key}`", 1)[1].split("\n### ", 1)[0]
    # Each line gives the tile's title, its kind first in the parentheses (bins may follow), an
    # optional note such as "the measure of A41", and the query.
    tiles = re.findall(
        r"^\d+\. (.+?) \(([^(),]+)[^()]*\)(?:, [^:`]+)?: `(.+)`$", section, re.MULTILINE
    )
    assert len(tiles) == count
    workbook = json.loads(
        (INFRA / "modules" / "monitoring" / "workbooks" / f"{key}.workbook.json").read_text()
    )
    actual = [
        (query["title"], query["visualization"], query["query"]) for query in _queries(workbook)
    ]
    # The catalogue writes the time filter `{T}`; the workbook's parameter is TimeRange.
    assert actual == [
        (title, VISUALIZATIONS[kind], query.replace("{T}", "{TimeRange}"))
        for title, kind, query in tiles
    ]


@pytest.mark.parametrize("environment", ENVIRONMENTS)
def test_t498_workbooks(main_deployment: MainFactory, environment: str) -> None:
    workbooks = of_type(main_deployment(environment), "Microsoft.Insights/workbooks")
    by_name = {str(workbook.name): workbook for workbook in workbooks}
    resource_group_id = f"{RESOURCE_GROUP_ID_PREFIX}/rg-docchat-{environment}"
    assert len(workbooks) == len(WORKBOOKS) == 9
    for key, title in WORKBOOKS.items():
        workbook = by_name[guid(resource_group_id, "docchat-workbook", key)]
        properties = workbook.properties
        assert workbook.field("kind") == "shared"
        assert properties["category"] == "workbook"
        assert properties["displayName"] == f"DocChat {environment} - {title}"
        assert (
            str(properties["sourceId"]) == f"<log-docchat-{environment}.outputs.resourceId.value>"
        )
        content = json.loads(re.sub(r"<[^>]+>", "x", properties["serializedData"]))
        remaining = {
            name for text in _strings(content) for name in re.findall(r"\{([A-Za-z]+)\}", text)
        }
        assert remaining <= {"TimeRange"}


@pytest.mark.parametrize("environment", ENVIRONMENTS)
def test_t498_conversations_human_access_tile_names_the_api_identity(
    main_deployment: MainFactory, environment: str
) -> None:
    workbooks = of_type(main_deployment(environment), "Microsoft.Insights/workbooks")
    (conversations,) = [
        workbook
        for workbook in workbooks
        if workbook.properties["displayName"] == f"DocChat {environment} - Conversations"
    ]
    content = json.loads(conversations.properties["serializedData"])
    tiles = {query["title"]: query["query"] for query in _queries(content)}
    assert (
        f'AadPrincipalId != "{_api_principal(environment)}"'
        in tiles["Human access to conversation data"]
    )


@pytest.mark.parametrize("environment", ENVIRONMENTS)
def test_budget_and_its_notifications(main_deployment: MainFactory, environment: str) -> None:
    deployment = main_deployment(environment)
    (budget,) = of_type(deployment, "Microsoft.Consumption/budgets")
    properties = budget.properties
    assert budget.name == f"budget-docchat-{environment}"
    assert properties["category"] == "Cost"
    assert properties["timeGrain"] == "Monthly"
    assert properties["amount"] == {"dev": 650, "prod": 2200, "load": 400}[environment]
    # The preprovision hook's yyyy-MM-01, set once when the budget is created.
    assert re.fullmatch(r"\d{4}-\d{2}-01", properties["timePeriod"]["startDate"])
    notifications = properties["notifications"]
    assert set(notifications) == {"actual-50", "actual-80", "actual-100"}
    for name, notification in notifications.items():
        assert notification["threshold"] == int(name.split("-", 1)[1])
        assert (notification["operator"], notification["thresholdType"]) == (
            "GreaterThan",
            "Actual",
        )
        assert notification["enabled"] is True
        assert [str(group) for group in notification["contactGroups"]] == [
            ACTION_GROUP.format(environment=environment)
        ]
        assert notification["contactEmails"] == []
        assert notification["locale"] == "en-us"
    group = avm_call(deployment, f"ag-docchat-{environment}-ops")
    assert group["groupShortName"] == f"dc{environment}ops"
    assert group["enabled"] is True


def test_budget_omitted_when_disabled(main_deployment: MainFactory) -> None:
    deployment = main_deployment("load", config={"budget": {"enabled": False, "amount": 400}})
    assert of_type(deployment, "Microsoft.Consumption/budgets") == []
    budget_workbook = next(
        workbook
        for workbook in of_type(deployment, "Microsoft.Insights/workbooks")
        if "Monthly budget" in workbook.properties["displayName"]
    )
    content = json.loads(budget_workbook.properties["serializedData"])
    (arm_query,) = [query for query in _queries(content) if query["queryType"] == 12]
    assert json.loads(arm_query["query"])["path"] == ""
