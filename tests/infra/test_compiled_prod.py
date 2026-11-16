"""`prod` at its parameters, its locks and its deletion-deny assignment in the compiled templates
(`90-build-order.md` section 3.7, "Infrastructure"; `83-infrastructure.md` sections 2, 12.1 to
12.3).

- Each environment's column of `83-infrastructure.md` section 2.1 in the compiled `main.json`,
  where the other compiled-template tests don't already hold it: the Static Web Apps plan, the
  ingestion plan, the SQL database, both storage SKUs, the environment Key Vault's protection and
  Defender for Storage with the scanner's resource-instance rule (malware scanning on in `prod`
  only, section 2.11).
- The six `docchat-protect` locks of section 12.3 are `scripts/bootstrap/lock_prod.py`'s, run by
  the bootstrap operator after `prod`'s first provision; here, the script locks exactly the table's
  six resources, and each is one resource of the compiled `prod` template, so the script finds it.
  No template a pipeline deploys declares a lock or a policy assignment: a lock or an assignment
  belongs where no pipeline identity can remove it.
- The deny policies of sections 12.1 and 12.2 in the compiled bootstrap template
  `bootstrap/policies.json`, the deletion-deny assignment `docchat-deny-store-deletion` at
  `rg-docchat-prod` alone among them.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Final

import pytest
from arm_template import Deployment, load_deployment
from compiled_support import (
    ENVIRONMENTS,
    REPO_ROOT,
    MainFactory,
    SharedFactory,
    avm_call,
    env_file,
    load_json,
    of_type,
)

# scripts/bootstrap holds command-line scripts, not a package, so its folder goes on the path.
BOOTSTRAP_SCRIPTS = str(REPO_ROOT / "scripts" / "bootstrap")
if BOOTSTRAP_SCRIPTS not in sys.path:
    sys.path.insert(0, BOOTSTRAP_SCRIPTS)

import lock_prod  # noqa: E402

from docchat_testing.provision_checks import expected_names  # noqa: E402

GIB: Final = 1073741824
SCANNER_ID: Final = (
    "/subscriptions/11111111-2222-3333-4444-555555555555"
    "/providers/Microsoft.Security/datascanners/StorageDataScanner"
)

# Section 2.1's columns for what this module checks: dev / prod / load.
INVENTORY: Final[dict[str, dict[str, Any]]] = {
    "dev": {
        "web": "Free",
        "ingestion": ("EP1", 1, 2),
        "sql": ("GP_S_Gen5_2", 32, "Local"),
        "storage": "Standard_LRS",
        "vault": (False, 7),
    },
    "prod": {
        "web": "Standard",
        "ingestion": ("EP2", 1, 4),
        "sql": ("GP_Gen5_2", 64, "Geo"),
        "storage": "Standard_ZRS",
        "vault": (True, 90),
    },
    "load": {
        "web": "Free",
        "ingestion": ("EP2", 2, 4),
        "sql": ("GP_Gen5_2", 32, "Local"),
        "storage": "Standard_ZRS",
        "vault": (False, 7),
    },
}

# Section 12.3's table: (resource type, name prefix in rg-docchat-prod, the locked child).
LOCKS: Final = {
    ("Microsoft.Sql/servers", "sql-docchat-prod-", "/databases/docchat"),
    ("Microsoft.DocumentDB/databaseAccounts", "cosmos-docchat-prod-", "/sqlDatabases/docchat"),
    ("Microsoft.Storage/storageAccounts", "stdocchatdataprod", ""),
    ("Microsoft.KeyVault/vaults", "kv-docchat-prod-", ""),
    ("Microsoft.Search/searchServices", "srch-docchat-prod-", ""),
    ("Microsoft.OperationalInsights/workspaces", "log-docchat-prod", ""),
}

# Section 12.1's table: assignment to the groups it applies to ("environment" is each environment
# group; "prod" and "shared" name rg-docchat-prod and rg-docchat-shared).
POLICY_SCOPES: Final = {
    "docchat-deny-storage-shared-key": {"environment"},
    "docchat-deny-storage-public-access": {"environment", "shared"},
    "docchat-deny-cosmos-local-auth": {"environment"},
    "docchat-deny-cosmos-azure-datacenters": {"environment"},
    "docchat-deny-ai-key-access": {"environment"},
    "docchat-deny-search-local-auth": {"environment"},
    "docchat-deny-keyvault-access-policies": {"environment", "shared"},
    "docchat-deny-keyvault-purge": {"prod", "shared"},
    "docchat-deny-sql-local-auth": {"environment"},
    "docchat-deny-sql-local-auth-create": {"environment"},
    "docchat-deny-federated-credentials": {"environment"},
    "docchat-deny-store-deletion": {"prod"},
    "docchat-deny-acr-admin": {"shared"},
    "docchat-deny-acr-anonymous": {"shared"},
    "docchat-deny-acr-tokens": {"shared"},
    "docchat-deny-acr-open-network": {"shared"},
}


def _token(deployment: Deployment) -> str:
    return str(deployment.outputs()["DOCCHAT_RESOURCE_TOKEN"])


@pytest.mark.parametrize("environment", ENVIRONMENTS)
def test_static_web_app_plan(main_deployment: MainFactory, environment: str) -> None:
    site = avm_call(main_deployment(environment), f"stapp-docchat-{environment}")
    assert site["sku"] == INVENTORY[environment]["web"]


@pytest.mark.parametrize("environment", ENVIRONMENTS)
def test_ingestion_plan_and_scale(main_deployment: MainFactory, environment: str) -> None:
    deployment = main_deployment(environment)
    sku, always_ready, burst = INVENTORY[environment]["ingestion"]
    plan = avm_call(deployment, f"asp-docchat-ingest-{environment}")
    assert (plan["skuName"], plan["maximumElasticWorkerCount"], plan["zoneRedundant"]) == (
        sku,
        burst,
        False,
    )
    site = avm_call(deployment, f"func-docchat-ingest-{environment}-{_token(deployment)}")
    config = site["siteConfig"]
    assert config["minimumElasticInstanceCount"] == always_ready
    assert config["functionAppScaleLimit"] == burst


@pytest.mark.parametrize("environment", ENVIRONMENTS)
def test_sql_database_at_its_parameters(main_deployment: MainFactory, environment: str) -> None:
    deployment = main_deployment(environment)
    sku, size_gb, backup = INVENTORY[environment]["sql"]
    server = avm_call(deployment, f"sql-docchat-{environment}-{_token(deployment)}")
    (database,) = server["databases"]
    assert database["sku"]["name"] == "_".join(sku.split("_")[:-1])
    assert database["sku"]["capacity"] == int(sku.rsplit("_", 1)[1])
    assert database["maxSizeBytes"] == size_gb * GIB
    assert database["requestedBackupStorageRedundancy"] == backup
    assert database["zoneRedundant"] is False
    # Provisioned prod pays for its licence with the database; serverless dev auto-pauses.
    assert database.get("licenseType") == ("LicenseIncluded" if environment == "prod" else None)
    if environment == "dev":
        assert (database["autoPauseDelay"], database["minCapacity"]) == (15, "0.5")
    else:
        assert "autoPauseDelay" not in database


def test_prod_sql_zone_redundancy_is_its_parameter(main_deployment: MainFactory) -> None:
    deployment = main_deployment(
        "prod", config={"sql": {**env_file("prod")["sql"], "zoneRedundant": True}}
    )
    server = avm_call(deployment, f"sql-docchat-prod-{_token(deployment)}")
    (database,) = server["databases"]
    assert database["zoneRedundant"] is True


@pytest.mark.parametrize("environment", ENVIRONMENTS)
def test_storage_skus_follow_the_environment(
    main_deployment: MainFactory, environment: str
) -> None:
    deployment = main_deployment(environment)
    token = _token(deployment)
    for account in (f"stdocchatdata{environment}{token}", f"stdocchathost{environment}{token}"):
        assert avm_call(deployment, account)["skuName"] == INVENTORY[environment]["storage"]


@pytest.mark.parametrize("environment", ENVIRONMENTS)
def test_environment_key_vault_protection(main_deployment: MainFactory, environment: str) -> None:
    deployment = main_deployment(environment)
    vault = avm_call(deployment, f"kv-docchat-{environment}-{_token(deployment)}")
    purge_protection, soft_delete_days = INVENTORY[environment]["vault"]
    assert vault["sku"] == "standard"
    assert vault["enableSoftDelete"] is True
    assert vault["enablePurgeProtection"] is purge_protection
    assert vault["softDeleteRetentionInDays"] == soft_delete_days


@pytest.mark.parametrize("environment", ENVIRONMENTS)
def test_defender_for_storage_scans_uploads_only_where_scanning_is_on(
    main_deployment: MainFactory, environment: str
) -> None:
    deployment = main_deployment(environment)
    scanning = environment == "prod"
    assert env_file(environment)["storage"]["malwareScanning"] is scanning
    (settings,) = of_type(deployment, "Microsoft.Security/defenderForStorageSettings")
    assert settings.name == "current"
    assert settings.properties == {
        "isEnabled": scanning,
        "malwareScanning": {
            "onUpload": {
                "isEnabled": scanning,
                "capGBPerMonth": 1000,
                "filters": {
                    "excludeBlobsWithPrefix": ["renditions/", "ingestion-work/"],
                    "excludeBlobsWithSuffix": ["/converted.pdf"],
                },
            },
            "blobScanResultsOptions": "blobIndexTags",
        },
        "sensitiveDataDiscovery": {"isEnabled": False},
        "overrideSubscriptionLevelSettings": True,
    }
    account = avm_call(deployment, f"stdocchatdata{environment}{_token(deployment)}")
    rules = account["networkAcls"]["resourceAccessRules"]
    tenant = env_file(environment)["tenantId"]
    assert rules == ([{"tenantId": tenant, "resourceId": SCANNER_ID}] if scanning else [])


def test_the_scan_cap_is_the_environment_files(main_deployment: MainFactory) -> None:
    storage = {**env_file("prod")["storage"], "malwareScanCapGbPerMonth": 250}
    deployment = main_deployment("prod", config={"storage": storage})
    (settings,) = of_type(deployment, "Microsoft.Security/defenderForStorageSettings")
    assert settings.properties["malwareScanning"]["onUpload"]["capGBPerMonth"] == 250


@pytest.mark.parametrize("environment", ["dev", "prod"])
def test_the_deployed_naming_check_expects_the_private_endpoints_main_bicep_declares(
    main_deployment: MainFactory, environment: str
) -> None:
    # T-437 judges every private endpoint and its network interface by these names after each
    # provision (docchat_testing.provision_checks). load's model-stub endpoint joins with the
    # load environment's extras.
    deployment = main_deployment(environment)
    endpoints = deployment.module("private-endpoints").parameters["endpoints"]
    declared = {f"pe-{endpoint['targetName']}-{endpoint['groupId']}" for endpoint in endpoints}
    expected = [
        entry
        for entry in expected_names(environment, _token(deployment), env_file(environment))
        if entry.resource_type == "microsoft.network/privateendpoints" and entry.required
    ]
    assert len(declared) == len(expected) == 13
    for name in declared:
        assert (
            sum(entry.matches("Microsoft.Network/privateEndpoints", name) for entry in expected)
            == 1
        )


def test_lock_prod_locks_exactly_the_six_resources_of_section_12_3() -> None:
    assert {(kind, prefix, child) for _, kind, prefix, child in lock_prod.LOCKED} == LOCKS
    assert (lock_prod.LOCK_NAME, lock_prod.LOCK_LEVEL) == ("docchat-protect", "CanNotDelete")
    assert lock_prod.STORE_DELETION_ASSIGNMENT == "docchat-deny-store-deletion"
    # No lock goes on the SQL server or the Cosmos DB account themselves (section 12.3).
    parents = {kind for kind, _, child in LOCKS if child}
    assert parents == {"Microsoft.Sql/servers", "Microsoft.DocumentDB/databaseAccounts"}


def test_each_locked_resource_is_one_resource_of_the_prod_template(
    main_deployment: MainFactory,
) -> None:
    deployment = main_deployment("prod")
    token = _token(deployment)
    modules = [str(call.name) for call in deployment.external_calls()]
    (cosmos,) = of_type(deployment, "Microsoft.DocumentDB/databaseAccounts")
    (cosmos_database,) = of_type(deployment, "Microsoft.DocumentDB/databaseAccounts/sqlDatabases")
    declared = {
        "Microsoft.Sql/servers": [name for name in modules if name.startswith("sql-docchat-")],
        "Microsoft.DocumentDB/databaseAccounts": [str(cosmos.name)],
        "Microsoft.Storage/storageAccounts": [n for n in modules if n.startswith("stdocchat")],
        "Microsoft.KeyVault/vaults": [name for name in modules if name.startswith("kv-docchat-")],
        "Microsoft.Search/searchServices": [n for n in modules if n.startswith("srch-docchat-")],
        "Microsoft.OperationalInsights/workspaces": [
            name for name in modules if name.startswith("log-docchat-")
        ],
    }
    for kind, prefix, _ in LOCKS:
        matching = [name for name in declared[kind] if name.startswith(prefix)]
        assert len(matching) == 1, (kind, prefix, matching)
    server = avm_call(deployment, f"sql-docchat-prod-{token}")
    assert [database["name"] for database in server["databases"]] == ["docchat"]
    assert str(cosmos_database.name) == f"cosmos-docchat-prod-{token}/docchat"


@pytest.mark.parametrize("environment", ENVIRONMENTS)
def test_no_pipeline_template_deploys_a_lock_or_a_policy_assignment(
    main_deployment: MainFactory, shared_deployment: SharedFactory, environment: str
) -> None:
    # The Azure Verified Modules declare an optional lock each, deployed only when their `lock`
    # parameter is set, so the check reads what each deployment actually deploys.
    for deployment in (main_deployment(environment), shared_deployment()):
        assert of_type(deployment, "Microsoft.Authorization/locks") == []
        assert of_type(deployment, "Microsoft.Authorization/policyAssignments") == []


@pytest.fixture(scope="module")
def policies(compiled_dir: Path) -> Deployment:
    """The bootstrap's policies step for a subscription holding all three environment groups and
    rg-docchat-shared (the default of section 1.2)."""
    template = load_json(compiled_dir / "bootstrap" / "policies.json")
    return load_deployment(
        template, {"environmentNames": list(ENVIRONMENTS)}, resource_group="subscription"
    )


def _assignments(policies: Deployment) -> dict[str, dict[str, dict[str, Any]]]:
    """Each resource group's assignments by name."""
    return {
        str(module.resource_group): {
            str(assignment.name): assignment.properties for assignment in module.child.resources()
        }
        for module in policies.modules()
    }


def test_the_deny_policies_sit_at_the_groups_of_section_12_1(policies: Deployment) -> None:
    groups = {
        f"rg-docchat-{environment}": {"environment"}
        | ({"prod"} if environment == "prod" else set())
        for environment in ENVIRONMENTS
    } | {"rg-docchat-shared": {"shared"}}
    expected = {
        group: {name for name, scopes in POLICY_SCOPES.items() if scopes & applies}
        for group, applies in groups.items()
    }
    assert {group: set(names) for group, names in _assignments(policies).items()} == expected


def test_every_assignment_denies_and_names_its_section(policies: Deployment) -> None:
    for assignments in _assignments(policies).values():
        for name, properties in assignments.items():
            assert properties["enforcementMode"] == "Default", name
            (message,) = properties["nonComplianceMessages"]
            assert message["message"].startswith(f"{name}: "), name
            # Section 12.1, for PSRule's Azure.Policy.AssignmentDescriptors and -AssignedBy.
            assert properties["displayName"], name
            assert "83-infrastructure.md section" in properties["description"], name
            assert properties["metadata"] == {"assignedBy": "DocChat bootstrap"}, name
            custom = name in {"docchat-deny-federated-credentials", "docchat-deny-store-deletion"}
            # The custom definitions fix their effect in the rule and take no parameter.
            assert properties["parameters"] == ({} if custom else {"effect": {"value": "Deny"}})


def test_the_deletion_deny_assignment_protects_the_prod_stores(policies: Deployment) -> None:
    assignment = _assignments(policies)["rg-docchat-prod"]["docchat-deny-store-deletion"]
    assert assignment["policyDefinitionId"] == (
        "/subscriptions/11111111-2222-3333-4444-555555555555"
        "/providers/Microsoft.Authorization/policyDefinitions/docchat-deny-store-deletion"
    )
    assert "section 12.3" in assignment["nonComplianceMessages"][0]["message"]
    for group in ("rg-docchat-dev", "rg-docchat-load", "rg-docchat-shared"):
        assert "docchat-deny-store-deletion" not in _assignments(policies)[group]

    definition = next(
        resource
        for resource in policies.resources()
        if resource.type == "Microsoft.Authorization/policyDefinitions"
        and resource.name == "docchat-deny-store-deletion"
    )
    properties = definition.properties
    assert definition.api_version == "2026-07-01"
    assert (properties["policyType"], properties["mode"]) == ("Custom", "Indexed")
    assert properties["policyRule"] == {
        "if": {
            "field": "type",
            "in": ["Microsoft.Sql/servers", "Microsoft.DocumentDB/databaseAccounts"],
        },
        "then": {
            "effect": "denyAction",
            "details": {"actionNames": ["delete"], "cascadeBehaviors": {"resourceGroup": "deny"}},
        },
    }


def test_the_federated_credential_definition_denies_every_app_identity_credential(
    policies: Deployment,
) -> None:
    definition = next(
        resource
        for resource in policies.resources()
        if resource.type == "Microsoft.Authorization/policyDefinitions"
        and resource.name == "docchat-deny-federated-credentials"
    )
    credential = "Microsoft.ManagedIdentity/userAssignedIdentities/federatedIdentityCredentials"
    assert definition.properties["mode"] == "All"
    assert definition.properties["policyRule"] == {
        "if": {"field": "type", "equals": credential},
        "then": {"effect": "deny"},
    }


def test_both_custom_definitions_carry_their_descriptors(policies: Deployment) -> None:
    # Section 12.2, for PSRule's Azure.Policy.Descriptors: display name, description, category.
    definitions = {
        str(resource.name): resource.properties
        for resource in policies.resources()
        if resource.type == "Microsoft.Authorization/policyDefinitions"
    }
    assert set(definitions) == {"docchat-deny-federated-credentials", "docchat-deny-store-deletion"}
    for name, properties in definitions.items():
        assert properties["displayName"].startswith("DocChat: "), name
        assert "83-infrastructure.md section" in properties["description"], name
        assert properties["metadata"] == {"category": "DocChat"}, name


def test_a_subscription_without_prod_assigns_no_deletion_deny(compiled_dir: Path) -> None:
    template = load_json(compiled_dir / "bootstrap" / "policies.json")
    deployment = load_deployment(
        template, {"environmentNames": ["dev", "load"]}, resource_group="subscription"
    )
    assert all(
        "docchat-deny-store-deletion" not in assignments
        for assignments in _assignments(deployment).values()
    )
