"""T-429: the test-credential vault in the compiled shared template (83-infrastructure.md sections
2.17, 4.2 and 4.5; ADR 0122)."""

from __future__ import annotations

from typing import Any

from arm_template import Deployment
from compiled_support import SharedFactory, avm_call, avm_calls, of_type

ALLOWLIST = ["203.0.113.10", "198.51.100.20"]


def _vault(deployment: Deployment) -> dict[str, object]:
    (call,) = avm_calls(deployment, "kv-docchat-tst-")
    return call.module_parameters


def _ci_network_declared(deployment: Deployment) -> bool:
    return bool(
        avm_calls(deployment, "vnet-docchat-ci")
        or avm_calls(deployment, "pe-kv-docchat-tst-")
        or avm_calls(deployment, "zone-privatelink.vaultcore.azure.net")
        or of_type(deployment, "Microsoft.Network/networkSecurityGroups")
    )


def test_t429_default_path_keeps_public_access_with_no_rule(
    shared_deployment: SharedFactory,
) -> None:
    deployment = shared_deployment()
    vault = _vault(deployment)
    assert vault["publicNetworkAccess"] == "Enabled"
    assert vault["networkAcls"] == {"defaultAction": "Deny", "bypass": "None", "ipRules": []}
    assert not _ci_network_declared(deployment)


def test_t429_shared_allowlist_becomes_exactly_the_ip_rules(
    shared_deployment: SharedFactory,
) -> None:
    deployment = shared_deployment(sharedOperatorIpAllowlist=ALLOWLIST)
    vault = _vault(deployment)
    assert vault["publicNetworkAccess"] == "Enabled"
    assert vault["networkAcls"] == {
        "defaultAction": "Deny",
        "bypass": "None",
        "ipRules": [{"value": address} for address in ALLOWLIST],
    }
    (registry,) = of_type(deployment, "Microsoft.ContainerRegistry/registries")
    assert registry.properties["networkRuleSet"] == {
        "defaultAction": "Deny",
        "ipRules": [{"action": "Allow", "value": address} for address in ALLOWLIST],
    }


def test_t429_private_networking_closes_the_vault(shared_deployment: SharedFactory) -> None:
    deployment = shared_deployment(ciPrivateNetworking=True)
    vault = _vault(deployment)
    assert vault["publicNetworkAccess"] == "Disabled"
    assert vault["networkAcls"] == {"defaultAction": "Deny", "bypass": "None", "ipRules": []}
    vault_name = vault["name"]
    endpoint = avm_call(deployment, f"pe-{vault_name}-vault")
    assert str(endpoint["subnetResourceId"]).endswith(
        "/virtualNetworks/vnet-docchat-ci/subnets/snet-ci-private-endpoints"
    )
    assert endpoint["privateLinkServiceConnections"][0]["properties"]["groupIds"] == ["vault"]
    zone = avm_call(deployment, "zone-privatelink.vaultcore.azure.net")
    assert zone["virtualNetworkLinks"] == [
        {
            "name": "link-vnet-docchat-ci",
            "virtualNetworkResourceId": zone["virtualNetworkLinks"][0]["virtualNetworkResourceId"],
            "registrationEnabled": False,
        }
    ]
    assert "vnet-docchat-ci" in str(zone["virtualNetworkLinks"][0]["virtualNetworkResourceId"])
    network = avm_call(deployment, "vnet-docchat-ci")
    assert network["addressPrefixes"] == ["10.60.0.0/24"]
    subnets = {subnet["name"]: subnet for subnet in network["subnets"]}
    assert subnets["snet-ci-runners"]["addressPrefix"] == "10.60.0.0/26"
    assert subnets["snet-ci-runners"]["delegation"] == "GitHub.Network/networkSettings"
    assert subnets["snet-ci-runners"]["defaultOutboundAccess"] is True
    assert subnets["snet-ci-private-endpoints"]["addressPrefix"] == "10.60.0.64/27"
    assert subnets["snet-ci-private-endpoints"]["defaultOutboundAccess"] is False
    assert {
        group.name for group in of_type(deployment, "Microsoft.Network/networkSecurityGroups")
    } == {
        "nsg-docchat-ci-runners",
        "nsg-docchat-ci-pe",
    }


def _rules(group: Any) -> dict[str, tuple[Any, ...]]:
    return {
        rule["name"]: (
            rule["properties"]["priority"],
            rule["properties"]["direction"],
            rule["properties"]["access"],
            rule["properties"]["protocol"],
            rule["properties"]["sourceAddressPrefix"],
            rule["properties"]["destinationAddressPrefix"],
            rule["properties"]["destinationPortRange"],
        )
        for rule in group.properties["securityRules"]
    }


def test_t429_ci_network_security_rules(shared_deployment: SharedFactory) -> None:
    # 83-infrastructure.md section 4.3: deny by default inside the network, both ways.
    deployment = shared_deployment(ciPrivateNetworking=True)
    groups = {
        group.name: _rules(group)
        for group in of_type(deployment, "Microsoft.Network/networkSecurityGroups")
    }
    deny_out = (4000, "Outbound", "Deny", "*", "*", "VirtualNetwork", "*")
    deny_in = (4000, "Inbound", "Deny", "*", "VirtualNetwork", "*", "*")
    assert groups == {
        "nsg-docchat-ci-runners": {
            "allow-vault-endpoint": (100, "Outbound", "Allow", "Tcp", "*", "10.60.0.64/27", "443"),
            "deny-vnet-outbound": deny_out,
            "deny-vnet-inbound": deny_in,
        },
        "nsg-docchat-ci-pe": {
            "allow-runners-https": (
                100,
                "Inbound",
                "Allow",
                "Tcp",
                "10.60.0.0/26",
                "10.60.0.64/27",
                "443",
            ),
            "deny-vnet-outbound": deny_out,
            "deny-vnet-inbound": deny_in,
        },
    }


def test_t429_vault_protection(shared_deployment: SharedFactory) -> None:
    vault = _vault(shared_deployment())
    assert vault["sku"] == "standard"
    assert vault["enableRbacAuthorization"] is True
    assert vault["softDeleteRetentionInDays"] == 90
    assert vault["enablePurgeProtection"] is True
