"""The bootstrap's security alerts in the compiled templates (T-502).

`infra/bootstrap/security.bicep` declares the eleven security alerts A44 to A54 of
`85-observability.md` section 6.6 in `rg-docchat-identity`, each routed to `ag-docchat-security`;
no environment template declares a security alert or a second security action group
(`83-infrastructure.md` sections 12.5 and 12.6).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Final

import pytest
from arm_template import Deployment, load_deployment
from compiled_support import ENVIRONMENTS, MainFactory, load_json, of_type

ACTIVITY_LOG_ALERT: Final = "Microsoft.Insights/activityLogAlerts"
LOG_SEARCH_ALERT: Final = "Microsoft.Insights/scheduledQueryRules"
ACTION_GROUP: Final = "Microsoft.Insights/actionGroups"
SECURITY_ACTION_GROUP: Final = "ag-docchat-security"

# 85-observability.md section 6.6. Activity Log alerts notify per operation with no mute and carry
# no severity property (the catalogue's value names a response class); each log search alert runs
# every 15 minutes over 30, muted for 30 minutes, at its catalogue severity.
ACTIVITY_LOG_ALERTS: Final = frozenset(
    {
        "docchat-alert-federated-credentials",
        "docchat-alert-key-access",
        "docchat-alert-governance",
    }
)
LOG_SEARCH_ALERTS: Final = {
    "docchat-alert-role-assignments": 2,
    "docchat-alert-cosmos-data-access": 1,
    "docchat-alert-diagnostic-settings": 2,
    "docchat-alert-app-registrations": 1,
    "docchat-alert-registry-writes": 2,
    "docchat-alert-jit-grant": 3,
    "docchat-alert-jit-self-grant": 1,
    "docchat-alert-jit-overdue": 2,
}
AUDIT_LOG_ALERTS: Final = frozenset(
    {
        "docchat-alert-app-registrations",
        "docchat-alert-jit-grant",
        "docchat-alert-jit-self-grant",
        "docchat-alert-jit-overdue",
    }
)


@pytest.fixture(scope="module")
def security(compiled_dir: Path) -> Deployment:
    template = load_json(compiled_dir / "bootstrap" / "security.json")
    return load_deployment(
        template,
        {
            "securityAlertEmails": ["security@example.org"],
            "jitGroupIds": ["00000000-0000-4000-8000-000000000001"],
        },
        resource_group="rg-docchat-identity",
    )


def _by_name(deployment: Deployment, resource_type: str) -> dict[str, Any]:
    return {str(resource.name): resource for resource in of_type(deployment, resource_type)}


def _routes_to_the_security_group(actions: Any) -> bool:
    groups = actions.get("actionGroups", actions) if isinstance(actions, dict) else actions
    ids = [group if isinstance(group, str) else group.get("actionGroupId", "") for group in groups]
    return len(ids) == 1 and str(ids[0]).endswith(f"/actionGroups/{SECURITY_ACTION_GROUP}")


def test_t502_the_bootstrap_declares_the_eleven_security_alerts(security: Deployment) -> None:
    activity = _by_name(security, ACTIVITY_LOG_ALERT)
    log_search = _by_name(security, LOG_SEARCH_ALERT)

    assert set(activity) == set(ACTIVITY_LOG_ALERTS)
    assert set(log_search) == set(LOG_SEARCH_ALERTS)
    for resource in (*activity.values(), *log_search.values()):
        assert resource.resource_group == "rg-docchat-identity"


def test_t502_activity_log_alerts_watch_successful_administrative_operations(
    security: Deployment,
) -> None:
    for name, resource in _by_name(security, ACTIVITY_LOG_ALERT).items():
        properties = resource.properties
        conditions = {
            (condition["field"], condition.get("equals"))
            for condition in properties["condition"]["allOf"]
            if "equals" in condition
        }
        assert ("category", "Administrative") in conditions, name
        assert ("status", "Succeeded") in conditions, name
        assert properties["enabled"] is True, name
        assert _routes_to_the_security_group(properties["actions"]), name


def test_t502_log_search_alerts_have_their_window_mute_and_severity(security: Deployment) -> None:
    for name, resource in _by_name(security, LOG_SEARCH_ALERT).items():
        properties = resource.properties
        assert properties["severity"] == LOG_SEARCH_ALERTS[name], name
        assert properties["windowSize"] == "PT30M", name
        assert properties["evaluationFrequency"] == "PT15M", name
        assert properties["muteActionsDuration"] == "PT30M", name
        assert properties["scopes"] == [
            security.resource_id(
                "Microsoft.OperationalInsights/workspaces", ["log-docchat-security"]
            )
        ], name
        query = properties["criteria"]["allOf"][0]["query"]
        table = "AuditLogs" if name in AUDIT_LOG_ALERTS else "AzureActivity"
        assert table in query, name
        assert _routes_to_the_security_group(properties["actions"]), name


def test_t502_one_security_action_group(security: Deployment) -> None:
    assert list(_by_name(security, ACTION_GROUP)) == [SECURITY_ACTION_GROUP]


@pytest.mark.parametrize("environment", ENVIRONMENTS)
def test_t502_no_environment_template_declares_a_security_alert(
    main_deployment: MainFactory, environment: str
) -> None:
    deployment = main_deployment(environment)
    security_names = set(ACTIVITY_LOG_ALERTS) | set(LOG_SEARCH_ALERTS)

    assert of_type(deployment, ACTIVITY_LOG_ALERT) == []
    alert_names = {str(resource.name) for resource in of_type(deployment, LOG_SEARCH_ALERT)}
    assert alert_names.isdisjoint(security_names)
    group_names = {str(resource.name) for resource in of_type(deployment, ACTION_GROUP)}
    assert SECURITY_ACTION_GROUP not in group_names
