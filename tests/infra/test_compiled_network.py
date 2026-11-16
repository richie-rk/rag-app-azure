"""T-424: the network of the compiled template (83-infrastructure.md sections 4.1, 4.3 and 4.4).

Every environment holds 13 endpoints and 10 zones: load has no Azure OpenAI endpoint or zone
and adds the model stub's sites endpoint and its privatelink.azurewebsites.net zone (section 4.4).
"""

from __future__ import annotations

from typing import Any

import pytest
from arm_template import Deployment
from compiled_support import ENVIRONMENTS, MainFactory, avm_call, avm_calls, of_type

ADDRESS_SPACES = {"dev": "10.61", "prod": "10.62", "load": "10.63"}
SQL_PORTS = ["1433", "11000-11999"]


def _prefixes(environment: str) -> dict[str, str]:
    base = ADDRESS_SPACES[environment]
    return {
        "api": f"{base}.0.0/24",
        "ingest": f"{base}.1.0/24",
        "pe": f"{base}.2.0/24",
        "stub": f"{base}.3.0/26",
    }


def _rule(rule: dict[str, Any]) -> tuple[Any, ...]:
    properties = rule["properties"]
    sources = properties.get("sourceAddressPrefixes") or [properties["sourceAddressPrefix"]]
    ports = properties.get("destinationPortRanges") or [properties["destinationPortRange"]]
    return (
        rule["name"],
        properties["priority"],
        properties["direction"],
        properties["access"],
        properties["protocol"],
        tuple(sources),
        properties["destinationAddressPrefix"],
        tuple(ports),
    )


def _expected_nsgs(environment: str) -> dict[str, set[tuple[Any, ...]]]:
    prefix = _prefixes(environment)
    apps = (prefix["api"], prefix["ingest"])
    deny_in = ("deny-vnet-inbound", 4000, "Inbound", "Deny", "*", ("VirtualNetwork",), "*", ("*",))
    deny_out = (
        "deny-vnet-outbound",
        4000,
        "Outbound",
        "Deny",
        "*",
        ("*",),
        "VirtualNetwork",
        ("*",),
    )
    integration = {
        (
            "allow-private-endpoints",
            100,
            "Outbound",
            "Allow",
            "Tcp",
            ("*",),
            prefix["pe"],
            ("443", *SQL_PORTS),
        ),
        deny_out,
        deny_in,
    }
    https_sources = (*apps, prefix["stub"]) if environment == "load" else apps
    expected: dict[str, set[tuple[Any, ...]]] = {
        f"nsg-docchat-{environment}-pe": {
            (
                "allow-apps-https",
                100,
                "Inbound",
                "Allow",
                "Tcp",
                https_sources,
                prefix["pe"],
                ("443",),
            ),
            (
                "allow-apps-sql",
                110,
                "Inbound",
                "Allow",
                "Tcp",
                apps,
                prefix["pe"],
                tuple(SQL_PORTS),
            ),
            # Private endpoints originate nothing, so the outbound deny changes no path; it keeps
            # every group deny-by-default both ways (section 4.3).
            deny_out,
            deny_in,
        },
        f"nsg-docchat-{environment}-api": integration,
        f"nsg-docchat-{environment}-ingest": integration,
    }
    if environment == "load":
        expected["nsg-docchat-load-stub"] = {
            (
                "allow-private-endpoints-https",
                100,
                "Outbound",
                "Allow",
                "Tcp",
                ("*",),
                prefix["pe"],
                ("443",),
            ),
            deny_out,
        }
    return expected


@pytest.mark.parametrize("environment", ENVIRONMENTS)
def test_t424_network_security_rules(main_deployment: MainFactory, environment: str) -> None:
    deployment = main_deployment(environment)
    groups = of_type(deployment, "Microsoft.Network/networkSecurityGroups")
    actual = {
        group.name: {_rule(rule) for rule in group.properties["securityRules"]} for group in groups
    }
    assert actual == _expected_nsgs(environment)


@pytest.mark.parametrize("environment", ENVIRONMENTS)
def test_t424_subnets_and_delegations(main_deployment: MainFactory, environment: str) -> None:
    deployment = main_deployment(environment)
    vnet = avm_call(deployment, f"vnet-{environment}")
    prefix = _prefixes(environment)
    assert vnet["name"] == f"vnet-docchat-{environment}"
    assert vnet["addressPrefixes"] == [f"{ADDRESS_SPACES[environment]}.0.0/22"]
    delegated = {"delegation": "Microsoft.Web/serverFarms"}
    expected = {
        "snet-api-integration": {
            "addressPrefix": prefix["api"],
            **delegated,
            "nsg": f"nsg-docchat-{environment}-api",
        },
        "snet-ingest-integration": {
            "addressPrefix": prefix["ingest"],
            **delegated,
            "nsg": f"nsg-docchat-{environment}-ingest",
        },
        "snet-private-endpoints": {
            "addressPrefix": prefix["pe"],
            "nsg": f"nsg-docchat-{environment}-pe",
            "privateEndpointNetworkPolicies": "Enabled",
            "defaultOutboundAccess": False,
        },
    }
    if environment == "load":
        expected["snet-stub-integration"] = {
            "addressPrefix": prefix["stub"],
            **delegated,
            "nsg": "nsg-docchat-load-stub",
        }
    actual = {}
    for subnet in vnet["subnets"]:
        entry = {
            key: value
            for key, value in subnet.items()
            if key not in {"name", "networkSecurityGroupResourceId"}
        }
        entry["nsg"] = str(subnet["networkSecurityGroupResourceId"]).rsplit("/", 1)[-1]
        actual[subnet["name"]] = entry
    assert actual == expected


def _zones(environment: str) -> set[str]:
    zones = {
        "privatelink.database.windows.net",
        "privatelink.documents.azure.com",
        "privatelink.blob.core.windows.net",
        "privatelink.queue.core.windows.net",
        "privatelink.table.core.windows.net",
        "privatelink.vaultcore.azure.net",
        "privatelink.azurecr.io",
        "privatelink.search.windows.net",
        "privatelink.cognitiveservices.azure.com",
    }
    zones.add(
        "privatelink.azurewebsites.net" if environment == "load" else "privatelink.openai.azure.com"
    )
    return zones


@pytest.mark.parametrize("environment", ENVIRONMENTS)
def test_t424_private_dns_zones_linked_to_the_environment_network_only(
    main_deployment: MainFactory, environment: str
) -> None:
    deployment = main_deployment(environment)
    zones = [call.module_parameters for call in avm_calls(deployment, "zone-")]
    assert {zone["name"] for zone in zones} == _zones(environment)
    assert len(zones) == 10
    for zone in zones:
        assert zone["location"] == "global"
        assert len(zone["virtualNetworkLinks"]) == 1
        link = zone["virtualNetworkLinks"][0]
        assert link["name"] == f"link-vnet-docchat-{environment}"
        assert link["registrationEnabled"] is False


def _target_name(service_id: object) -> str:
    """The target resource's name, from its ID or from the module output that stands for it."""
    text = str(service_id)
    if text.startswith("<"):
        return text[1:].split(".outputs.", 1)[0]
    return text.rsplit("/", 1)[-1]


def _endpoint_targets(deployment: Deployment, environment: str) -> dict[tuple[str, str], str]:
    outputs = deployment.outputs()
    token = outputs["DOCCHAT_RESOURCE_TOKEN"]
    registry = next(
        call.module_parameters["privateLinkServiceConnections"][0]["properties"][
            "privateLinkServiceId"
        ]
        for call in avm_calls(deployment, "pe-crdocchat")
    )
    return {
        (f"sql-docchat-{environment}-{token}", "sqlServer"): "privatelink.database.windows.net",
        (f"cosmos-docchat-{environment}-{token}", "Sql"): "privatelink.documents.azure.com",
        (f"stdocchatdata{environment}{token}", "blob"): "privatelink.blob.core.windows.net",
        (f"stdocchatdata{environment}{token}", "queue"): "privatelink.queue.core.windows.net",
        (f"stdocchathost{environment}{token}", "blob"): "privatelink.blob.core.windows.net",
        (f"stdocchathost{environment}{token}", "queue"): "privatelink.queue.core.windows.net",
        (f"stdocchathost{environment}{token}", "table"): "privatelink.table.core.windows.net",
        (f"kv-docchat-{environment}-{token}", "vault"): "privatelink.vaultcore.azure.net",
        (_target_name(registry), "registry"): "privatelink.azurecr.io",
        (f"srch-docchat-{environment}-{token}", "searchService"): "privatelink.search.windows.net",
        (f"cs-docchat-{environment}-{token}", "account"): "privatelink.cognitiveservices.azure.com",
        (f"di-docchat-{environment}-{token}", "account"): "privatelink.cognitiveservices.azure.com",
    } | (
        {(f"app-docchat-stub-load-{token}", "sites"): "privatelink.azurewebsites.net"}
        if environment == "load"
        else {(f"oai-docchat-{environment}-{token}", "account"): "privatelink.openai.azure.com"}
    )


@pytest.mark.parametrize("environment", ENVIRONMENTS)
def test_t424_private_endpoints(main_deployment: MainFactory, environment: str) -> None:
    deployment = main_deployment(environment)
    expected = _endpoint_targets(deployment, environment)
    endpoints = [call.module_parameters for call in avm_calls(deployment, "pe-")]
    assert len(endpoints) == len(expected) == 13
    actual: dict[tuple[str, str], str] = {}
    for endpoint in endpoints:
        connection = endpoint["privateLinkServiceConnections"][0]["properties"]
        target = _target_name(connection["privateLinkServiceId"])
        (group,) = connection["groupIds"]
        assert endpoint["name"] == f"pe-{target}-{group}"
        assert endpoint["customNetworkInterfaceName"] == f"nic-pe-{target}-{group}"
        assert str(endpoint["subnetResourceId"]).endswith(
            f"/virtualNetworks/vnet-docchat-{environment}/subnets/snet-private-endpoints"
        )
        zone_group = endpoint["privateDnsZoneGroup"]
        assert zone_group["name"] == "default"
        (config,) = zone_group["privateDnsZoneGroupConfigs"]
        actual[(target, group)] = str(config["privateDnsZoneResourceId"]).rsplit("/", 1)[-1]
    assert actual == expected
    registry_id = next(
        str(endpoint["privateLinkServiceConnections"][0]["properties"]["privateLinkServiceId"])
        for endpoint in endpoints
        if endpoint["privateLinkServiceConnections"][0]["properties"]["groupIds"] == ["registry"]
    )
    assert (
        "/resourceGroups/rg-docchat-shared/providers/Microsoft.ContainerRegistry/registries/crdocchat"
        in registry_id
    )


@pytest.mark.parametrize("environment", ENVIRONMENTS)
def test_t424_no_ci_network_in_an_environment(
    main_deployment: MainFactory, environment: str
) -> None:
    deployment = main_deployment(environment)
    assert not avm_calls(deployment, "vnet-docchat-ci")
    assert not [
        group
        for group in of_type(deployment, "Microsoft.Network/networkSecurityGroups")
        if "-ci-" in group.name
    ]
