"""The load environment's extras in the compiled template (`83-infrastructure.md` sections 2.1,
2.21, 8.3 and 8.9; `84-testing.md` section 7.3; ADRs 0112 and 0151; `90-build-order.md`
section 3.7).

In load the model stub host stands in for Azure OpenAI: a one-instance P1v3 plan and a container
site reached only through its private endpoint, with the identity `id-docchat-stub-load` (AcrPull
only), both apps' model endpoint on its default host name, and its settings from the `modelStub`
key of `infra/config/app-settings.json`. The three conversation containers are created at
30,000 RU/s and the workspace has no daily cap. dev and prod declare none of the stub.
"""

from __future__ import annotations

from typing import Any

import pytest
from arm_template import Deployment
from compiled_support import (
    ENVIRONMENTS,
    INFRA,
    MainFactory,
    avm_call,
    avm_calls,
    env_file,
    load_json,
    of_type,
)

STUB_IMAGE = "crdocchatci0000.azurecr.io/docchat/model-stub@sha256:" + "0" * 64


def _token(deployment: Deployment) -> str:
    return str(deployment.outputs()["DOCCHAT_RESOURCE_TOKEN"])


def _stub_site(deployment: Deployment) -> dict[str, Any]:
    return avm_call(deployment, f"app-docchat-stub-load-{_token(deployment)}")


def test_load_json_carries_the_extras_of_stage_6() -> None:
    config = env_file("load")
    assert config["modelStub"] == {"enabled": True}
    assert config["openAi"]["enabled"] is False
    # 60 turns a second x 400 RU per completed turn x 1.25 headroom (ADR 0151).
    assert config["cosmos"]["maxThroughput"] == {
        "conversations": 30000,
        "memories": 30000,
        "usage": 30000,
    }
    assert 60 * 400 * 1.25 == 30000
    assert config["monitoring"]["dailyCapGb"] == -1
    assert config["cosmos"]["serverless"] is False
    for other in ("dev", "prod"):
        assert env_file(other)["modelStub"] == {"enabled": False}


def test_the_stub_plan_is_one_p1v3_instance_without_zone_redundancy(
    main_deployment: MainFactory,
) -> None:
    plan = avm_call(main_deployment("load"), "asp-docchat-stub-load")
    assert plan["kind"] == "linux"
    assert plan["reserved"] is True
    assert plan["skuName"] == "P1v3"
    assert plan["skuCapacity"] == 1
    assert plan["zoneRedundant"] is False
    assert plan["perSiteScaling"] is False
    assert plan["elasticScaleEnabled"] is False
    assert plan["tags"]["docchat-component"] == "ai"
    assert plan["tags"]["docchat-env"] == "load"


def test_the_stub_site_is_private_hardened_and_runs_the_release_image(
    main_deployment: MainFactory,
) -> None:
    deployment = main_deployment("load", modelStubImage=STUB_IMAGE)
    site = _stub_site(deployment)
    assert site["kind"] == "app,linux,container"
    assert [str(i) for i in site["managedIdentities"]["userAssignedResourceIds"]] == [
        "<id-docchat-stub-load.outputs.resourceId.value>"
    ]
    assert site["publicNetworkAccess"] == "Disabled"
    assert site["httpsOnly"] is True
    assert site["clientAffinityEnabled"] is False
    assert str(site["virtualNetworkSubnetResourceId"]).endswith(
        "/virtualNetworks/vnet-docchat-load/subnets/snet-stub-integration"
    )
    assert site["outboundVnetRouting"]["imagePullTraffic"] is True
    assert site["outboundVnetRouting"]["allTraffic"] is False
    config = site["siteConfig"]
    assert config["linuxFxVersion"] == f"DOCKER|{STUB_IMAGE}"
    assert config["acrUseManagedIdentityCreds"] is True
    assert (
        str(config["acrUserManagedIdentityID"]) == "<id-docchat-stub-load.outputs.clientId.value>"
    )
    assert config["alwaysOn"] is True
    assert config["healthCheckPath"] == "/healthz"
    assert (config["minTlsVersion"], config["scmMinTlsVersion"]) == ("1.3", "1.3")
    assert config["ftpsState"] == "Disabled"
    assert config["remoteDebuggingEnabled"] is False
    assert config["scmIpSecurityRestrictionsDefaultAction"] == "Deny"
    assert config["scmIpSecurityRestrictions"] == []
    assert config["appCommandLine"] == ""
    assert site["basicPublishingCredentialsPolicies"] == [
        {"name": "ftp", "allow": False},
        {"name": "scm", "allow": False},
    ]
    assert "azd-service-name" not in site["tags"]


def test_the_stub_settings_are_its_defaults_and_the_container_constants(
    main_deployment: MainFactory,
) -> None:
    [settings] = _stub_site(main_deployment("load"))["configs"]
    assert settings["name"] == "appsettings"
    assert settings["retainCurrentAppSettings"] is False
    defaults = load_json(INFRA / "config" / "app-settings.json")["modelStub"]
    assert settings["properties"] == defaults | {
        "WEBSITES_ENABLE_APP_SERVICE_STORAGE": "false",
        "WEBSITES_PORT": "8000",
        "WEBSITES_CONTAINER_START_TIME_LIMIT": "300",
        "WEBSITE_HEALTHCHECK_MAXPINGFAILURES": "3",
    }
    assert defaults["DOCCHAT_STUB_WORKERS"] == "2"
    assert defaults["DOCCHAT_STUB_THROTTLE_EVERY_N"] == "20"


def test_the_stub_identity_exists_only_in_load(main_deployment: MainFactory) -> None:
    for environment in ENVIRONMENTS:
        deployment = main_deployment(environment)
        names = {str(call.name) for call in deployment.external_calls()}
        has_stub = environment == "load"
        assert ("id-docchat-stub-load" in names) is has_stub
        assert ("asp-docchat-stub-load" in names) is has_stub
        assert any(name.startswith("app-docchat-stub-load-") for name in names) is has_stub
        identity = list(avm_calls(deployment, "id-docchat-stub-load"))
        if has_stub:
            [call] = identity
            assert call.module_parameters["tags"]["docchat-component"] == "ai"


@pytest.mark.parametrize("environment", ENVIRONMENTS)
def test_the_stub_app_name_is_an_output_in_load_only(
    main_deployment: MainFactory, environment: str
) -> None:
    deployment = main_deployment(environment)
    outputs = deployment.outputs()
    expected = f"app-docchat-stub-load-{_token(deployment)}" if environment == "load" else ""
    assert outputs["DOCCHAT_MODEL_STUB_APP_NAME"] == expected


@pytest.mark.parametrize("environment", ["load", "prod"])
def test_the_containers_start_at_their_environments_autoscale_maximum(
    main_deployment: MainFactory, environment: str
) -> None:
    containers = of_type(
        main_deployment(environment),
        "Microsoft.DocumentDB/databaseAccounts/sqlDatabases/containers",
    )
    maxima = {
        str(container.name).rsplit("/", 1)[-1]: container.properties["options"][
            "autoscaleSettings"
        ]["maxThroughput"]
        for container in containers
    }
    assert maxima == env_file(environment)["cosmos"]["maxThroughput"]
    if environment == "load":
        assert set(maxima.values()) == {30000}


def test_the_load_workspace_has_no_daily_cap(main_deployment: MainFactory) -> None:
    workspace = avm_call(main_deployment("load"), "log-docchat-load")
    assert str(workspace["dailyQuotaGb"]) == "-1"
