"""T-426: every diagnostic setting in the compiled templates enables exactly the categories of
83-infrastructure.md section 2.19, for the resources this build stage provisions; each later stage
adds its resources' rows. AppServiceHTTPLogs, AppServiceConsoleLogs, AppServiceAuthenticationLogs
and the host account's StorageRead are never collected (ADR 0107).
"""

from __future__ import annotations

from typing import Any

import pytest
from arm_template import Deployment
from compiled_support import (
    ENVIRONMENTS,
    SECURITY_WORKSPACE_ID,
    MainFactory,
    SharedFactory,
    avm_call,
    of_type,
)

Categories = tuple[frozenset[str], frozenset[str]]

DATA_SERVICE = (
    frozenset({"StorageRead", "StorageWrite", "StorageDelete"}),
    frozenset({"Transaction"}),
)
HOST_SERVICE = (frozenset({"StorageWrite", "StorageDelete"}), frozenset({"Transaction"}))
ACCOUNT: Categories = (frozenset(), frozenset({"Transaction"}))
NEVER_COLLECTED = {"AppServiceHTTPLogs", "AppServiceConsoleLogs", "AppServiceAuthenticationLogs"}


def _avm_setting(settings: list[dict[str, Any]], workspace: str) -> Categories:
    (setting,) = settings
    assert setting["name"] == "to-log-analytics"
    assert setting["logAnalyticsDestinationType"] == "Dedicated"
    assert str(setting["workspaceResourceId"]) == workspace
    logs = frozenset(entry["category"] for entry in setting.get("logCategoriesAndGroups", []))
    metrics = frozenset(entry["category"] for entry in setting.get("metricCategories", []))
    return logs, metrics


def _direct_settings(deployment: Deployment, workspace: str) -> dict[str, Categories]:
    found: dict[str, Categories] = {}
    for setting in of_type(deployment, "Microsoft.Insights/diagnosticSettings"):
        properties = setting.properties
        assert properties["logAnalyticsDestinationType"] == "Dedicated"
        assert str(properties["workspaceId"]) == workspace
        assert all(
            entry["enabled"] is True
            for entry in properties.get("logs", []) + properties.get("metrics", [])
        )
        logs = frozenset(entry["category"] for entry in properties.get("logs", []))
        metrics = frozenset(entry["category"] for entry in properties.get("metrics", []))
        target = str(setting.field("scope")).split("/providers/", 1)[1]
        found[f"{target} {setting.name}"] = (logs, metrics)
    return found


def _avm_settings(
    deployment: Deployment, environment: str, workspace: str
) -> dict[str, Categories]:
    token = deployment.outputs()["DOCCHAT_RESOURCE_TOKEN"]
    site = avm_call(deployment, f"func-docchat-ingest-{environment}-{token}")
    plan = avm_call(deployment, f"asp-docchat-ingest-{environment}")
    api_site = avm_call(deployment, f"app-docchat-api-{environment}-{token}")
    api_plan = avm_call(deployment, f"asp-docchat-api-{environment}")
    vault = avm_call(deployment, f"kv-docchat-{environment}-{token}")
    data = avm_call(deployment, f"stdocchatdata{environment}{token}")
    host = avm_call(deployment, f"stdocchathost{environment}{token}")
    (database,) = avm_call(deployment, f"sql-docchat-{environment}-{token}")["databases"]
    search = avm_call(deployment, f"srch-docchat-{environment}-{token}")
    docintel = avm_call(deployment, f"di-docchat-{environment}-{token}")
    content_safety = avm_call(deployment, f"cs-docchat-{environment}-{token}")
    ai: dict[str, Categories] = {
        "search": _avm_setting(search["diagnosticSettings"], workspace),
        "content safety": _avm_setting(content_safety["diagnosticSettings"], workspace),
        "document intelligence": _avm_setting(docintel["diagnosticSettings"], workspace),
    }
    if environment != "load":
        openai = avm_call(deployment, f"oai-docchat-{environment}-{token}")
        ai["azure openai"] = _avm_setting(openai["diagnosticSettings"], workspace)
    else:
        stub_site = avm_call(deployment, f"app-docchat-stub-load-{token}")
        stub_plan = avm_call(deployment, "asp-docchat-stub-load")
        ai["model stub app"] = _avm_setting(stub_site["diagnosticSettings"], workspace)
        ai["model stub plan"] = _avm_setting(stub_plan["diagnosticSettings"], workspace)
    return ai | {
        "api app": _avm_setting(api_site["diagnosticSettings"], workspace),
        "api plan": _avm_setting(api_plan["diagnosticSettings"], workspace),
        "ingestion app": _avm_setting(site["diagnosticSettings"], workspace),
        "ingestion plan": _avm_setting(plan["diagnosticSettings"], workspace),
        "key vault": _avm_setting(vault["diagnosticSettings"], workspace),
        "data account": _avm_setting(data["diagnosticSettings"], workspace),
        "data blob": _avm_setting(data["blobServices"]["diagnosticSettings"], workspace),
        "data queue": _avm_setting(data["queueServices"]["diagnosticSettings"], workspace),
        "host account": _avm_setting(host["diagnosticSettings"], workspace),
        "host blob": _avm_setting(host["blobServices"]["diagnosticSettings"], workspace),
        "host queue": _avm_setting(host["queueServices"]["diagnosticSettings"], workspace),
        "host table": _avm_setting(host["tableServices"]["diagnosticSettings"], workspace),
        "sql docchat": _avm_setting(database["diagnosticSettings"], workspace),
    }


@pytest.mark.parametrize("environment", ENVIRONMENTS)
def test_t426_diagnostic_categories(main_deployment: MainFactory, environment: str) -> None:
    deployment = main_deployment(environment)
    token = deployment.outputs()["DOCCHAT_RESOURCE_TOKEN"]
    workspace = f"<log-docchat-{environment}.outputs.resourceId.value>"
    ai_service = (frozenset({"Audit", "RequestResponse"}), frozenset({"AllMetrics"}))
    ai = {
        "search": (frozenset({"OperationLogs"}), frozenset({"AllMetrics"})),
        "content safety": ai_service,
        "document intelligence": ai_service,
    }
    if environment != "load":
        ai["azure openai"] = ai_service
    else:
        ai["model stub app"] = (frozenset({"AppServicePlatformLogs"}), frozenset({"AllMetrics"}))
        ai["model stub plan"] = (frozenset(), frozenset({"AllMetrics"}))
    assert _avm_settings(deployment, environment, workspace) == ai | {
        "api app": (
            frozenset(
                {"AppServicePlatformLogs", "AppServiceAuditLogs", "AppServiceIPSecAuditLogs"}
            ),
            frozenset({"AllMetrics"}),
        ),
        "api plan": (frozenset(), frozenset({"AllMetrics"})),
        "ingestion app": (frozenset({"FunctionAppLogs"}), frozenset({"AllMetrics"})),
        "ingestion plan": (frozenset(), frozenset({"AllMetrics"})),
        "key vault": (frozenset({"AuditEvent"}), frozenset({"AllMetrics"})),
        "data account": ACCOUNT,
        "data blob": DATA_SERVICE,
        "data queue": DATA_SERVICE,
        "host account": ACCOUNT,
        "host blob": HOST_SERVICE,
        "host queue": HOST_SERVICE,
        "host table": HOST_SERVICE,
        "sql docchat": (
            frozenset({"SQLSecurityAuditEvents", "Errors", "Timeouts", "Blocks", "Deadlocks"}),
            frozenset({"Basic", "InstanceAndAppAdvanced"}),
        ),
    }
    cosmos = f"Microsoft.DocumentDB/databaseAccounts/cosmos-docchat-{environment}-{token}"
    expected_direct = {
        f"Microsoft.Sql/servers/sql-docchat-{environment}-{token}/databases/master sql-audit": (
            frozenset({"SQLSecurityAuditEvents", "DevOpsOperationsAudit"}),
            frozenset(),
        ),
        f"{cosmos} to-log-analytics": (
            frozenset({"DataPlaneRequests", "ControlPlaneRequests", "PartitionKeyRUConsumption"}),
            frozenset({"Requests"}),
        ),
    }
    if environment == "prod":
        # Scanning is on in prod only; dev turns it on only while the malware-scanning suite runs.
        key = (
            f"Microsoft.Storage/storageAccounts/stdocchatdata{environment}{token}"
            "/providers/Microsoft.Security/defenderForStorageSettings/current scan-results"
        )
        expected_direct[key] = (frozenset({"ScanResults"}), frozenset())
    if environment != "dev":
        # dev's single B1 instance has no autoscale setting.
        key = (
            f"Microsoft.Insights/autoscalesettings/autoscale-docchat-api-{environment}"
            " to-log-analytics"
        )
        expected_direct[key] = (frozenset({"AutoscaleScaleActionsLog"}), frozenset({"AllMetrics"}))
    assert _direct_settings(deployment, workspace) == expected_direct


def test_t426_scan_results_follow_the_scanning_switch(main_deployment: MainFactory) -> None:
    deployment = main_deployment(
        "dev", config={"storage": {"malwareScanning": True, "malwareScanCapGbPerMonth": 1000}}
    )
    workspace = "<log-docchat-dev.outputs.resourceId.value>"
    assert any(key.endswith(" scan-results") for key in _direct_settings(deployment, workspace))


def test_t426_excluded_categories_never_appear(main_template: dict[str, Any]) -> None:
    text = str(main_template)
    for category in NEVER_COLLECTED:
        assert category not in text


def test_t426_shared_resources_report_to_the_security_workspace(
    shared_deployment: SharedFactory,
) -> None:
    deployment = shared_deployment()
    (registry_setting,) = of_type(deployment, "Microsoft.Insights/diagnosticSettings")
    assert registry_setting.name == "registry-audit"
    assert registry_setting.properties["workspaceId"] == SECURITY_WORKSPACE_ID
    assert {entry["category"] for entry in registry_setting.properties["logs"]} == {
        "ContainerRegistryRepositoryEvents",
        "ContainerRegistryLoginEvents",
    }
    assert {entry["category"] for entry in registry_setting.properties["metrics"]} == {"AllMetrics"}
    vault_name = next(
        call.name
        for call in deployment.external_calls()
        if str(call.name).startswith("kv-docchat-tst-")
    )
    vault = avm_call(deployment, str(vault_name))
    assert _avm_setting(vault["diagnosticSettings"], SECURITY_WORKSPACE_ID) == (
        frozenset({"AuditEvent"}),
        frozenset({"AllMetrics"}),
    )
