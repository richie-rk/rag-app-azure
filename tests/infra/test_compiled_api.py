"""The API plan, its autoscale setting and the API site of the compiled template
(83-infrastructure.md sections 2.3 to 2.5, 2.22 and 4.5; ADRs 0004, 0007, 0017, 0023, 0110).

The deployed tests prove the behaviour (T-024's CORS cases, T-447's closed publishing surfaces,
T-474's TLS floor, T-452's prod authsettingsV2); these check what the template declares.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
from arm_template import TENANT_ID, Deployment, Resource
from compiled_support import ENVIRONMENTS, MainFactory, avm_call, load_json, of_type

API_IMAGE = "crdocchatci0000.azurecr.io/docchat/api@sha256:" + "0" * 64
RELEASE = "0123456789abcdef0123456789abcdef01234567"
INSTANCES = {"dev": (1, 1), "prod": (2, 6), "load": (1, 6)}
SKUS = {"dev": "B1", "prod": "P1v3", "load": "P1v3"}


def _token(deployment: Deployment) -> str:
    return str(deployment.outputs()["DOCCHAT_RESOURCE_TOKEN"])


def _site(deployment: Deployment, environment: str) -> dict[str, Any]:
    return avm_call(deployment, f"app-docchat-api-{environment}-{_token(deployment)}")


def _auth_settings(deployment: Deployment) -> Resource:
    (auth,) = [
        config
        for config in of_type(deployment, "Microsoft.Web/sites/config")
        if str(config.name).endswith("/authsettingsV2")
    ]
    return auth


def _api_config(environment: str, **changes: Any) -> dict[str, Any]:
    minimum, maximum = INSTANCES[environment]
    api = {
        "sku": SKUS[environment],
        "minInstances": minimum,
        "maxInstances": maximum,
        "zoneRedundant": False,
    }
    api.update(changes)
    return {"api": api}


@pytest.mark.parametrize("environment", ENVIRONMENTS)
def test_api_plan(main_deployment: MainFactory, environment: str) -> None:
    plan = avm_call(main_deployment(environment), f"asp-docchat-api-{environment}")
    assert plan["kind"] == "linux"
    assert plan["reserved"] is True
    assert plan["skuName"] == SKUS[environment]
    assert plan["skuCapacity"] == INSTANCES[environment][0]
    # The module's default of true for Premium SKUs is always overridden.
    assert plan["zoneRedundant"] is False
    assert plan["perSiteScaling"] is False
    assert plan["elasticScaleEnabled"] is False
    assert plan["enableTelemetry"] is False
    assert plan["tags"]["docchat-component"] == "api"


@pytest.mark.parametrize("environment", ENVIRONMENTS)
def test_api_site(main_deployment: MainFactory, environment: str) -> None:
    deployment = main_deployment(environment, apiImage=API_IMAGE)
    site = _site(deployment, environment)
    assert site["kind"] == "app,linux,container"
    assert [str(identity) for identity in site["managedIdentities"]["userAssignedResourceIds"]] == [
        f"<id-docchat-api-{environment}.outputs.resourceId.value>"
    ]
    assert "systemAssigned" not in site["managedIdentities"]
    assert site["httpsOnly"] is True
    assert site["clientAffinityEnabled"] is False
    assert site["clientAffinityProxyEnabled"] is False
    # The only internet-facing endpoint: no private endpoint, behind platform authentication.
    assert site["publicNetworkAccess"] == "Enabled"
    assert "privateEndpoints" not in site
    assert str(site["virtualNetworkSubnetResourceId"]).endswith(
        f"/virtualNetworks/vnet-docchat-{environment}/subnets/snet-api-integration"
    )
    assert site["outboundVnetRouting"] == {
        "allTraffic": False,
        "applicationTraffic": False,
        "imagePullTraffic": True,
        "contentShareTraffic": False,
        "backupRestoreTraffic": False,
    }
    # No Key Vault reference exists.
    assert "keyVaultAccessIdentityResourceId" not in site
    assert site["basicPublishingCredentialsPolicies"] == [
        {"name": "ftp", "allow": False},
        {"name": "scm", "allow": False},
    ]
    assert site["enableTelemetry"] is False


@pytest.mark.parametrize("environment", ENVIRONMENTS)
def test_api_site_configuration(main_deployment: MainFactory, environment: str) -> None:
    deployment = main_deployment(environment, apiImage=API_IMAGE)
    config = _site(deployment, environment)["siteConfig"]
    assert config["linuxFxVersion"] == f"DOCKER|{API_IMAGE}"
    assert config["acrUseManagedIdentityCreds"] is True
    assert str(config["acrUserManagedIdentityID"]) == (
        f"<id-docchat-api-{environment}.outputs.clientId.value>"
    )
    expected = {
        "alwaysOn": True,
        "http20Enabled": True,
        "minTlsVersion": "1.3",
        "scmMinTlsVersion": "1.3",
        "ftpsState": "Disabled",
        "remoteDebuggingEnabled": False,
        "use32BitWorkerProcess": False,
        "webSocketsEnabled": False,
        "healthCheckPath": "/healthz",
        "ipSecurityRestrictionsDefaultAction": "Allow",
        "ipSecurityRestrictions": [],
        "scmIpSecurityRestrictionsDefaultAction": "Deny",
        "scmIpSecurityRestrictions": [],
        "scmIpSecurityRestrictionsUseMain": False,
        "appCommandLine": "",
    }
    assert {key: config[key] for key in expected} == expected


@pytest.mark.parametrize("environment", ENVIRONMENTS)
def test_api_platform_cors(main_deployment: MainFactory, environment: str) -> None:
    deployment = main_deployment(environment)
    cors = _site(deployment, environment)["siteConfig"]["cors"]
    assert cors == {
        "allowedOrigins": [f"https://<stapp-docchat-{environment}.outputs.defaultHostname.value>"],
        "supportCredentials": False,
    }


def test_api_platform_cors_adds_each_web_custom_domain(main_deployment: MainFactory) -> None:
    domains = ["chat.example.com", "docs.example.com"]
    deployment = main_deployment("prod", config={"webCustomDomains": domains})
    origins = _site(deployment, "prod")["siteConfig"]["cors"]["allowedOrigins"]
    assert origins == [
        "https://<stapp-docchat-prod.outputs.defaultHostname.value>",
        "https://chat.example.com",
        "https://docs.example.com",
    ]
    assert not [origin for origin in origins if "*" in origin or origin.endswith("/")]


@pytest.mark.parametrize("environment", ENVIRONMENTS)
def test_api_platform_authentication(main_deployment: MainFactory, environment: str) -> None:
    deployment = main_deployment(environment)
    auth = _auth_settings(deployment)
    assert auth.api_version == "2025-03-01"
    assert auth.name == f"app-docchat-api-{environment}-{_token(deployment)}/authsettingsV2"
    properties = auth.properties
    assert properties["platform"] == {"enabled": True, "runtimeVersion": "~1"}
    assert properties["globalValidation"] == {
        "requireAuthentication": True,
        "unauthenticatedClientAction": "Return401",
        "excludedPaths": [],
    }
    assert properties["httpSettings"] == {
        "requireHttps": True,
        "routes": {"apiPrefix": "/.auth"},
        "forwardProxy": {"convention": "NoProxy"},
    }
    assert set(properties["identityProviders"]) == {"azureActiveDirectory"}
    entra = properties["identityProviders"]["azureActiveDirectory"]
    outputs = deployment.outputs()
    api_app = outputs["API_CLIENT_ID"]
    assert entra["enabled"] is True
    # Bearer-only with Return401: no client secret or certificate setting.
    assert entra["registration"] == {
        "clientId": api_app,
        "openIdIssuer": f"https://login.microsoftonline.com/{TENANT_ID}/v2.0",
    }
    assert entra["login"] == {"disableWWWAuthenticate": False, "loginParameters": []}
    assert entra["validation"]["allowedAudiences"] == [api_app]
    allowed = [outputs["WEB_CLIENT_ID"]]
    if environment != "prod":
        allowed.append(outputs["TEST_CLIENT_ID"])
    assert entra["validation"]["defaultAuthorizationPolicy"] == {"allowedApplications": allowed}
    assert "allowedPrincipals" not in entra["validation"]["defaultAuthorizationPolicy"]
    assert properties["login"] == {
        "tokenStore": {"enabled": False},
        "preserveUrlFragmentsForLogins": False,
        "allowedExternalRedirectUrls": [],
    }


def test_prod_platform_authentication_allows_only_the_web_client(
    main_deployment: MainFactory,
) -> None:
    deployment = main_deployment("prod")
    outputs = deployment.outputs()
    assert outputs["TEST_CLIENT_ID"] == ""
    policy = _auth_settings(deployment).properties["identityProviders"]["azureActiveDirectory"][
        "validation"
    ]["defaultAuthorizationPolicy"]
    assert policy["allowedApplications"] == [outputs["WEB_CLIENT_ID"]]


@pytest.mark.parametrize("environment", ENVIRONMENTS)
def test_api_site_tags(main_deployment: MainFactory, environment: str) -> None:
    deployment = main_deployment(environment, releaseSha=RELEASE)
    assert _site(deployment, environment)["tags"] == {
        "docchat-env": environment,
        "azd-env-name": environment,
        "docchat-component": "api",
        "azd-service-name": "api",
        "docchat-release-sha": RELEASE,
    }


@pytest.mark.parametrize("environment", ENVIRONMENTS)
def test_api_parameters(compiled_dir: Path, environment: str) -> None:
    parameters = load_json(compiled_dir / f"main.{environment}.parameters.json")["parameters"]
    # Outside the pipeline DOCCHAT_RELEASE_SHA is unset, and the tag reads local.
    assert parameters["releaseSha"]["value"] == "local"
    assert re.fullmatch(
        r"[a-z0-9.-]+\.azurecr\.io/docchat/api@sha256:[0-9a-f]{64}", parameters["apiImage"]["value"]
    )


@pytest.mark.parametrize("environment", ENVIRONMENTS)
def test_api_outputs(main_deployment: MainFactory, environment: str) -> None:
    outputs = main_deployment(environment).outputs()
    token = outputs["DOCCHAT_RESOURCE_TOKEN"]
    assert outputs["DOCCHAT_API_APP_NAME"] == f"app-docchat-api-{environment}-{token}"
    # The default host only, with no path and no trailing slash (ADR 0110).
    assert (
        outputs["API_BASE_URL"]
        == f"https://app-docchat-api-{environment}-{token}.azurewebsites.net"
    )


@pytest.mark.parametrize("environment", ENVIRONMENTS)
def test_no_api_custom_domain(main_deployment: MainFactory, environment: str) -> None:
    deployment = main_deployment(environment)
    assert of_type(deployment, "Microsoft.Web/certificates") == []
    assert of_type(deployment, "Microsoft.Web/sites/hostNameBindings") == []


@pytest.mark.parametrize("environment", ["prod", "load"])
def test_api_autoscale(main_deployment: MainFactory, environment: str) -> None:
    deployment = main_deployment(environment)
    (autoscale,) = of_type(deployment, "Microsoft.Insights/autoscalesettings")
    assert autoscale.api_version == "2022-10-01"
    assert autoscale.name == f"autoscale-docchat-api-{environment}"
    assert autoscale.field("tags")["docchat-component"] == "api"
    properties = autoscale.properties
    plan = f"<asp-docchat-api-{environment}.outputs.resourceId.value>"
    assert properties["enabled"] is True
    assert str(properties["targetResourceUri"]) == plan
    (profile,) = properties["profiles"]
    minimum, maximum = INSTANCES[environment]
    assert profile["name"] == "default"
    assert profile["capacity"] == {
        "minimum": str(minimum),
        "default": str(minimum),
        "maximum": str(maximum),
    }
    out, scale_in = profile["rules"]
    common = {
        "metricName": "CpuPercentage",
        "metricNamespace": "microsoft.web/serverfarms",
        "timeGrain": "PT1M",
        "statistic": "Average",
        "timeAggregation": "Average",
    }
    for rule in (out, scale_in):
        trigger = rule["metricTrigger"]
        assert {key: trigger[key] for key in common} == common
        assert str(trigger["metricResourceUri"]) == plan
    assert (out["metricTrigger"]["timeWindow"], out["metricTrigger"]["operator"]) == (
        "PT5M",
        "GreaterThan",
    )
    assert out["metricTrigger"]["threshold"] == 70
    assert out["scaleAction"] == {
        "direction": "Increase",
        "type": "ChangeCount",
        "value": "1",
        "cooldown": "PT5M",
    }
    assert (scale_in["metricTrigger"]["timeWindow"], scale_in["metricTrigger"]["operator"]) == (
        "PT10M",
        "LessThan",
    )
    assert scale_in["metricTrigger"]["threshold"] == 35
    assert scale_in["scaleAction"] == {
        "direction": "Decrease",
        "type": "ChangeCount",
        "value": "1",
        "cooldown": "PT10M",
    }


def test_dev_has_no_autoscale(main_deployment: MainFactory) -> None:
    assert of_type(main_deployment("dev"), "Microsoft.Insights/autoscalesettings") == []


def test_zone_redundancy_raises_every_minimum_to_two(main_deployment: MainFactory) -> None:
    config = _api_config("load", zoneRedundant=True)
    deployment = main_deployment("load", config=config)
    plan = avm_call(deployment, "asp-docchat-api-load")
    assert plan["zoneRedundant"] is True
    assert plan["skuCapacity"] == 2
    (autoscale,) = of_type(deployment, "Microsoft.Insights/autoscalesettings")
    (profile,) = autoscale.properties["profiles"]
    assert profile["capacity"] == {"minimum": "2", "default": "2", "maximum": "6"}


def test_equal_bounds_need_no_autoscale(main_deployment: MainFactory) -> None:
    deployment = main_deployment("prod", config=_api_config("prod", maxInstances=2))
    assert of_type(deployment, "Microsoft.Insights/autoscalesettings") == []
    assert avm_call(deployment, "asp-docchat-api-prod")["skuCapacity"] == 2
