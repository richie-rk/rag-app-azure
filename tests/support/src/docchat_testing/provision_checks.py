"""What the deployed checks after each provision compare (`84-testing.md` sections 5.9 and 8.3).

- The resource naming check T-437: `expected_names(environment, token, config)` lists, for each
  resource type that `83-infrastructure.md` section 1.4 names, the names `main.bicep` gives one
  environment with its environment file `config`; `naming_problems` compares an
  `az resource list --resource-group rg-docchat-<env>` answer with them. A resource of a listed
  type whose name isn't one of them is a resource that didn't come from `main.bicep` and that
  file, and a missing one is a provision that didn't finish. Types section 1.4 names no pattern
  for aren't judged by name: workbooks (their names are GUIDs), alert rules (named by
  `85-observability.md`), private DNS zones and their links (named by Private Link), and what the
  platform adds itself, such as Defender's Event Grid topic.
- The budget persistence check T-472: `budget_problems` compares the budget a provision left
  (`budget-docchat-<env>`, read through Resource Manager) with section 12.7: the start date the
  `preprovision` hook read before provisioning (`DOCCHAT_BUDGET_START_DATE`), and the three
  notifications to the environment's ops action group.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Final

SHARED_TOKEN: Final = "[a-z0-9]{6}"
"""`take(uniqueString(subscription().id, 'shared'), 6)`, which only Azure computes."""

BUDGET_THRESHOLDS: Final = {"actual-50": 50, "actual-80": 80, "actual-100": 100}

NAMED_TYPES: Final = frozenset(
    resource_type.lower()
    for resource_type in (
        "Microsoft.Web/staticSites",
        "Microsoft.Web/serverFarms",
        "Microsoft.Web/sites",
        "Microsoft.ManagedIdentity/userAssignedIdentities",
        "Microsoft.Sql/servers",
        "Microsoft.Sql/servers/databases",
        "Microsoft.DocumentDB/databaseAccounts",
        "Microsoft.Storage/storageAccounts",
        "Microsoft.Search/searchServices",
        "Microsoft.CognitiveServices/accounts",
        "Microsoft.KeyVault/vaults",
        "Microsoft.OperationalInsights/workspaces",
        "Microsoft.Insights/components",
        "Microsoft.Network/virtualNetworks",
        "Microsoft.Network/networkSecurityGroups",
        "Microsoft.Network/natGateways",
        "Microsoft.Network/publicIPAddresses",
        "Microsoft.Network/privateEndpoints",
        "Microsoft.Network/networkInterfaces",
        "Microsoft.Insights/actionGroups",
        "Microsoft.Insights/autoscaleSettings",
    )
)
"""Every environment resource type section 1.4 gives a pattern for, whether or not this
environment declares one; a resource of such a type is judged by its name."""


@dataclass(frozen=True)
class ExpectedName:
    """One resource the provision leaves: its type (lower case) and a full-match pattern of its
    name; an optional one (content-share fallback 1, SQL's `master`) may be absent."""

    resource_type: str
    pattern: str
    required: bool = True

    def matches(self, resource_type: str, name: str) -> bool:
        return resource_type.lower() == self.resource_type and bool(
            re.fullmatch(self.pattern, name, re.IGNORECASE)
        )


def _literal(resource_type: str, name: str, *, required: bool = True) -> ExpectedName:
    return ExpectedName(resource_type.lower(), re.escape(name), required)


def private_endpoint_targets(
    environment: str, token: str, config: Mapping[str, Any]
) -> list[tuple[str, str]]:
    """`(target name pattern, group ID)` of every private endpoint `main.bicep` declares
    (`83-infrastructure.md` section 4.4): 13 with Azure OpenAI in `dev` and `prod`; in `load`
    the model stub's site takes Azure OpenAI's place."""
    data = re.escape(f"stdocchatdata{environment}{token}")
    host = re.escape(f"stdocchathost{environment}{token}")
    targets = [
        (re.escape(f"sql-docchat-{environment}-{token}"), "sqlServer"),
        (re.escape(f"cosmos-docchat-{environment}-{token}"), "Sql"),
        (data, "blob"),
        (data, "queue"),
        (host, "blob"),
        (host, "queue"),
        (host, "table"),
        (re.escape(f"kv-docchat-{environment}-{token}"), "vault"),
        (f"crdocchat{SHARED_TOKEN}", "registry"),
        (re.escape(f"srch-docchat-{environment}-{token}"), "searchService"),
        (re.escape(f"cs-docchat-{environment}-{token}"), "account"),
        (re.escape(f"di-docchat-{environment}-{token}"), "account"),
    ]
    if config["openAi"]["enabled"]:
        targets.append((re.escape(f"oai-docchat-{environment}-{token}"), "account"))
    if config["modelStub"]["enabled"]:
        targets.append((re.escape(f"app-docchat-stub-load-{token}"), "sites"))
    return targets


def expected_names(environment: str, token: str, config: Mapping[str, Any]) -> list[ExpectedName]:
    """The names of section 1.4 that one environment's provision leaves in its group."""
    env, tok = environment, token
    names = [
        _literal("Microsoft.Web/staticSites", f"stapp-docchat-{env}"),
        _literal("Microsoft.Web/serverFarms", f"asp-docchat-api-{env}"),
        _literal("Microsoft.Web/serverFarms", f"asp-docchat-ingest-{env}"),
        _literal("Microsoft.Web/sites", f"app-docchat-api-{env}-{tok}"),
        _literal("Microsoft.Web/sites", f"func-docchat-ingest-{env}-{tok}"),
        _literal("Microsoft.ManagedIdentity/userAssignedIdentities", f"id-docchat-api-{env}"),
        _literal("Microsoft.ManagedIdentity/userAssignedIdentities", f"id-docchat-ingest-{env}"),
        _literal("Microsoft.Sql/servers", f"sql-docchat-{env}-{tok}"),
        _literal("Microsoft.Sql/servers/databases", f"sql-docchat-{env}-{tok}/docchat"),
        # The server's system database, which the privileged audit's diagnostic setting sits on.
        _literal(
            "Microsoft.Sql/servers/databases", f"sql-docchat-{env}-{tok}/master", required=False
        ),
        _literal("Microsoft.DocumentDB/databaseAccounts", f"cosmos-docchat-{env}-{tok}"),
        _literal("Microsoft.Storage/storageAccounts", f"stdocchatdata{env}{tok}"),
        _literal("Microsoft.Storage/storageAccounts", f"stdocchathost{env}{tok}"),
        _literal("Microsoft.Search/searchServices", f"srch-docchat-{env}-{tok}"),
        _literal("Microsoft.CognitiveServices/accounts", f"cs-docchat-{env}-{tok}"),
        _literal("Microsoft.CognitiveServices/accounts", f"di-docchat-{env}-{tok}"),
        _literal("Microsoft.KeyVault/vaults", f"kv-docchat-{env}-{tok}"),
        _literal("Microsoft.OperationalInsights/workspaces", f"log-docchat-{env}"),
        _literal("Microsoft.Insights/components", f"appi-docchat-{env}"),
        _literal("Microsoft.Network/virtualNetworks", f"vnet-docchat-{env}"),
        _literal("Microsoft.Network/networkSecurityGroups", f"nsg-docchat-{env}-api"),
        _literal("Microsoft.Network/networkSecurityGroups", f"nsg-docchat-{env}-ingest"),
        _literal("Microsoft.Network/networkSecurityGroups", f"nsg-docchat-{env}-pe"),
        _literal("Microsoft.Insights/actionGroups", f"ag-docchat-{env}-ops"),
        # Content-share fallback 1 only (section 2.7).
        _literal("Microsoft.Network/natGateways", f"ng-docchat-{env}", required=False),
        _literal("Microsoft.Network/publicIPAddresses", f"pip-docchat-{env}-nat", required=False),
    ]
    if config["openAi"]["enabled"]:
        names.append(_literal("Microsoft.CognitiveServices/accounts", f"oai-docchat-{env}-{tok}"))
    if config["modelStub"]["enabled"]:
        names += [
            _literal("Microsoft.Web/serverFarms", "asp-docchat-stub-load"),
            _literal("Microsoft.Web/sites", f"app-docchat-stub-load-{tok}"),
            _literal("Microsoft.ManagedIdentity/userAssignedIdentities", "id-docchat-stub-load"),
            _literal("Microsoft.Network/networkSecurityGroups", "nsg-docchat-load-stub"),
        ]
    # main.bicep declares the autoscale setting only where the plan can scale (prod and load).
    if config["api"]["maxInstances"] > config["api"]["minInstances"]:
        names.append(
            _literal("Microsoft.Insights/autoscaleSettings", f"autoscale-docchat-api-{env}")
        )
    endpoints = private_endpoint_targets(env, tok, config)
    # Fallback 1 adds the host account's file endpoint.
    optional = [(re.escape(f"stdocchathost{env}{tok}"), "file")]
    for targets, required in ((endpoints, True), (optional, False)):
        for target, group in targets:
            names += [
                ExpectedName(
                    "microsoft.network/privateendpoints", f"pe-{target}-{group}", required
                ),
                ExpectedName(
                    "microsoft.network/networkinterfaces", f"nic-pe-{target}-{group}", required
                ),
            ]
    return names


def naming_problems(listed: Iterable[Mapping[str, Any]], expected: list[ExpectedName]) -> list[str]:
    """Each listed resource of a judged type whose name isn't expected, then each expected
    resource the listing lacks."""
    judged = NAMED_TYPES | {name.resource_type for name in expected}
    found: set[int] = set()
    problems: list[str] = []
    for resource in listed:
        resource_type = str(resource.get("type", ""))
        name = str(resource.get("name", ""))
        if resource_type.lower() not in judged:
            continue
        matches = {
            index for index, entry in enumerate(expected) if entry.matches(resource_type, name)
        }
        if not matches:
            problems.append(
                f"{resource_type} {name}: not a name section 1.4 gives this environment"
            )
        found |= matches
    problems += [
        f"{entry.resource_type} {entry.pattern}: missing"
        for index, entry in enumerate(expected)
        if entry.required and index not in found
    ]
    return problems


def budget_problems(
    budget: Mapping[str, Any], *, start_date: str, action_group_id: str
) -> list[str]:
    """How `budget` (a Resource Manager budget body) differs from what the provision must keep:
    the start date the `preprovision` hook read, and `actual-50`, `actual-80` and `actual-100`
    to the ops action group (`83-infrastructure.md` section 12.7)."""
    properties = budget.get("properties") or {}
    problems: list[str] = []
    stored = str((properties.get("timePeriod") or {}).get("startDate", ""))
    if stored[:10] != start_date:
        problems.append(f"startDate {stored[:10] or 'missing'}, not {start_date}")
    notifications = {
        str(key).lower(): value for key, value in (properties.get("notifications") or {}).items()
    }
    if set(notifications) != set(BUDGET_THRESHOLDS):
        problems.append(f"notifications {sorted(notifications)}, not {sorted(BUDGET_THRESHOLDS)}")
    for key, threshold in BUDGET_THRESHOLDS.items():
        notification = notifications.get(key)
        if notification is None:
            continue
        groups = [str(group).lower() for group in notification.get("contactGroups") or []]
        settings = (
            notification.get("enabled") is True,
            str(notification.get("operator", "")),
            notification.get("threshold"),
            str(notification.get("thresholdType", "Actual")),
        )
        if settings != (True, "GreaterThan", threshold, "Actual"):
            problems.append(f"{key}: {settings}, not enabled GreaterThan {threshold} Actual")
        if groups != [action_group_id.lower()]:
            problems.append(f"{key}: contact groups {groups}, not the ops action group")
    return problems
