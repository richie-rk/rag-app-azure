"""T-425: role assignments in the compiled template against the call matrices of
83-infrastructure.md sections 6.5 to 6.10, for the targets provisioned so far.

The API identity holds Search Index Data Reader on AI Search, Cognitive Services OpenAI User on
Azure OpenAI (not in load, whose apps call the model stub with no role) and Cognitive Services User
on Content Safety (every environment); it holds no Document Intelligence role. The ingestion
identity holds its AI Search, Azure OpenAI and Document Intelligence roles and no Content Safety
role, and sends to no queue but the poison queue: the stage-0 spike trigger's Message Sender row
on `ingestion-requests` left with the starters.
"""

from __future__ import annotations

import pytest
from arm_template import SUBSCRIPTION_ID, Deployment, guid
from compiled_support import ENVIRONMENTS, GROUP_IDS, MainFactory, of_type

READER = "acdd72a7-3385-48ef-bd42-f606fba81ae7"
LOG_ANALYTICS_READER = "73c42c96-874c-492b-b04d-ab87d138a893"
METRICS_PUBLISHER = "3913510d-42f4-4e42-8a64-420c390055eb"
BLOB_CONTRIBUTOR = "ba92f5b4-2d11-453d-a403-e96b0029c9fe"
BLOB_READER = "2a2b9908-6ea1-4ae2-8e65-a410df84e7d1"
BLOB_OWNER = "b7e6dc6d-f1e8-4753-8033-0f276bb0955b"
QUEUE_CONTRIBUTOR = "974c5e8b-45b9-4653-ba55-5f855dd0fb88"
QUEUE_READER = "19e7f393-937e-4f77-808e-94535e297925"
QUEUE_PROCESSOR = "8a0f0c08-91a1-4084-bc3d-661d67233fed"
QUEUE_SENDER = "c6a89b2d-59bc-44d0-9896-0f6e12d7b80a"
TABLE_CONTRIBUTOR = "0a9a7e1f-b9d0-4cc4-a60d-0319b160aaa3"
BLOB_TAG_READER = "a5f5ab92-08e9-42d6-b9f2-31ca2b30a345"
ACR_PULL = "7f951dda-4ed3-4680-a7ca-43fe172d538d"
SEARCH_INDEX_MANAGER = "e622b7ad-d5a9-48fb-8d1c-ae3d52a7cb9d"
SEARCH_DATA_READER = "1407120a-92aa-4202-b7e9-c0e197c71c8f"
SEARCH_DATA_CONTRIBUTOR = "8ebe5a00-799e-43f5-93ac-243d3dce84a7"
OPENAI_USER = "5e0bd9bd-7b93-4f28-af87-19fc36ad61bd"
COGNITIVE_SERVICES_USER = "a97b65f3-24c7-4388-baec-2e87135dc908"

COSMOS_READER = "00000000-0000-0000-0000-000000000001"
COSMOS_CONTRIBUTOR = "00000000-0000-0000-0000-000000000002"


def _principals(environment: str) -> dict[str, str]:
    return {
        "api": f"<id-docchat-api-{environment}.outputs.principalId.value>",
        "ingestion": f"<id-docchat-ingest-{environment}.outputs.principalId.value>",
        "stub": "<id-docchat-stub-load.outputs.principalId.value>",
        "operators": GROUP_IDS["operators"],
        "configOperators": GROUP_IDS["configOperators"],
        "userDataOperators": GROUP_IDS["userDataOperators"],
        "conversationReaders": GROUP_IDS["conversationReaders"],
    }


def _expected(environment: str, token: str) -> set[tuple[str, str, str]]:
    data = f"stdocchatdata{environment}{token}"
    host = f"stdocchathost{environment}{token}"
    rows = {
        ("resourceGroup", "operators", READER),
        (f"workspaces/log-docchat-{environment}", "operators", LOG_ANALYTICS_READER),
        (f"components/appi-docchat-{environment}", "api", METRICS_PUBLISHER),
        (f"components/appi-docchat-{environment}", "ingestion", METRICS_PUBLISHER),
        (f"storageAccounts/{host}", "ingestion", BLOB_OWNER),
        (f"storageAccounts/{host}", "ingestion", QUEUE_CONTRIBUTOR),
        (f"storageAccounts/{host}", "ingestion", TABLE_CONTRIBUTOR),
        (f"{data}/blobServices/default/containers/documents", "api", BLOB_CONTRIBUTOR),
        (f"{data}/blobServices/default/containers/documents", "ingestion", BLOB_CONTRIBUTOR),
        (f"{data}/blobServices/default/containers/documents", "ingestion", BLOB_TAG_READER),
        (f"{data}/blobServices/default/containers/documents", "userDataOperators", BLOB_READER),
        (f"{data}/blobServices/default/containers/renditions", "api", BLOB_READER),
        (f"{data}/blobServices/default/containers/renditions", "ingestion", BLOB_CONTRIBUTOR),
        (f"{data}/blobServices/default/containers/ingestion-work", "ingestion", BLOB_CONTRIBUTOR),
        (f"{data}/queueServices/default/queues/ingestion-requests", "api", QUEUE_SENDER),
        (f"{data}/queueServices/default/queues/ingestion-requests", "ingestion", QUEUE_READER),
        (f"{data}/queueServices/default/queues/ingestion-requests", "ingestion", QUEUE_PROCESSOR),
        (
            f"{data}/queueServices/default/queues/ingestion-requests-poison",
            "ingestion",
            QUEUE_SENDER,
        ),
        (
            f"{data}/queueServices/default/queues/ingestion-requests-poison",
            "ingestion",
            QUEUE_READER,
        ),
        (
            f"{data}/queueServices/default/queues/ingestion-requests-poison",
            "configOperators",
            QUEUE_PROCESSOR,
        ),
    }
    search = f"searchServices/srch-docchat-{environment}-{token}"
    rows |= {
        (search, "api", SEARCH_DATA_READER),
        (search, "ingestion", SEARCH_INDEX_MANAGER),
        (search, "ingestion", SEARCH_DATA_CONTRIBUTOR),
        (f"accounts/cs-docchat-{environment}-{token}", "api", COGNITIVE_SERVICES_USER),
        (f"accounts/di-docchat-{environment}-{token}", "ingestion", COGNITIVE_SERVICES_USER),
    }
    if environment != "load":
        rows.add((f"accounts/oai-docchat-{environment}-{token}", "api", OPENAI_USER))
        rows.add((f"accounts/oai-docchat-{environment}-{token}", "ingestion", OPENAI_USER))
    rows |= {("registry", "api", ACR_PULL), ("registry", "ingestion", ACR_PULL)}
    if environment == "load":
        # The model stub's identity holds AcrPull and nothing else (section 6.7).
        rows.add(("registry", "stub", ACR_PULL))
    return rows


def _scope_key(scope: str, resource_group: str) -> str:
    if scope.rstrip("/").endswith(f"/resourceGroups/{resource_group}"):
        return "resourceGroup"
    if "/Microsoft.ContainerRegistry/registries/" in scope:
        return "registry"
    marker = "/providers/"
    path = scope.split(marker, 1)[1]
    provider_free = path.split("/", 1)[1]
    if "/storageAccounts/" in f"/{provider_free}" and "Services/" in provider_free:
        return provider_free.split("storageAccounts/", 1)[1]
    return provider_free


def _assignments(deployment: Deployment, environment: str) -> set[tuple[str, str, str]]:
    names = {value: key for key, value in _principals(environment).items()}
    actual: set[tuple[str, str, str]] = set()
    for assignment in of_type(deployment, "Microsoft.Authorization/roleAssignments"):
        properties = assignment.properties
        role = str(properties["roleDefinitionId"]).rsplit("/", 1)[-1]
        principal = str(properties["principalId"])
        scope = str(assignment.field("scope") or "") or (
            f"/subscriptions/{SUBSCRIPTION_ID}/resourceGroups/{assignment.resource_group}"
        )
        assert properties["roleDefinitionId"] == (
            f"/subscriptions/{SUBSCRIPTION_ID}/providers/Microsoft.Authorization/roleDefinitions/{role}"
        )
        # Every name is guid(<scope ID>, <principal ID>, <role GUID>), so a redeploy is idempotent.
        assert assignment.name == guid(scope, properties["principalId"], role)
        expected_type = "Group" if principal in GROUP_IDS.values() else "ServicePrincipal"
        assert properties["principalType"] == expected_type
        actual.add(
            (_scope_key(scope, assignment.resource_group), names.get(principal, principal), role)
        )
    return actual


@pytest.mark.parametrize("environment", ENVIRONMENTS)
def test_t425_role_assignments_equal_the_call_matrices(
    main_deployment: MainFactory, environment: str
) -> None:
    deployment = main_deployment(environment)
    token = str(deployment.outputs()["DOCCHAT_RESOURCE_TOKEN"])
    assert _assignments(deployment, environment) == _expected(environment, token)


@pytest.mark.parametrize("environment", ENVIRONMENTS)
def test_t425_acr_pull_is_assigned_in_the_shared_group(
    main_deployment: MainFactory, environment: str
) -> None:
    deployment = main_deployment(environment)
    pulls = [
        assignment
        for assignment in of_type(deployment, "Microsoft.Authorization/roleAssignments")
        if str(assignment.properties["roleDefinitionId"]).endswith(ACR_PULL)
    ]
    assert len(pulls) == (3 if environment == "load" else 2)
    assert {assignment.resource_group for assignment in pulls} == {"rg-docchat-shared"}


@pytest.mark.parametrize("group_access", [True, False])
def test_t425_cosmos_data_plane_assignments(
    main_deployment: MainFactory, group_access: bool
) -> None:
    deployment = main_deployment("dev", config={"cosmosGroupDataAccess": group_access})
    principals = _principals("dev")
    (account,) = of_type(deployment, "Microsoft.DocumentDB/databaseAccounts")
    account_id = account.id
    expected = {(principals["api"], COSMOS_CONTRIBUTOR, f"{account_id}/dbs/docchat")}
    if group_access:
        expected |= {
            (
                principals["conversationReaders"],
                COSMOS_READER,
                f"{account_id}/dbs/docchat/colls/conversations",
            ),
            (principals["userDataOperators"], COSMOS_CONTRIBUTOR, f"{account_id}/dbs/docchat"),
        }
    actual = set()
    for assignment in of_type(
        deployment, "Microsoft.DocumentDB/databaseAccounts/sqlRoleAssignments"
    ):
        properties = assignment.properties
        definition = str(properties["roleDefinitionId"])
        assert definition.startswith(f"{account_id}/sqlRoleDefinitions/")
        role = definition.rsplit("/", 1)[-1]
        assert str(assignment.name).rsplit("/", 1)[-1] == guid(
            account_id, properties["principalId"], role, properties["scope"]
        )
        actual.add((str(properties["principalId"]), role, properties["scope"]))
    assert actual == expected
    assert deployment.outputs()["DOCCHAT_COSMOS_GROUP_DATA_ACCESS"] is group_access
    assert len(str(deployment.outputs()["DOCCHAT_COSMOS_ROLE_ASSIGNMENT_IDS"]).split(",")) == len(
        expected
    )


# 83-infrastructure.md section 6.11: the roles the deploy identity's conditioned Role Based Access
# Control Administrator may assign at or under the resource group; AcrPull on the registry.
CONDITION_ROLES = {
    BLOB_CONTRIBUTOR,
    BLOB_READER,
    BLOB_OWNER,
    QUEUE_CONTRIBUTOR,
    QUEUE_READER,
    QUEUE_PROCESSOR,
    QUEUE_SENDER,
    TABLE_CONTRIBUTOR,
    SEARCH_DATA_READER,
    SEARCH_DATA_CONTRIBUTOR,
    OPENAI_USER,
    COGNITIVE_SERVICES_USER,
    METRICS_PUBLISHER,
    "4633458b-17de-408a-b874-0445c86b69e6",  # Key Vault Secrets User
    READER,
    LOG_ANALYTICS_READER,
    "1e7ca9b1-60d1-4db8-a914-f2ca1ff27c40",  # Defender for Storage Data Scanner
    SEARCH_INDEX_MANAGER,
    BLOB_TAG_READER,
}


@pytest.mark.parametrize("environment", ENVIRONMENTS)
def test_t425_every_role_is_inside_the_deploy_identity_condition(
    main_deployment: MainFactory, environment: str
) -> None:
    assignments = of_type(main_deployment(environment), "Microsoft.Authorization/roleAssignments")
    for assignment in assignments:
        role = str(assignment.properties["roleDefinitionId"]).rsplit("/", 1)[-1]
        if assignment.resource_group == "rg-docchat-shared":
            assert role == ACR_PULL
        else:
            assert role in CONDITION_ROLES
