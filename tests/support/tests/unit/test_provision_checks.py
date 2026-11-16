from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Final

import pytest

from docchat_testing.provision_checks import (
    SHARED_TOKEN,
    ExpectedName,
    budget_problems,
    expected_names,
    naming_problems,
    private_endpoint_targets,
)

REPOSITORY_ROOT: Final = Path(__file__).resolve().parents[4]
TOKEN: Final = "a1b2c3"
REGISTRY_TOKEN: Final = "z9y8x7"
GROUP: Final = "/subscriptions/0000/resourceGroups/rg-docchat-prod"
ACTION_GROUP: Final = f"{GROUP}/providers/microsoft.insights/actionGroups/ag-docchat-prod-ops"


def environment_file(environment: str) -> dict[str, Any]:
    path = REPOSITORY_ROOT / "infra" / "env" / f"{environment}.json"
    loaded: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return loaded


def concrete(entry: ExpectedName) -> str:
    """The one name an entry's pattern stands for, with the shared token filled in."""
    return re.sub(r"\\(.)", r"\1", entry.pattern.replace(SHARED_TOKEN, REGISTRY_TOKEN))


def listing(environment: str) -> list[dict[str, str]]:
    """What `az resource list` shows after a complete provision of `environment`, plus the
    resources whose names section 1.4 doesn't judge."""
    resources = [
        {"type": entry.resource_type, "name": concrete(entry)}
        for entry in expected_names(environment, TOKEN, environment_file(environment))
        if entry.required
    ]
    return [
        *resources,
        {"type": "Microsoft.Insights/workbooks", "name": "0f8e5c2a-3b4d-4e6f-8a9b-1c2d3e4f5a6b"},
        {
            "type": "Microsoft.Insights/scheduledQueryRules",
            "name": f"docchat-{environment}-ttft-p95",
        },
        {"type": "Microsoft.Network/privateDnsZones", "name": "privatelink.documents.azure.com"},
        {"type": "Microsoft.EventGrid/systemTopics", "name": "some-topic-defender-created"},
    ]


@pytest.mark.parametrize("environment", ["dev", "prod", "load"])
def test_t437_a_complete_provision_has_no_naming_problem(environment: str) -> None:
    expected = expected_names(environment, TOKEN, environment_file(environment))

    assert naming_problems(listing(environment), expected) == []


@pytest.mark.parametrize("environment", ["dev", "prod", "load"])
def test_t437_every_environment_has_thirteen_private_endpoints(environment: str) -> None:
    targets = private_endpoint_targets(environment, TOKEN, environment_file(environment))

    assert len(targets) == 13
    assert len(set(targets)) == 13


def test_t437_prod_names_follow_section_1_4() -> None:
    names = {
        (entry.resource_type, concrete(entry))
        for entry in expected_names("prod", TOKEN, environment_file("prod"))
        if entry.required
    }

    for expected in (
        ("microsoft.web/staticsites", "stapp-docchat-prod"),
        ("microsoft.web/sites", f"app-docchat-api-prod-{TOKEN}"),
        ("microsoft.web/sites", f"func-docchat-ingest-prod-{TOKEN}"),
        ("microsoft.sql/servers/databases", f"sql-docchat-prod-{TOKEN}/docchat"),
        ("microsoft.storage/storageaccounts", f"stdocchatdataprod{TOKEN}"),
        ("microsoft.cognitiveservices/accounts", f"oai-docchat-prod-{TOKEN}"),
        ("microsoft.keyvault/vaults", f"kv-docchat-prod-{TOKEN}"),
        ("microsoft.insights/autoscalesettings", "autoscale-docchat-api-prod"),
        ("microsoft.network/privateendpoints", f"pe-crdocchat{REGISTRY_TOKEN}-registry"),
        ("microsoft.network/networkinterfaces", f"nic-pe-cosmos-docchat-prod-{TOKEN}-Sql"),
    ):
        assert expected in names, expected
    assert not any("stub" in name for _, name in names)


def test_t437_load_has_the_model_stub_and_no_azure_openai() -> None:
    names = {concrete(e) for e in expected_names("load", TOKEN, environment_file("load"))}

    assert {"asp-docchat-stub-load", f"app-docchat-stub-load-{TOKEN}", "id-docchat-stub-load"} <= (
        names
    )
    assert f"pe-app-docchat-stub-load-{TOKEN}-sites" in names
    assert f"oai-docchat-load-{TOKEN}" not in names


def test_t437_dev_has_no_autoscale_setting() -> None:
    expected = expected_names("dev", TOKEN, environment_file("dev"))
    extra = {"type": "Microsoft.Insights/autoscalesettings", "name": "autoscale-docchat-api-dev"}

    assert naming_problems([*listing("dev"), extra], expected) == [
        refused("Microsoft.Insights/autoscalesettings autoscale-docchat-api-dev")
    ]


def refused(resource: str) -> str:
    return f"{resource}: not a name section 1.4 gives this environment"


def test_t437_a_resource_main_bicep_never_declares_is_named() -> None:
    expected = expected_names("prod", TOKEN, environment_file("prod"))
    stray = {"type": "Microsoft.Storage/storageAccounts", "name": "stmanualcopy01"}

    problems = naming_problems([*listing("prod"), stray], expected)

    assert problems == [refused("Microsoft.Storage/storageAccounts stmanualcopy01")]


def test_t437_a_name_from_another_environment_or_token_is_refused() -> None:
    expected = expected_names("prod", TOKEN, environment_file("prod"))
    resources = [
        {"type": "Microsoft.KeyVault/vaults", "name": "kv-docchat-dev-a1b2c3"},
        {"type": "Microsoft.Web/sites", "name": "app-docchat-api-prod-ffffff"},
    ]

    problems = naming_problems(resources, expected)

    assert problems[:2] == [
        refused("Microsoft.KeyVault/vaults kv-docchat-dev-a1b2c3"),
        refused("Microsoft.Web/sites app-docchat-api-prod-ffffff"),
    ]


def test_t437_a_missing_resource_is_named() -> None:
    expected = expected_names("prod", TOKEN, environment_file("prod"))
    resources = [r for r in listing("prod") if r["name"] != f"srch-docchat-prod-{TOKEN}"]

    assert naming_problems(resources, expected) == [
        rf"microsoft.search/searchservices srch\-docchat\-prod\-{TOKEN}: missing"
    ]


def test_t437_the_fallback_1_resources_and_sql_master_may_exist() -> None:
    expected = expected_names("prod", TOKEN, environment_file("prod"))
    optional = [
        {"type": "Microsoft.Sql/servers/databases", "name": f"sql-docchat-prod-{TOKEN}/master"},
        {"type": "Microsoft.Network/natGateways", "name": "ng-docchat-prod"},
        {"type": "Microsoft.Network/publicIPAddresses", "name": "pip-docchat-prod-nat"},
        {"type": "Microsoft.Network/privateEndpoints", "name": f"pe-stdocchathostprod{TOKEN}-file"},
    ]

    assert naming_problems([*listing("prod"), *optional], expected) == []


def test_t437_types_are_compared_without_case() -> None:
    entry = ExpectedName("microsoft.web/sites", re.escape("app-docchat-api-prod-a1b2c3"))

    assert entry.matches("Microsoft.Web/sites", "APP-docchat-api-prod-a1b2c3")
    assert not entry.matches("Microsoft.Web/serverFarms", "app-docchat-api-prod-a1b2c3")


def budget(**changes: Any) -> dict[str, Any]:
    notifications = {
        key: {
            "enabled": True,
            "operator": "GreaterThan",
            "threshold": threshold,
            "thresholdType": "Actual",
            "contactGroups": [ACTION_GROUP],
            "contactEmails": [],
            "locale": "en-us",
        }
        for key, threshold in (("actual-50", 50), ("actual-80", 80), ("actual-100", 100))
    }
    properties: dict[str, Any] = {
        "category": "Cost",
        "amount": 2200,
        "timeGrain": "Monthly",
        "timePeriod": {"startDate": "2026-11-01T00:00:00Z"},
        "notifications": notifications,
    }
    properties.update(changes)
    return {"name": "budget-docchat-prod", "properties": properties}


def test_t472_the_kept_budget_passes() -> None:
    assert budget_problems(budget(), start_date="2026-11-01", action_group_id=ACTION_GROUP) == []


def test_t472_a_moved_start_date_fails() -> None:
    problems = budget_problems(budget(), start_date="2026-10-01", action_group_id=ACTION_GROUP)

    assert problems == ["startDate 2026-11-01, not 2026-10-01"]


def test_t472_a_missing_notification_fails() -> None:
    kept = budget()
    del kept["properties"]["notifications"]["actual-80"]

    problems = budget_problems(kept, start_date="2026-11-01", action_group_id=ACTION_GROUP)

    assert problems == [
        "notifications ['actual-100', 'actual-50'], not ['actual-100', 'actual-50', 'actual-80']"
    ]


def test_t472_notifications_go_to_the_ops_action_group_only() -> None:
    kept = budget()
    kept["properties"]["notifications"]["actual-100"]["contactGroups"] = [
        ACTION_GROUP.upper(),
        f"{GROUP}/providers/microsoft.insights/actionGroups/elsewhere",
    ]

    problems = budget_problems(kept, start_date="2026-11-01", action_group_id=ACTION_GROUP)

    assert len(problems) == 1
    assert problems[0].startswith("actual-100: contact groups")


def test_t472_a_changed_threshold_fails() -> None:
    kept = budget()
    kept["properties"]["notifications"]["actual-50"]["threshold"] = 60

    problems = budget_problems(kept, start_date="2026-11-01", action_group_id=ACTION_GROUP)

    assert problems == [
        "actual-50: (True, 'GreaterThan', 60, 'Actual'), not enabled GreaterThan 50 Actual"
    ]


def test_t472_notification_keys_are_compared_without_case() -> None:
    kept = budget()
    notifications = kept["properties"]["notifications"]
    kept["properties"]["notifications"] = {
        key.upper(): value for key, value in notifications.items()
    }

    assert budget_problems(kept, start_date="2026-11-01", action_group_id=ACTION_GROUP) == []


def test_t472_an_absent_budget_fails() -> None:
    problems = budget_problems({}, start_date="2026-11-01", action_group_id=ACTION_GROUP)

    assert problems[0] == "startDate missing, not 2026-11-01"
