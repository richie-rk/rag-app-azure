"""T-427, T-428 and T-430: retention, backup and the Cosmos DB module in the compiled template.

Sources: 80-data-model.md sections 3.1 and 3.13; 83-infrastructure.md sections 2.8, 2.10 and 2.18.
"""

from __future__ import annotations

from typing import Any

import pytest
from arm_template import Deployment
from compiled_support import ENVIRONMENTS, MainFactory, avm_call, of_type

AUDIT_TABLES = {
    "CDBDataPlaneRequests",
    "SQLSecurityAuditEvents",
    "AZKVAuditLogs",
    "StorageBlobLogs",
    "AppServiceAuditLogs",
}
SECONDS_PER_DAY = 86400


def _containers(deployment: Deployment) -> dict[str, dict[str, Any]]:
    containers = of_type(
        deployment, "Microsoft.DocumentDB/databaseAccounts/sqlDatabases/containers"
    )
    return {
        str(container.name).rsplit("/", 1)[-1]: container.properties for container in containers
    }


@pytest.mark.parametrize("environment", ENVIRONMENTS)
def test_t427_container_time_to_live_keeps_everything_by_default(
    main_deployment: MainFactory, environment: str
) -> None:
    containers = _containers(main_deployment(environment))
    assert {
        name: properties["resource"]["defaultTtl"] for name, properties in containers.items()
    } == {
        "conversations": -1,
        "memories": -1,
        "usage": -1,
    }


def test_t427_retention_days_become_the_default_ttl(main_deployment: MainFactory) -> None:
    deployment = main_deployment(
        "dev", config={"conversationRetentionDays": 30, "memoryRetentionDays": 400}
    )
    ttl = {
        name: properties["resource"]["defaultTtl"]
        for name, properties in _containers(deployment).items()
    }
    # usage documents carry their own ttl, so the container default never changes.
    assert ttl == {
        "conversations": 30 * SECONDS_PER_DAY,
        "memories": 400 * SECONDS_PER_DAY,
        "usage": -1,
    }


@pytest.mark.parametrize("environment", ENVIRONMENTS)
def test_t427_workspace_retention_and_audit_tables(
    main_deployment: MainFactory, environment: str
) -> None:
    workspace = avm_call(main_deployment(environment), f"log-docchat-{environment}")
    assert workspace["dataRetention"] == {"dev": 30, "prod": 90, "load": 30}[environment]
    assert workspace["dailyQuotaGb"] == {"dev": "2", "prod": "-1", "load": "-1"}[environment]
    if environment == "prod":
        assert {table["name"] for table in workspace["tables"]} == AUDIT_TABLES
        assert len(workspace["tables"]) == len(AUDIT_TABLES)
        for table in workspace["tables"]:
            assert (table["retentionInDays"], table["totalRetentionInDays"]) == (90, 365)
    else:
        assert workspace["tables"] == []


@pytest.mark.parametrize("environment", ENVIRONMENTS)
def test_t428_cosmos_continuous_backup(main_deployment: MainFactory, environment: str) -> None:
    (account,) = of_type(main_deployment(environment), "Microsoft.DocumentDB/databaseAccounts")
    tier = "Continuous30Days" if environment == "prod" else "Continuous7Days"
    assert account.properties["backupPolicy"] == {
        "type": "Continuous",
        "continuousModeProperties": {"tier": tier},
    }


@pytest.mark.parametrize("environment", ENVIRONMENTS)
def test_t428_sql_backup_retention(main_deployment: MainFactory, environment: str) -> None:
    deployment = main_deployment(environment)
    token = deployment.outputs()["DOCCHAT_RESOURCE_TOKEN"]
    server = avm_call(deployment, f"sql-docchat-{environment}-{token}")
    (database,) = server["databases"]
    assert database["name"] == "docchat"
    assert database["backupShortTermRetentionPolicy"] == {
        "retentionDays": {"dev": 7, "prod": 14, "load": 7}[environment],
        "diffBackupIntervalInHours": 12,
    }
    assert (
        database["requestedBackupStorageRedundancy"]
        == {"dev": "Local", "prod": "Geo", "load": "Local"}[environment]
    )
    assert "backupLongTermRetentionPolicy" not in database


def _contains_key(value: Any, key: str) -> bool:
    if isinstance(value, dict):
        return key in value or any(_contains_key(item, key) for item in value.values())
    if isinstance(value, list):
        return any(_contains_key(item, key) for item in value)
    return False


@pytest.mark.parametrize("environment", ENVIRONMENTS)
def test_t430_cosmos_module_uses_only_the_ga_api(
    main_deployment: MainFactory, environment: str
) -> None:
    cosmos = main_deployment(environment).module("cosmos")
    resources = cosmos.resources(include_existing=True)
    assert resources
    for resource in resources:
        assert resource.type.startswith("Microsoft.DocumentDB/databaseAccounts"), resource.type
        assert resource.api_version == "2026-03-15", resource.type
    assert not _contains_key(cosmos.template, "diagnosticLogSettings")
    roles = main_deployment(environment).module("cosmos-data-roles")
    for resource in roles.resources(include_existing=True):
        assert (resource.type, resource.api_version) in {
            ("Microsoft.DocumentDB/databaseAccounts", "2026-03-15"),
            ("Microsoft.DocumentDB/databaseAccounts/sqlRoleAssignments", "2026-03-15"),
        }


@pytest.mark.parametrize("environment", ENVIRONMENTS)
def test_t430_cosmos_account_settings(main_deployment: MainFactory, environment: str) -> None:
    (account,) = of_type(main_deployment(environment), "Microsoft.DocumentDB/databaseAccounts")
    properties = account.properties
    assert properties["capabilities"] == (
        [{"name": "EnableServerless"}] if environment == "dev" else []
    )
    (location,) = properties["locations"]
    assert location["isZoneRedundant"] is (environment != "dev")
    assert properties["disableLocalAuth"] is True
    assert properties["disableKeyBasedMetadataWriteAccess"] is True
    assert properties["networkAclBypass"] == "None"
    assert properties["publicNetworkAccess"] == "Disabled"
    containers = _containers(main_deployment(environment))
    assert containers["conversations"]["resource"]["partitionKey"] == {
        "paths": ["/userId", "/sessionId"],
        "kind": "MultiHash",
        "version": 2,
    }
    if environment == "dev":
        assert all("options" not in container for container in containers.values())
    else:
        maxima = {
            name: container["options"]["autoscaleSettings"]["maxThroughput"]
            for name, container in containers.items()
        }
        expected = (
            {"conversations": 4000, "memories": 1000, "usage": 1000}
            if environment == "prod"
            else {"conversations": 30000, "memories": 30000, "usage": 30000}
        )
        assert maxima == expected
