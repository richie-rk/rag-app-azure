"""Unit tests of scripts/bootstrap/tighten_conditions.py (83-infrastructure.md 6.11; T-439)."""

from __future__ import annotations

import importlib
import json
from typing import TYPE_CHECKING, Any

from bootstrap_harness import SUBSCRIPTION, identity_show, record_identity, uid

if TYPE_CHECKING:
    from bootstrap_harness import Call, Harness

tighten_conditions = importlib.import_module("tighten_conditions")
bootstrap = importlib.import_module("bootstrap")

API = uid(1001)
INGESTION = uid(1002)
OPERATORS = uid(1003)
CONFIG_OPERATORS = uid(1004)
USER_DATA_OPERATORS = uid(1005)
SCANNER = uid(1006)
DEPLOY = uid(1010)
RBAC_ADMINISTRATOR = "f58310d9-a9f6-439a-9e8d-f62e7b41a168"
SQL_SERVER = (
    f"/subscriptions/{SUBSCRIPTION}/resourceGroups/rg-docchat-{{env}}"
    "/providers/Microsoft.Sql/servers/sql-docchat-{env}-a1b2c3"
)
REGISTRY = (
    f"/subscriptions/{SUBSCRIPTION}/resourceGroups/rg-docchat-shared"
    "/providers/Microsoft.ContainerRegistry/registries/crdocchata1b2c3"
)
COSMOS_IDS = [f"/subscriptions/{SUBSCRIPTION}/x/sqlRoleAssignments/{uid(1100)}", f"/y/{uid(1101)}"]

EXPECTED_TWO_PAIRS = """(
 (
  !(ActionMatches{'Microsoft.Authorization/roleAssignments/write'})
 )
 OR
 (
  (
   @Request[Microsoft.Authorization/roleAssignments:RoleDefinitionId] ForAnyOfAnyValues:GuidEquals {ba92f5b4-2d11-453d-a403-e96b0029c9fe, 2a2b9908-6ea1-4ae2-8e65-a410df84e7d1}
   AND
   @Request[Microsoft.Authorization/roleAssignments:PrincipalId] ForAnyOfAnyValues:GuidEquals {00000000-0000-4000-8000-000000001001}
  )
  OR
  (
   @Request[Microsoft.Authorization/roleAssignments:RoleDefinitionId] ForAnyOfAnyValues:GuidEquals {8a0f0c08-91a1-4084-bc3d-661d67233fed}
   AND
   @Request[Microsoft.Authorization/roleAssignments:PrincipalId] ForAnyOfAnyValues:GuidEquals {00000000-0000-4000-8000-000000001004}
  )
 )
)
AND
(
 (
  !(ActionMatches{'Microsoft.Authorization/roleAssignments/delete'})
 )
 OR
 (
  (
   @Resource[Microsoft.Authorization/roleAssignments:RoleDefinitionId] ForAnyOfAnyValues:GuidEquals {ba92f5b4-2d11-453d-a403-e96b0029c9fe, 2a2b9908-6ea1-4ae2-8e65-a410df84e7d1}
   AND
   @Resource[Microsoft.Authorization/roleAssignments:PrincipalId] ForAnyOfAnyValues:GuidEquals {00000000-0000-4000-8000-000000001001}
  )
  OR
  (
   @Resource[Microsoft.Authorization/roleAssignments:RoleDefinitionId] ForAnyOfAnyValues:GuidEquals {8a0f0c08-91a1-4084-bc3d-661d67233fed}
   AND
   @Resource[Microsoft.Authorization/roleAssignments:PrincipalId] ForAnyOfAnyValues:GuidEquals {00000000-0000-4000-8000-000000001004}
  )
 )
)"""  # noqa: E501 - the condition text is compared byte for byte


def principals(**changes: Any) -> Any:
    values: dict[str, Any] = {
        "api": API,
        "ingestion": INGESTION,
        "operators": OPERATORS,
        "config_operators": CONFIG_OPERATORS,
        "user_data_operators": USER_DATA_OPERATORS,
        "scanner": SCANNER,
        "content_share_fallback": False,
        "cosmos_assignment_ids": tuple(COSMOS_IDS),
        "sql_server_id": SQL_SERVER.format(env="dev"),
    }
    return tighten_conditions.Principals(**(values | changes))


def guids(*roles: str) -> tuple[str, ...]:
    return tuple(tighten_conditions.ROLES[role] for role in roles)


def as_guid_pairs(pairs: list[Any]) -> list[tuple[tuple[str, ...], str]]:
    return [(guids(*roles), principal) for roles, principal in pairs]


def test_t439_condition_renders_byte_for_byte_with_the_config_operators_pair() -> None:
    pairs = [
        (("Storage Blob Data Contributor", "Storage Blob Data Reader"), API),
        (tighten_conditions.CONFIG_OPERATORS_ROLES, CONFIG_OPERATORS),
    ]

    assert tighten_conditions.render_condition(pairs) == EXPECTED_TWO_PAIRS


def test_t439_resource_group_pairs_follow_the_tightened_table() -> None:
    pairs = tighten_conditions.resource_group_pairs(principals())

    assert as_guid_pairs(pairs) == [
        (
            guids(
                "Storage Blob Data Contributor",
                "Storage Blob Data Reader",
                "Storage Queue Data Message Sender",
                "Search Index Data Reader",
                "Cognitive Services OpenAI User",
                "Cognitive Services User",
                "Monitoring Metrics Publisher",
            ),
            API,
        ),
        (
            guids(
                "Storage Blob Data Contributor",
                "DocChat Blob Tag Reader",
                "Storage Queue Data Reader",
                "Storage Queue Data Message Processor",
                "Storage Queue Data Message Sender",
                "Storage Blob Data Owner",
                "Storage Queue Data Contributor",
                "Storage Table Data Contributor",
                "Search Index Data Contributor",
                "DocChat Search Index Manager",
                "Cognitive Services OpenAI User",
                "Cognitive Services User",
                "Monitoring Metrics Publisher",
            ),
            INGESTION,
        ),
        (guids("Reader", "Log Analytics Reader"), OPERATORS),
        (("8a0f0c08-91a1-4084-bc3d-661d67233fed",), CONFIG_OPERATORS),
        (("2a2b9908-6ea1-4ae2-8e65-a410df84e7d1",), USER_DATA_OPERATORS),
        (("1e7ca9b1-60d1-4db8-a914-f2ca1ff27c40",), SCANNER),
    ]


def test_t439_the_scanner_pair_is_left_out_while_no_scanner_exists() -> None:
    pairs = tighten_conditions.resource_group_pairs(principals(scanner=None))

    assert [principal for _, principal in pairs] == [
        API,
        INGESTION,
        OPERATORS,
        CONFIG_OPERATORS,
        USER_DATA_OPERATORS,
    ]


def test_content_share_fallback_adds_key_vault_secrets_user_to_ingestion_only() -> None:
    pairs = tighten_conditions.resource_group_pairs(principals(content_share_fallback=True))

    assert pairs[1][0][-1] == "Key Vault Secrets User"
    assert "Key Vault Secrets User" not in pairs[0][0]


def test_the_registry_condition_allows_acrpull_for_the_two_app_identities() -> None:
    assert as_guid_pairs(tighten_conditions.registry_pairs(principals())) == [
        (("7f951dda-4ed3-4680-a7ca-43fe172d538d",), API),
        (("7f951dda-4ed3-4680-a7ca-43fe172d538d",), INGESTION),
    ]


# A whole run -------------------------------------------------------------------------------------


def az_tsv(*args: str) -> tuple[str, ...]:
    return ("az", *args, "--output", "tsv")


def record_reads(
    harness: Harness,
    environment: str,
    *,
    scanner: str | None,
    api: str = API,
    fallback: bool = False,
) -> None:
    runner = harness.runner
    env_file = harness.repo / "infra" / "env" / f"{environment}.json"
    env_file.parent.mkdir(parents=True, exist_ok=True)
    groups = {
        "operators": OPERATORS,
        "configOperators": CONFIG_OPERATORS,
        "userDataOperators": USER_DATA_OPERATORS,
    }
    env_file.write_text(json.dumps({"groups": groups}))
    group = f"rg-docchat-{environment}"
    identities = (
        f"/subscriptions/{SUBSCRIPTION}/resourceGroups/{group}"
        "/providers/Microsoft.ManagedIdentity/userAssignedIdentities"
    )
    runner.answer(
        az_tsv(
            "identity",
            "show",
            "--ids",
            f"{identities}/id-docchat-api-{environment}",
            "--query",
            "principalId",
        ),
        f"{api}\n",
    )
    runner.answer(
        az_tsv(
            "identity",
            "show",
            "--ids",
            f"{identities}/id-docchat-ingest-{environment}",
            "--query",
            "principalId",
        ),
        f"{INGESTION}\n",
    )
    scanner_args = az_tsv(
        "resource",
        "show",
        "--ids",
        f"/subscriptions/{SUBSCRIPTION}/providers/Microsoft.Security/datascanners/StorageDataScanner",
        "--query",
        "identity.principalId",
    )
    if scanner:
        runner.answer(scanner_args, f"{scanner}\n")
    else:
        runner.answer(scanner_args, returncode=3, stderr="ResourceNotFound\n")
    runner.answer(
        az_tsv(
            "resource",
            "list",
            "--resource-group",
            group,
            "--subscription",
            SUBSCRIPTION,
            "--resource-type",
            "Microsoft.Sql/servers",
            "--query",
            f"[?starts_with(name, 'sql-docchat-{environment}-')].id",
        ),
        SQL_SERVER.format(env=environment) + "\n",
    )
    runner.answer_json(
        (
            "az",
            "storage",
            "account",
            "list",
            "--resource-group",
            group,
            "--subscription",
            SUBSCRIPTION,
            "--query",
            f"[?starts_with(name, 'stdocchathost{environment}')].allowSharedKeyAccess",
            "--output",
            "json",
        ),
        [fallback],
    )
    runner.answer_json(
        (
            "az",
            "deployment",
            "group",
            "list",
            "--resource-group",
            group,
            "--subscription",
            SUBSCRIPTION,
            "--query",
            (
                "[?properties.provisioningState=='Succeeded']"
                ".{timestamp: properties.timestamp, outputs: properties.outputs}"
            ),
            "--output",
            "json",
        ),
        [
            {
                "timestamp": "2026-10-01T10:00:00Z",
                "outputs": {"docchat_cosmos_role_assignment_ids": {"value": "/old"}},
            },
            {
                "timestamp": "2026-10-02T10:00:00Z",
                "outputs": {
                    "DOCCHAT_COSMOS_ROLE_ASSIGNMENT_IDS": {
                        "type": "String",
                        "value": ",".join(COSMOS_IDS),
                    }
                },
            },
            {"timestamp": "2026-10-03T10:00:00Z", "outputs": None},
        ],
    )


class RoleAssignments:
    """The two conditioned assignments as Azure stores them; `update` replaces a condition."""

    def __init__(self, environment: str, *, store: bool = True) -> None:
        self.store = store
        self.conditions = {
            f"/subscriptions/{SUBSCRIPTION}/resourceGroups/rg-docchat-{environment}": "initial",
            REGISTRY: "initial",
        }

    def listed(self, call: Call) -> Any:
        scope = call.args[call.args.index("--scope") + 1]
        assignment = {
            "id": f"{scope}/providers/Microsoft.Authorization/roleAssignments/{uid(1200)}",
            "name": uid(1200),
            "principalId": DEPLOY,
            "roleDefinitionId": (
                f"/subscriptions/{SUBSCRIPTION}/providers/Microsoft.Authorization"
                f"/roleDefinitions/{RBAC_ADMINISTRATOR}"
            ),
            "scope": scope,
            "condition": self.conditions[scope],
            "conditionVersion": "2.0",
        }
        return bootstrap.CommandResult(0, json.dumps([assignment]), "")

    def update(self, call: Call) -> Any:
        assignment = json.loads(call.args[call.args.index("--role-assignment") + 1])
        if self.store:
            self.conditions[assignment["scope"]] = assignment["condition"]
        return bootstrap.CommandResult(0, "", "")


def record_changes(harness: Harness, environment: str, assignments: RoleAssignments) -> None:
    runner = harness.runner
    record_identity(runner, f"id-docchat-deploy-{environment}", uid(1011), DEPLOY)
    runner.answer(
        az_tsv(
            "resource",
            "list",
            "--resource-group",
            "rg-docchat-shared",
            "--subscription",
            SUBSCRIPTION,
            "--resource-type",
            "Microsoft.ContainerRegistry/registries",
            "--query",
            "[?starts_with(name, 'crdocchat')].id",
        ),
        f"{REGISTRY}\n",
    )
    runner.answer_prefix(("az", "role", "assignment", "list"), assignments.listed)
    runner.answer_prefix(("az", "role", "assignment", "update"), assignments.update)
    script = str(harness.repo / "scripts" / "bootstrap" / "bootstrap.py")
    runner.answer(
        (bootstrap.sys.executable, script, "--step", "security"),
        '{"step": "security", "status": "succeeded"}\n',
    )
    runner.answer_prefix(
        ("az", "monitor", "diagnostic-settings", "create"),
        lambda call: bootstrap.CommandResult(0, "", ""),
    )


def run(harness: Harness, environment: str) -> int:
    code: int = tighten_conditions.main(["--env", environment], harness.runtime())
    return code


def test_t439_a_run_takes_principal_ids_from_az_identity_show_and_tightens_both_conditions(
    harness: Harness,
) -> None:
    record_reads(harness, "dev", scanner=SCANNER)
    assignments = RoleAssignments("dev")
    record_changes(harness, "dev", assignments)

    assert run(harness, "dev") == 0, harness.err.getvalue()

    reads = [call.args[4] for call in harness.runner.called("az", "identity", "show", "--ids")]
    assert [read.rsplit("/", 1)[1] for read in reads] == [
        "id-docchat-api-dev",
        "id-docchat-ingest-dev",
    ]
    group_condition = assignments.conditions[
        f"/subscriptions/{SUBSCRIPTION}/resourceGroups/rg-docchat-dev"
    ]
    expected = tighten_conditions.resource_group_pairs(principals())
    assert group_condition == tighten_conditions.render_condition(expected)
    assert f"PrincipalId] ForAnyOfAnyValues:GuidEquals {{{SCANNER}}}" in group_condition
    registry_condition = assignments.conditions[REGISTRY]
    assert registry_condition == tighten_conditions.render_condition(
        tighten_conditions.registry_pairs(principals())
    )
    config = harness.read_config()
    assert config["allowedRoleAssigneePrincipalIds"] == [
        API,
        INGESTION,
        CONFIG_OPERATORS,
        USER_DATA_OPERATORS,
        SCANNER,
    ]
    assert config["templateCosmosAssignmentIds"] == COSMOS_IDS
    assert len(harness.runner.called(bootstrap.sys.executable)) == 1
    (setting,) = harness.runner.called("az", "monitor", "diagnostic-settings", "create")
    assert setting.args[7] == SQL_SERVER.format(env="dev") + "/databases/master"
    assert json.loads(setting.args[11]) == [
        {"category": "SQLSecurityAuditEvents", "enabled": True},
        {"category": "DevOpsOperationsAudit", "enabled": True},
    ]
    assert "+++ b/scripts/bootstrap/bootstrap.config.json" in harness.out.getvalue()


def test_t439_a_missing_scanner_stops_prod_with_principal_ids_missing(harness: Harness) -> None:
    record_reads(harness, "prod", scanner=None)

    assert run(harness, "prod") == 2

    refusal = harness.refusal()
    assert refusal["code"] == "principal_ids_missing"
    assert "StorageDataScanner" in refusal["detail"]
    assert harness.runner.called("az", "role") == []


def test_t439_a_missing_dev_scanner_is_left_out_with_a_notice(harness: Harness) -> None:
    record_reads(harness, "dev", scanner=None)
    assignments = RoleAssignments("dev")
    record_changes(harness, "dev", assignments)

    assert run(harness, "dev") == 0

    condition = assignments.conditions[
        f"/subscriptions/{SUBSCRIPTION}/resourceGroups/rg-docchat-dev"
    ]
    assert "1e7ca9b1-60d1-4db8-a914-f2ca1ff27c40" not in condition
    assert "once more before dev scanning" in harness.out.getvalue()


def test_a_missing_workload_identity_stops_before_any_change(harness: Harness) -> None:
    record_reads(harness, "dev", scanner=SCANNER, api="")

    assert run(harness, "dev") == 2

    assert harness.refusal()["code"] == "principal_ids_missing"
    assert "id-docchat-api-dev" in harness.refusal()["detail"]
    assert harness.runner.called("az", "role") == []


def test_a_stored_condition_that_differs_stops_with_condition_update_failed(
    harness: Harness,
) -> None:
    record_reads(harness, "dev", scanner=SCANNER)
    record_changes(harness, "dev", RoleAssignments("dev", store=False))

    assert run(harness, "dev") == 2

    assert harness.refusal()["code"] == "condition_update_failed"
    assert harness.runner.called(bootstrap.sys.executable) == []


def test_load_is_never_tightened(harness: Harness) -> None:
    exit_code = 0
    try:
        tighten_conditions.main(["--env", "load"], harness.runtime())
    except SystemExit as stop:
        exit_code = int(stop.code or 0)

    assert exit_code == 2
    assert harness.runner.calls == []


def test_the_deploy_identity_is_read_from_rg_docchat_identity() -> None:
    assert identity_show("id-docchat-deploy-prod")[6] == "rg-docchat-identity"
