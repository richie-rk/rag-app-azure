"""The AI services of the compiled template: AI Search, the Azure OpenAI account with its content
filter policies and its five deployments, Content Safety and Document Intelligence
(83-infrastructure.md sections 2.13, 2.14, 4.5, 5, 8.4 and 8.8; ADRs 0066, 0067, 0099 and 0100).

T-126: chat, chat-small and ingest-summary, and the embed-query-<key> and embed-ingest-<key> pair
of each embedding model, each with its SKU from its own type field.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest
from arm_template import Deployment, TemplateFailureError
from compiled_support import ENVIRONMENTS, MainFactory, avm_call, env_file, of_type

ALLOWLIST = ["203.0.113.10", "198.51.100.7"]
DEPLOYMENT_TYPES = ("GlobalStandard", "DataZoneStandard", "Standard")
HARM_CATEGORIES = ("Hate", "Sexual", "Violence", "Selfharm")


def _token(deployment: Deployment) -> str:
    return str(deployment.outputs()["DOCCHAT_RESOURCE_TOKEN"])


def _model_deployments(deployment: Deployment) -> dict[str, Any]:
    return {
        str(resource.name).rsplit("/", 1)[-1]: resource
        for resource in of_type(deployment, "Microsoft.CognitiveServices/accounts/deployments")
    }


def _policies(deployment: Deployment) -> dict[str, dict[str, Any]]:
    return {
        str(resource.name).rsplit("/", 1)[-1]: resource.properties
        for resource in of_type(deployment, "Microsoft.CognitiveServices/accounts/raiPolicies")
    }


# --- AI Search ---------------------------------------------------------------------------------


@pytest.mark.parametrize("environment", ENVIRONMENTS)
def test_search_service(main_deployment: MainFactory, environment: str) -> None:
    deployment = main_deployment(environment)
    service = avm_call(deployment, f"srch-docchat-{environment}-{_token(deployment)}")
    assert service["sku"] == {"dev": "basic", "prod": "standard", "load": "standard"}[environment]
    assert service["replicaCount"] == {"dev": 1, "prod": 2, "load": 2}[environment]
    assert service["partitionCount"] == 1
    assert service["hostingMode"] == "Default"
    assert service["semanticSearch"] == "standard"
    assert service["disableLocalAuth"] is True
    # Role-based access only: no key options, and no identity because it never calls out.
    assert "authOptions" not in service
    assert "managedIdentities" not in service
    assert service["computeType"] == "Default"
    assert service["publicNetworkAccess"] == "Disabled"
    assert service["networkRuleSet"] == {"ipRules": [], "bypass": "None"}
    assert service["enableTelemetry"] is False


def test_search_service_admits_the_operator_allowlist(main_deployment: MainFactory) -> None:
    deployment = main_deployment("dev", operatorIpAllowlist=ALLOWLIST)
    service = avm_call(deployment, f"srch-docchat-dev-{_token(deployment)}")
    assert service["publicNetworkAccess"] == "Enabled"
    assert service["networkRuleSet"] == {
        "ipRules": [{"value": address} for address in ALLOWLIST],
        "bypass": "None",
    }


@pytest.mark.parametrize("environment", ["prod", "load"])
def test_search_replicas_below_two_fail_outside_dev(
    main_deployment: MainFactory, environment: str
) -> None:
    search = {**env_file(environment)["search"], "replicas": 1}
    deployment = main_deployment(environment, config={"search": search})
    with pytest.raises(
        TemplateFailureError, match=r"search\.replicas must be at least 2 in prod and load"
    ):
        avm_call(deployment, f"srch-docchat-{environment}-{_token(deployment)}")


def test_dev_search_runs_on_one_replica(main_deployment: MainFactory) -> None:
    deployment = main_deployment("dev")
    assert avm_call(deployment, f"srch-docchat-dev-{_token(deployment)}")["replicaCount"] == 1


# --- Cognitive Services accounts -----------------------------------------------------------------


def _assert_closed_account(account: dict[str, Any], name: str, kind: str) -> None:
    assert account["name"] == name
    assert account["kind"] == kind
    assert account["sku"] == "S0"
    # Microsoft Entra authentication needs the custom subdomain.
    assert account["customSubDomainName"] == name
    assert account["disableLocalAuth"] is True
    assert account["publicNetworkAccess"] == "Disabled"
    assert account["networkAcls"] == {"defaultAction": "Deny", "bypass": "None", "ipRules": []}
    assert account["restrictOutboundNetworkAccess"] is True
    assert account["allowedFqdnList"] == []
    assert "managedIdentities" not in account
    assert account["enableTelemetry"] is False


@pytest.mark.parametrize("environment", ["dev", "prod"])
def test_openai_account(main_deployment: MainFactory, environment: str) -> None:
    deployment = main_deployment(environment)
    name = f"oai-docchat-{environment}-{_token(deployment)}"
    account = avm_call(deployment, name)
    _assert_closed_account(account, name, "OpenAI")
    assert account["dynamicThrottlingEnabled"] is False
    # Deployments and policies are declared on their GA APIs by openai-deployments.bicep.
    assert account["deployments"] == []


@pytest.mark.parametrize("environment", ENVIRONMENTS)
def test_document_intelligence_account(main_deployment: MainFactory, environment: str) -> None:
    deployment = main_deployment(environment)
    name = f"di-docchat-{environment}-{_token(deployment)}"
    _assert_closed_account(avm_call(deployment, name), name, "FormRecognizer")


@pytest.mark.parametrize("environment", ENVIRONMENTS)
def test_content_safety_account(main_deployment: MainFactory, environment: str) -> None:
    # Real in every environment, load included: the model stub stands in for Azure OpenAI only.
    deployment = main_deployment(environment)
    name = f"cs-docchat-{environment}-{_token(deployment)}"
    _assert_closed_account(avm_call(deployment, name), name, "ContentSafety")
    endpoint = f"https://{name}.cognitiveservices.azure.com"
    assert deployment.outputs()["DOCCHAT_CONTENTSAFETY_ENDPOINT"] == endpoint


def test_ai_accounts_admit_the_operator_allowlist(main_deployment: MainFactory) -> None:
    deployment = main_deployment("dev", operatorIpAllowlist=ALLOWLIST)
    for prefix in ("oai", "cs", "di"):
        account = avm_call(deployment, f"{prefix}-docchat-dev-{_token(deployment)}")
        assert account["publicNetworkAccess"] == "Enabled"
        assert account["networkAcls"] == {
            "defaultAction": "Deny",
            "bypass": "None",
            "ipRules": [{"value": address} for address in ALLOWLIST],
        }


def test_load_has_no_azure_openai(main_deployment: MainFactory) -> None:
    deployment = main_deployment("load")
    names = {str(call.name) for call in deployment.external_calls()}
    assert not [name for name in names if name.startswith("oai-")]
    assert _policies(deployment) == {}
    assert _model_deployments(deployment) == {}


@pytest.mark.parametrize("environment", ENVIRONMENTS)
def test_ai_resources_carry_the_ai_component_tag(
    main_deployment: MainFactory, environment: str
) -> None:
    deployment = main_deployment(environment)
    token = _token(deployment)
    names = [
        f"srch-docchat-{environment}-{token}",
        f"cs-docchat-{environment}-{token}",
        f"di-docchat-{environment}-{token}",
    ]
    if environment != "load":
        names.append(f"oai-docchat-{environment}-{token}")
    for name in names:
        assert avm_call(deployment, name)["tags"] == {
            "docchat-env": environment,
            "azd-env-name": environment,
            "docchat-component": "ai",
        }


# --- Content filter policies ---------------------------------------------------------------------


def _filters(policy: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    filters: dict[tuple[str, str], dict[str, Any]] = {}
    for entry in policy["contentFilters"]:
        key = (entry["name"], entry["source"])
        assert key not in filters
        filters[key] = {
            name: value for name, value in entry.items() if name not in {"name", "source"}
        }
    return filters


@pytest.mark.parametrize("environment", ["dev", "prod"])
def test_content_filter_policies(main_deployment: MainFactory, environment: str) -> None:
    policies = _policies(main_deployment(environment))
    assert set(policies) == {"docchat-text-guard", "docchat-table-summary-guard"}
    blocking: dict[str, object] = {"enabled": True, "blocking": True}
    for name, policy in policies.items():
        assert policy["basePolicyName"] == "Microsoft.DefaultV2"
        assert policy["mode"] == "Default"
        assert policy["customBlocklists"] == []
        expected: dict[tuple[str, str], dict[str, object]] = {
            (category, source): {**blocking, "severityThreshold": "Medium"}
            for category in HARM_CATEGORIES
            for source in ("Prompt", "Completion")
        }
        expected |= {
            ("Jailbreak", "Prompt"): blocking,
            ("Protected Material Text", "Completion"): blocking,
            ("Protected Material Code", "Completion"): blocking,
            ("Profanity", "Prompt"): {"enabled": False, "blocking": False},
            ("Profanity", "Completion"): {"enabled": False, "blocking": False},
            # Only the table summary's documents block goes to the indirect-attack shield; chat
            # sources are screened per source by Prompt Shields.
            ("Indirect Attack", "Prompt"): (
                blocking
                if name == "docchat-table-summary-guard"
                else {"enabled": False, "blocking": False}
            ),
        }
        assert _filters(policy) == expected


# --- Deployments (T-126) ------------------------------------------------------------------------

CHAT_PATH = ("chat", "chat-small", "embed-query-te3-large-v1")
INGESTION = ("ingest-summary", "embed-ingest-te3-large-v1")

# The deployment each type field of the environment file sets: (path to the field, deployment).
TYPE_FIELDS = (
    (("deployments", "chat", "type"), "chat"),
    (("deployments", "chatSmall", "type"), "chat-small"),
    (("deployments", "tableSummary", "type"), "ingest-summary"),
    (("embeddingModels", 0, "queryType"), "embed-query-te3-large-v1"),
    (("embeddingModels", 0, "ingestionType"), "embed-ingest-te3-large-v1"),
)

# 83-infrastructure.md section 5.3: model, version, upgrade option, content filter policy, and the
# dev and prod capacities.
DEFAULTS: dict[str, tuple[str, str, str, str | None, dict[str, int]]] = {
    "chat": (
        "gpt-5.6-sol",
        "2026-07-09",
        "OnceCurrentVersionExpired",
        "docchat-text-guard",
        {"dev": 30, "prod": 500},
    ),
    "chat-small": (
        "gpt-5.6-terra",
        "2026-07-09",
        "OnceCurrentVersionExpired",
        "docchat-text-guard",
        {"dev": 30, "prod": 530},
    ),
    "ingest-summary": (
        "gpt-5.6-terra",
        "2026-07-09",
        "OnceCurrentVersionExpired",
        "docchat-table-summary-guard",
        {"dev": 30, "prod": 150},
    ),
    "embed-query-te3-large-v1": (
        "text-embedding-3-large",
        "1",
        "NoAutoUpgrade",
        None,
        {"dev": 30, "prod": 100},
    ),
    "embed-ingest-te3-large-v1": (
        "text-embedding-3-large",
        "1",
        "NoAutoUpgrade",
        None,
        {"dev": 60, "prod": 250},
    ),
}


@pytest.mark.parametrize("environment", ["dev", "prod"])
def test_t126_five_pinned_deployments(main_deployment: MainFactory, environment: str) -> None:
    deployments = _model_deployments(main_deployment(environment))
    assert set(deployments) == {*CHAT_PATH, *INGESTION}
    for name, (model, version, upgrade, policy, capacities) in DEFAULTS.items():
        resource = deployments[name]
        assert resource.field("sku") == {
            "name": "GlobalStandard",
            "capacity": capacities[environment],
        }, name
        expected: dict[str, object] = {
            # Every version pinned: an omitted version can change under a deployment.
            "model": {"format": "OpenAI", "name": model, "version": version},
            "versionUpgradeOption": upgrade,
        }
        if policy is not None:
            expected["raiPolicyName"] = policy
        # Embedding deployments name no policy (content filtering doesn't apply to embeddings) and
        # never upgrade in place, because a changed embedding model needs a new physical index.
        assert resource.properties == expected, name


@pytest.mark.parametrize("environment", ["dev", "prod"])
def test_t126_deployments_follow_the_environment_file(
    main_deployment: MainFactory, environment: str
) -> None:
    open_ai = env_file(environment)["openAi"]
    deployments = _model_deployments(main_deployment(environment))
    for role in ("chat", "chatSmall", "tableSummary"):
        entry = open_ai["deployments"][role]
        resource = deployments[entry["name"]]
        assert resource.field("sku") == {"name": entry["type"], "capacity": entry["capacity"]}
        assert resource.properties["model"]["name"] == entry["model"]
        assert resource.properties["model"]["version"] == entry["version"]
        assert resource.properties["versionUpgradeOption"] == entry["versionUpgradeOption"]
        assert resource.properties["raiPolicyName"] == entry["raiPolicyName"]
    (model,) = open_ai["embeddingModels"]
    for prefix, type_key, capacity_key in (
        ("embed-query", "queryType", "queryCapacity"),
        ("embed-ingest", "ingestionType", "ingestionCapacity"),
    ):
        resource = deployments[f"{prefix}-{model['key']}"]
        assert resource.field("sku") == {"name": model[type_key], "capacity": model[capacity_key]}
        assert resource.properties["model"]["version"] == model["version"]


def test_t126_deployments_are_written_one_at_a_time_after_the_policies(
    main_deployment: MainFactory,
) -> None:
    deployments = _model_deployments(main_deployment("dev"))
    declaration = next(iter(deployments.values())).raw
    assert declaration["copy"]["mode"] == "serial"
    assert declaration["copy"]["batchSize"] == 1
    assert any("raiPolicies" in str(entry) for entry in declaration["dependsOn"])


def _set_field(open_ai: dict[str, Any], path: tuple[str | int, ...], value: str) -> None:
    target: Any = open_ai
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value


@pytest.mark.parametrize("environment", ["dev", "prod"])
@pytest.mark.parametrize("deployment_type", DEPLOYMENT_TYPES)
@pytest.mark.parametrize(("path", "changed_name"), TYPE_FIELDS)
def test_t126_a_type_field_changes_only_its_own_deployment(
    main_deployment: MainFactory,
    environment: str,
    deployment_type: str,
    path: tuple[str | int, ...],
    changed_name: str,
) -> None:
    base = _model_deployments(main_deployment(environment))
    open_ai = copy.deepcopy(env_file(environment)["openAi"])
    _set_field(open_ai, path, deployment_type)
    changed = _model_deployments(main_deployment(environment, config={"openAi": open_ai}))
    assert set(changed) == set(base)
    for name, resource in changed.items():
        if name == changed_name:
            assert resource.field("sku") == {
                "name": deployment_type,
                "capacity": base[name].field("sku")["capacity"],
            }
        else:
            # No deployment inherits another's type.
            assert resource.field("sku") == base[name].field("sku"), name


def test_t126_a_new_embedding_entry_adds_its_own_deployment_pair(
    main_deployment: MainFactory,
) -> None:
    open_ai = copy.deepcopy(env_file("dev")["openAi"])
    open_ai["embeddingModels"].append(
        {
            **open_ai["embeddingModels"][0],
            "key": "te3-large-v2",
            "queryType": "DataZoneStandard",
            "ingestionType": "Standard",
        }
    )
    deployments = _model_deployments(main_deployment("dev", config={"openAi": open_ai}))
    assert set(deployments) == {
        *CHAT_PATH,
        *INGESTION,
        "embed-query-te3-large-v2",
        "embed-ingest-te3-large-v2",
    }
    assert deployments["embed-query-te3-large-v2"].field("sku")["name"] == "DataZoneStandard"
    assert deployments["embed-ingest-te3-large-v2"].field("sku")["name"] == "Standard"
    assert deployments["embed-query-te3-large-v1"].field("sku")["name"] == "GlobalStandard"
    assert deployments["embed-ingest-te3-large-v1"].field("sku")["name"] == "GlobalStandard"


@pytest.mark.parametrize("environment", ENVIRONMENTS)
def test_capacity_outputs_for_the_capacity_check(
    main_deployment: MainFactory, environment: str
) -> None:
    outputs = main_deployment(environment).outputs()
    # load has no Azure OpenAI account; docchat-ops chat capacity-check reads empty as absent.
    expected = {"dev": ("30", "30"), "prod": ("500", "530"), "load": ("", "")}[environment]
    assert (
        outputs["DOCCHAT_OPENAI_CHAT_CAPACITY"],
        outputs["DOCCHAT_OPENAI_SMALL_CAPACITY"],
    ) == expected


def test_capacity_outputs_come_from_the_deployment_objects(main_deployment: MainFactory) -> None:
    open_ai = copy.deepcopy(env_file("prod")["openAi"])
    open_ai["deployments"]["chat"]["capacity"] = 640
    open_ai["deployments"]["chatSmall"]["capacity"] = 700
    deployment = main_deployment("prod", config={"openAi": open_ai})
    outputs = deployment.outputs()
    assert (outputs["DOCCHAT_OPENAI_CHAT_CAPACITY"], outputs["DOCCHAT_OPENAI_SMALL_CAPACITY"]) == (
        "640",
        "700",
    )
    deployments = _model_deployments(deployment)
    assert deployments["chat"].field("sku")["capacity"] == 640
    assert deployments["chat-small"].field("sku")["capacity"] == 700


@pytest.mark.parametrize("environment", ENVIRONMENTS)
def test_every_deployment_type_field_defaults_to_global_standard(environment: str) -> None:
    open_ai = env_file(environment)["openAi"]
    types = [entry["type"] for entry in open_ai["deployments"].values()]
    for model in open_ai["embeddingModels"]:
        types += [model["queryType"], model["ingestionType"]]
    assert types == ["GlobalStandard"] * 5
