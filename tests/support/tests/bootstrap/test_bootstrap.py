"""Unit tests of scripts/bootstrap/bootstrap.py (83-infrastructure.md section 11; T-458)."""

from __future__ import annotations

import importlib
import json
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from bootstrap_harness import (
    APPROVERS,
    GRAPH,
    OPERATOR,
    ORGANIZATION,
    REPOSITORY,
    SUBSCRIPTION,
    TENANT,
    VAULT_ID,
    VAULT_NAME,
    config_data,
    graph_get,
    graph_write,
    group_lookup,
    oidc_template,
    record_group,
    record_identity,
    uid,
    vault_lookup,
)

if TYPE_CHECKING:
    from bootstrap_harness import Harness

bootstrap = importlib.import_module("bootstrap")

DEPLOY = {"dev": uid(101), "prod": uid(102), "load": uid(103)}
PR = uid(104)
CITEST = uid(105)


def run(harness: Harness, *argv: str) -> int:
    code: int = bootstrap.main(list(argv), harness.runtime())
    return code


def deployment_calls(harness: Harness) -> list[Any]:
    return harness.runner.called("az", "deployment")


# Prerequisites -----------------------------------------------------------------------------------


def test_t458_prerequisites_stop_an_approver_listed_among_operators(harness: Harness) -> None:
    harness.write_config(config_data(operators=[OPERATOR, APPROVERS["prod"][1]]))

    assert run(harness, "--step", "prerequisites") == 2

    refusal = harness.refusal()
    assert refusal["code"] == "approver_is_operator"
    assert refusal["message"] == bootstrap.MESSAGES["approver_is_operator"]
    assert "prod" in refusal["detail"]
    assert harness.runner.calls == []


def test_t458_prerequisites_stop_fewer_than_two_group_owners(harness: Harness) -> None:
    harness.write_config(config_data(groupOwners=[uid(21)]))

    assert run(harness, "--step", "prerequisites") == 2

    assert harness.refusal()["code"] == "missing_prerequisite"
    assert "groupOwners" in harness.refusal()["detail"]
    assert harness.runner.calls == []


def test_prerequisites_stop_an_environment_with_one_approver(harness: Harness) -> None:
    approvers = {**APPROVERS, "load": [uid(15)]}
    harness.write_config(config_data(approverObjectIds=approvers))

    assert run(harness, "--step", "prerequisites") == 2

    assert harness.refusal()["code"] == "missing_prerequisite"
    assert "approverObjectIds.load" in harness.refusal()["detail"]


def test_prerequisites_stop_without_a_security_mailbox(harness: Harness) -> None:
    harness.write_config(config_data(securityAlertEmails=[]))

    assert run(harness, "--step", "prerequisites") == 2

    assert harness.refusal()["code"] == "missing_prerequisite"
    assert "securityAlertEmails" in harness.refusal()["detail"]


def record_tools_and_roles(harness: Harness, *, az_version: str = "2.91.0") -> None:
    runner = harness.runner
    runner.answer_json(("az", "version", "--output", "json"), {"azure-cli": az_version})
    runner.answer(("az", "bicep", "version"), "Bicep CLI version 0.47.16 (3f73e1a234)\n")
    runner.answer(("gh", "api", "user", "--jq", ".login"), "octocat\n")
    runner.answer(
        ("gh", "api", f"/orgs/{ORGANIZATION}/memberships/octocat", "--jq", ".role"), "admin\n"
    )
    runner.answer(
        ("az", "ad", "signed-in-user", "show", "--query", "id", "--output", "tsv"), uid(9)
    )
    runner.answer(
        (
            "az",
            "role",
            "assignment",
            "list",
            "--assignee",
            uid(9),
            "--role",
            bootstrap.OWNER_ROLE,
            "--scope",
            f"/subscriptions/{SUBSCRIPTION}",
            "--include-inherited",
            "--include-groups",
            "--query",
            "length(@)",
            "--output",
            "tsv",
        ),
        "1\n",
    )
    runner.answer_json(
        graph_get(
            f"{GRAPH}/me/transitiveMemberOf/microsoft.graph.directoryRole?$select=roleTemplateId"
        ),
        {"value": [{"roleTemplateId": role} for role in bootstrap.ENTRA_ROLES.values()]},
    )


def test_prerequisites_pass_with_the_tools_and_the_callers_roles(harness: Harness) -> None:
    record_tools_and_roles(harness)

    assert run(harness, "--step", "prerequisites") == 0

    assert harness.reports() == [{"step": "prerequisites", "status": "succeeded"}]


def test_prerequisites_refuse_an_az_older_than_2_76(harness: Harness) -> None:
    record_tools_and_roles(harness, az_version="2.75.1")

    assert run(harness, "--step", "prerequisites") == 2

    assert harness.refusal()["code"] == "missing_prerequisite"


def test_prerequisites_turn_a_failed_tool_call_into_missing_prerequisite(harness: Harness) -> None:
    harness.runner.answer(("az", "version", "--output", "json"), returncode=127, stderr="no az\n")

    assert run(harness, "--step", "prerequisites") == 2

    assert harness.refusal()["code"] == "missing_prerequisite"


# Configuration -----------------------------------------------------------------------------------


def test_the_committed_configuration_holds_the_keys_and_no_personal_value() -> None:
    path = Path(bootstrap.REPO_ROOT) / "scripts" / "bootstrap" / "bootstrap.config.json"
    committed = json.loads(path.read_text(encoding="utf-8"))

    assert set(committed) == set(bootstrap.CONFIG_KEYS)
    assert committed["ciPrivateNetworking"] is False
    assert committed["ciNetworkLocation"] == "eastus2"
    assert committed["operators"] == []
    assert committed["groupOwners"] == []
    assert committed["securityAlertEmails"] == []
    assert committed["approverObjectIds"] == {"dev": [], "prod": [], "load": []}
    assert len(committed["testUserAliases"]) == 25
    assert not re.search(r"\b\d{1,3}(\.\d{1,3}){3}\b", path.read_text(encoding="utf-8"))


def test_an_unfilled_configuration_stops_every_step(harness: Harness) -> None:
    harness.write_config(config_data(tenantId=""))

    assert run(harness, "--step", "checklist") == 2

    assert harness.refusal()["code"] == "missing_prerequisite"
    assert "tenantId" in harness.refusal()["detail"]


def test_an_unknown_configuration_key_is_refused(harness: Harness) -> None:
    harness.write_config({**config_data(), "operator": []})

    assert run(harness, "--step", "checklist") == 2

    assert "operator" in harness.refusal()["detail"]


def test_the_checklist_prints_the_nine_post_provision_steps(harness: Harness) -> None:
    assert run(harness, "--step", "checklist") == 0

    printed = harness.out.getvalue()
    assert all(f"\n{number}. " in printed for number in range(1, 10))
    assert harness.reports() == [{"step": "checklist", "status": "succeeded"}]
    assert harness.runner.calls == []


def test_a_refusal_is_also_written_to_the_job_summary(harness: Harness, tmp_path: Path) -> None:
    harness.write_config(config_data(groupOwners=[]))
    summary = tmp_path / "summary.md"
    runtime = bootstrap.Runtime(
        runner=harness.runner,
        repo_root=harness.repo,
        environ={"GITHUB_STEP_SUMMARY": str(summary)},
        out=harness.out,
        err=harness.err,
    )

    assert bootstrap.main(["--step", "prerequisites"], runtime) == 2

    assert json.loads(summary.read_text(encoding="utf-8"))["code"] == "missing_prerequisite"


# Identity and OIDC subjects ----------------------------------------------------------------------


def identity_deployment(harness: Harness) -> tuple[str, ...]:
    return (
        "az",
        "deployment",
        "group",
        "create",
        "--name",
        "docchat-bootstrap-identity",
        "--resource-group",
        "rg-docchat-identity",
        "--subscription",
        SUBSCRIPTION,
        "--parameters",
        harness.template("identity.bicepparam"),
        "--output",
        "none",
    )


def test_t458_identity_step_stops_when_two_identities_trust_one_subject(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = (
        *bootstrap.FEDERATED_CREDENTIALS,
        ("id-docchat-citest", "github-environment-dev", "environment:dev"),
    )
    monkeypatch.setattr(bootstrap, "FEDERATED_CREDENTIALS", plan)
    oidc_template(harness.runner, {"use_default": True})

    assert run(harness, "--step", "identity") == 2

    refusal = harness.refusal()
    assert refusal["code"] == "oidc_subject_shared"
    assert "id-docchat-deploy-dev" in refusal["detail"]
    assert "id-docchat-citest" in refusal["detail"]
    assert f"repo:{ORGANIZATION}/{REPOSITORY}:environment:dev" in refusal["detail"]
    assert deployment_calls(harness) == []


def test_t458_shared_subject_check_names_both_identities() -> None:
    plan = [
        {"identity": "id-docchat-deploy-dev", "name": "a", "subject": "repo:o/r:environment:dev"},
        {"identity": "id-docchat-pr", "name": "b", "subject": "repo:o/r:environment:dev"},
    ]

    with pytest.raises(bootstrap.ScriptError) as raised:
        bootstrap.check_unique_subjects(plan)

    assert raised.value.code == "oidc_subject_shared"


def test_the_shipped_credentials_give_each_identity_its_own_subjects() -> None:
    plan = bootstrap.federated_credential_plan("repo:contoso/docchat")

    bootstrap.check_unique_subjects(plan)
    assert [credential["subject"] for credential in plan] == [
        "repo:contoso/docchat:environment:dev",
        "repo:contoso/docchat:environment:prod",
        "repo:contoso/docchat:environment:load",
        "repo:contoso/docchat:pull_request",
        "repo:contoso/docchat:environment:dev-tests",
        "repo:contoso/docchat:environment:load-tests",
    ]
    citest = [c["name"] for c in plan if c["identity"] == "id-docchat-citest"]
    assert citest == ["github-environment-dev-tests", "github-environment-load-tests"]


def test_identity_step_deploys_exactly_the_checked_plan(harness: Harness) -> None:
    oidc_template(harness.runner, {"use_default": True})
    harness.runner.answer(identity_deployment(harness))

    assert run(harness, "--step", "identity") == 0

    (call,) = deployment_calls(harness)
    assert call.env is not None
    deployed = json.loads(call.env["DOCCHAT_BOOTSTRAP_FEDERATED_CREDENTIALS"])
    assert deployed == bootstrap.federated_credential_plan(f"repo:{ORGANIZATION}/{REPOSITORY}")


def test_identity_step_uses_the_immutable_subject_prefix(harness: Harness) -> None:
    oidc_template(harness.runner, {"use_default": True, "use_immutable_subject": True})
    harness.runner.answer_json(
        ("gh", "api", f"/repos/{ORGANIZATION}/{REPOSITORY}"),
        {"id": 42, "name": REPOSITORY, "owner": {"login": ORGANIZATION, "id": 7}},
    )
    harness.runner.answer(identity_deployment(harness))

    assert run(harness, "--step", "identity") == 0

    (call,) = deployment_calls(harness)
    subjects = [
        c["subject"] for c in json.loads(call.env["DOCCHAT_BOOTSTRAP_FEDERATED_CREDENTIALS"])
    ]
    assert subjects[0] == "repo:contoso@7/docchat@42:environment:dev"


def test_a_customised_subject_template_stops_the_identity_step(harness: Harness) -> None:
    oidc_template(harness.runner, {"use_default": False, "include_claim_keys": ["repo", "context"]})

    assert run(harness, "--step", "identity") == 2

    assert harness.refusal()["code"] == "oidc_subject_customised"
    assert deployment_calls(harness) == []


def test_identity_step_retries_a_409_twenty_seconds_apart(harness: Harness) -> None:
    oidc_template(harness.runner, {"use_default": True})
    conflict = (
        "ERROR: (Conflict) ConcurrentFederatedIdentityCredentialsWritesForSingleManagedIdentity\n"
    )
    harness.runner.answer(identity_deployment(harness), returncode=1, stderr=conflict)
    harness.runner.answer(identity_deployment(harness), returncode=1, stderr=conflict)
    harness.runner.answer(identity_deployment(harness))

    assert run(harness, "--step", "identity") == 0

    assert len(deployment_calls(harness)) == 3
    assert harness.sleeps == [20.0, 20.0]


def test_identity_step_gives_up_after_six_retries(harness: Harness) -> None:
    oidc_template(harness.runner, {"use_default": True})
    harness.runner.answer(identity_deployment(harness), returncode=1, stderr="Conflict\n")

    assert run(harness, "--step", "identity") == 1

    assert len(deployment_calls(harness)) == 7
    assert harness.err.getvalue() == "Conflict\n"


def test_any_other_deployment_error_is_relayed_at_once(harness: Harness) -> None:
    oidc_template(harness.runner, {"use_default": True})
    harness.runner.answer(identity_deployment(harness), returncode=3, stderr="InvalidTemplate\n")

    assert run(harness, "--step", "identity") == 3

    assert len(deployment_calls(harness)) == 1
    assert harness.err.getvalue() == "InvalidTemplate\n"


# Resource groups, providers and roles ------------------------------------------------------------


def test_resource_groups_create_missing_groups_and_merge_tags_into_existing_ones(
    harness: Harness,
) -> None:
    harness.write_config(config_data(location="westus3"))
    plan = bootstrap.resource_group_plan(bootstrap.load_config(harness.config_path))
    for name, subscription, location, tags in plan:
        exists = name == "rg-docchat-load"
        harness.runner.answer(
            (
                "az",
                "group",
                "exists",
                "--name",
                name,
                "--subscription",
                subscription,
                "--output",
                "tsv",
            ),
            "true\n" if exists else "false\n",
        )
        tag_args = [f"{key}={value}" for key, value in tags.items()]
        if exists:
            harness.runner.answer(
                (
                    "az",
                    "tag",
                    "update",
                    "--resource-id",
                    f"/subscriptions/{subscription}/resourceGroups/{name}",
                    "--operation",
                    "Merge",
                    "--tags",
                    *tag_args,
                    "--output",
                    "none",
                )
            )
        else:
            harness.runner.answer(
                (
                    "az",
                    "group",
                    "create",
                    "--name",
                    name,
                    "--location",
                    location,
                    "--subscription",
                    subscription,
                    "--tags",
                    *tag_args,
                    "--output",
                    "none",
                )
            )

    assert run(harness, "--step", "resource-groups") == 0

    created = harness.runner.called("az", "group", "create")
    assert [call.args[4] for call in created] == [
        "rg-docchat-identity",
        "rg-docchat-shared",
        "rg-docchat-dev",
        "rg-docchat-prod",
    ]
    assert [call.args[6] for call in created] == ["eastus2", "eastus2", "westus3", "westus3"]
    assert "docchat-purpose=identity" in created[0].args
    assert "docchat-env=shared" in created[1].args
    assert "docchat-purpose=environment" in created[2].args
    (merged,) = harness.runner.called("az", "tag", "update")
    assert merged.args[-4:-2] == ("docchat-env=load", "docchat-purpose=environment")


def test_providers_register_only_what_isnt_registered(harness: Harness) -> None:
    for namespace in bootstrap.PROVIDERS:
        show = (
            "az",
            "provider",
            "show",
            "--namespace",
            namespace,
            "--subscription",
            SUBSCRIPTION,
            "--query",
            "registrationState",
            "--output",
            "tsv",
        )
        if namespace == "Microsoft.Search":
            harness.runner.answer(show, "NotRegistered\n")
        harness.runner.answer(show, "Registered\n")
    harness.runner.answer(
        (
            "az",
            "provider",
            "register",
            "--namespace",
            "Microsoft.Search",
            "--subscription",
            SUBSCRIPTION,
            "--wait",
            "--output",
            "none",
        )
    )

    assert run(harness, "--step", "providers") == 0

    registered = harness.runner.called("az", "provider", "register")
    assert [call.args[4] for call in registered] == ["Microsoft.Search"]
    assert not any("GitHub.Network" in call.args for call in harness.runner.calls)


def compiled_roles(harness: Harness, actions: list[str]) -> None:
    template = {
        "variables": {
            "roles": [
                {"roleName": "DocChat Example", "actions": actions, "dataActions": []},
            ]
        }
    }
    harness.runner.answer_json(
        ("az", "bicep", "build", "--file", harness.template("roles.bicep"), "--stdout"), template
    )


def operations(namespace: str, names: list[str]) -> dict[str, Any]:
    return {
        "name": namespace,
        "operations": [{"name": names[0]}],
        "resourceTypes": [{"name": "x", "operations": [{"name": name} for name in names[1:]]}],
    }


def test_roles_step_stops_on_an_unknown_action_and_creates_nothing(harness: Harness) -> None:
    compiled_roles(
        harness,
        [
            "Microsoft.Search/searchServices/indexes/read",
            "Microsoft.Search/searchServices/indexes/purge",
        ],
    )
    harness.runner.answer_json(
        (
            "az",
            "provider",
            "operation",
            "show",
            "--namespace",
            "Microsoft.Search",
            "--output",
            "json",
        ),
        operations(
            "Microsoft.Search",
            ["Microsoft.Search/register/action", "Microsoft.Search/searchServices/indexes/read"],
        ),
    )

    assert run(harness, "--step", "roles") == 2

    assert harness.refusal()["code"] == "unknown_action"
    assert "indexes/purge" in harness.refusal()["detail"]
    assert deployment_calls(harness) == []


def test_roles_step_validates_every_action_then_deploys(harness: Harness) -> None:
    compiled_roles(harness, ["microsoft.search/searchServices/indexes/READ"])
    harness.runner.answer_json(
        (
            "az",
            "provider",
            "operation",
            "show",
            "--namespace",
            "microsoft.search",
            "--output",
            "json",
        ),
        operations(
            "Microsoft.Search",
            ["Microsoft.Search/register/action", "Microsoft.Search/searchServices/indexes/read"],
        ),
    )
    harness.runner.answer(
        (
            "az",
            "deployment",
            "sub",
            "create",
            "--name",
            "docchat-bootstrap-roles",
            "--location",
            "eastus2",
            "--subscription",
            SUBSCRIPTION,
            "--parameters",
            harness.template("roles.bicepparam"),
            "--output",
            "none",
        )
    )

    assert run(harness, "--step", "roles") == 0

    assert len(deployment_calls(harness)) == 1


# Policies ----------------------------------------------------------------------------------------


def compiled_policies(harness: Harness, definitions: list[str]) -> None:
    harness.runner.answer_json(
        ("az", "bicep", "build", "--file", harness.template("policies.bicep"), "--stdout"),
        {"variables": {"builtInPolicies": [{"definition": d} for d in definitions]}},
    )


def policy_show(definition: str) -> tuple[str, ...]:
    return ("az", "policy", "definition", "show", "--name", definition, "--output", "json")


def test_policies_step_stops_on_a_definition_that_cant_deny(harness: Harness) -> None:
    compiled_policies(harness, ["8c6a50c6-9ffd-4ae7-986f-5fa6111f9a54"])
    harness.runner.answer_json(
        policy_show("8c6a50c6-9ffd-4ae7-986f-5fa6111f9a54"),
        {"parameters": {"effect": {"allowedValues": ["Audit", "Disabled"]}}},
    )

    assert run(harness, "--step", "policies") == 2

    assert harness.refusal()["code"] == "policy_definition_missing"
    assert deployment_calls(harness) == []


def test_policies_step_stops_on_a_missing_definition(harness: Harness) -> None:
    compiled_policies(harness, ["12d4fa5e-1f9f-4c21-97a9-b99b3c6611b5"])
    harness.runner.answer(
        policy_show("12d4fa5e-1f9f-4c21-97a9-b99b3c6611b5"),
        returncode=3,
        stderr="PolicyDefinitionNotFound\n",
    )

    assert run(harness, "--step", "policies") == 2

    assert harness.refusal()["code"] == "policy_definition_missing"


def test_policies_step_deploys_once_per_subscription_in_use(harness: Harness) -> None:
    prod_subscription = uid(3000)
    harness.write_config(
        config_data(
            subscriptions={"dev": SUBSCRIPTION, "prod": prod_subscription, "load": SUBSCRIPTION}
        )
    )
    compiled_policies(harness, ["b3a22bc9-66de-45fb-98fa-00f5df42f41a"])
    harness.runner.answer_json(
        policy_show("b3a22bc9-66de-45fb-98fa-00f5df42f41a"),
        {
            "properties": {
                "parameters": {"Effect": {"allowedValues": ["Audit", "Deny", "Disabled"]}}
            }
        },
    )
    for subscription in (SUBSCRIPTION, prod_subscription):
        harness.runner.answer(
            (
                "az",
                "deployment",
                "sub",
                "create",
                "--name",
                "docchat-bootstrap-policies",
                "--location",
                "eastus2",
                "--subscription",
                subscription,
                "--parameters",
                harness.template("policies.bicepparam"),
                "--output",
                "none",
            )
        )

    assert run(harness, "--step", "policies") == 0

    environments = [
        call.env["DOCCHAT_BOOTSTRAP_ENVIRONMENTS"] for call in deployment_calls(harness)
    ]
    assert environments == ["dev,load", "prod"]


# Shared, groups and the Graph permission ---------------------------------------------------------


def test_shared_step_passes_the_ci_and_allowlist_settings(harness: Harness) -> None:
    harness.write_config(config_data(ciPrivateNetworking=True, ciNetworkLocation="westus2"))
    args = (
        "az",
        "deployment",
        "group",
        "create",
        "--name",
        "docchat-bootstrap-shared",
        "--resource-group",
        "rg-docchat-shared",
        "--subscription",
        SUBSCRIPTION,
        "--parameters",
        str(harness.repo / "infra" / "shared.bicepparam"),
        "--output",
        "none",
    )
    harness.runner.answer(args)
    runtime = bootstrap.Runtime(
        runner=harness.runner,
        repo_root=harness.repo,
        environ={"DOCCHAT_SHARED_OPERATOR_IP_ALLOWLIST": "203.0.113.7"},
        out=harness.out,
        err=harness.err,
    )

    assert bootstrap.main(["--step", "shared"], runtime) == 0

    (call,) = deployment_calls(harness)
    assert call.env == {
        "DOCCHAT_CI_PRIVATE_NETWORKING": "true",
        "DOCCHAT_CI_NETWORK_LOCATION": "westus2",
        "DOCCHAT_SHARED_OPERATOR_IP_ALLOWLIST": "203.0.113.7",
        "DOCCHAT_SECURITY_WORKSPACE_ID": (
            f"/subscriptions/{SUBSCRIPTION}/resourceGroups/rg-docchat-identity"
            "/providers/Microsoft.OperationalInsights/workspaces/log-docchat-security"
        ),
    }


def test_ensure_group_creates_a_missing_group_and_adds_only_missing_owners(
    harness: Harness,
) -> None:
    name = "sg-docchat-dev-config-operators"
    record_group(harness.runner, name, None)
    body = {
        "displayName": name,
        "mailNickname": name,
        "securityEnabled": True,
        "mailEnabled": False,
        "isAssignableToRole": False,
    }
    harness.runner.answer_json(graph_write("POST", f"{GRAPH}/groups", body), {"id": uid(500)})
    harness.runner.answer_json(
        graph_get(f"{GRAPH}/groups/{uid(500)}/owners?$select=id"), {"value": [{"id": uid(11)}]}
    )
    harness.runner.answer(
        graph_write(
            "POST",
            f"{GRAPH}/groups/{uid(500)}/owners/$ref",
            {"@odata.id": f"{GRAPH}/directoryObjects/{uid(12)}"},
        )
    )
    context = context_for(harness)

    assert bootstrap.ensure_group(context, name, owners=APPROVERS["dev"]) == uid(500)

    added = [call for call in harness.runner.calls if call.args[5].endswith("/owners/$ref")]
    assert len(added) == 1


def context_for(harness: Harness) -> Any:
    return bootstrap.Context(
        repo_root=harness.repo,
        config=bootstrap.load_config(harness.config_path),
        tools=bootstrap.Tools(harness.runner),
        environ={},
        out=harness.out,
        err=harness.err,
        sleep=harness.sleep,
        clock=harness.clock,
    )


def test_graph_permission_is_retried_for_five_minutes_then_refused(harness: Harness) -> None:
    harness.runner.answer_json(
        graph_get(
            f"{GRAPH}/servicePrincipals(appId='00000003-0000-0000-c000-000000000000')?$select=id"
        ),
        {"id": uid(600)},
    )
    record_identity(harness.runner, "id-docchat-deploy-dev", uid(201), DEPLOY["dev"])
    harness.runner.answer(
        graph_get(f"{GRAPH}/servicePrincipals/{DEPLOY['dev']}/appRoleAssignments"),
        returncode=1,
        stderr="Request_ResourceNotFound\n",
    )

    assert run(harness, "--step", "graph-permission") == 2

    assert harness.refusal()["code"] == "graph_permission_failed"
    assert sum(harness.sleeps) >= 300
    assert set(harness.sleeps) == {20.0}


def test_graph_permission_skips_an_identity_that_already_holds_it(harness: Harness) -> None:
    harness.runner.answer_json(
        graph_get(
            f"{GRAPH}/servicePrincipals(appId='00000003-0000-0000-c000-000000000000')?$select=id"
        ),
        {"id": uid(600)},
    )
    for environment, principal in DEPLOY.items():
        record_identity(harness.runner, f"id-docchat-deploy-{environment}", uid(201), principal)
        held = [{"appRoleId": bootstrap.APPLICATION_READWRITE_OWNEDBY, "resourceId": uid(600)}]
        harness.runner.answer_json(
            graph_get(f"{GRAPH}/servicePrincipals/{principal}/appRoleAssignments"),
            {"value": held if environment != "load" else []},
        )
    harness.runner.answer(
        graph_write(
            "POST",
            f"{GRAPH}/servicePrincipals/{uid(600)}/appRoleAssignedTo",
            {
                "principalId": DEPLOY["load"],
                "resourceId": uid(600),
                "appRoleId": bootstrap.APPLICATION_READWRITE_OWNEDBY,
            },
        ),
        "{}",
    )

    assert run(harness, "--step", "graph-permission") == 0

    posts = [call for call in harness.runner.calls if "POST" in call.args]
    assert len(posts) == 1


# Assignments, security and config ----------------------------------------------------------------


def record_pipeline_identities(harness: Harness) -> None:
    for environment, principal in DEPLOY.items():
        record_identity(harness.runner, f"id-docchat-deploy-{environment}", uid(210), principal)
    record_identity(harness.runner, "id-docchat-pr", uid(211), PR)
    record_identity(harness.runner, "id-docchat-citest", uid(212), CITEST)


def assignments_deployment() -> tuple[str, ...]:
    return ("az", "deployment", "sub", "create", "--name", "docchat-bootstrap-assignments")


DEV_WORKSPACE_ID = (
    f"/subscriptions/{SUBSCRIPTION}/resourceGroups/rg-docchat-dev"
    "/providers/Microsoft.OperationalInsights/workspaces/log-docchat-dev"
)


def record_dev_workspace(harness: Harness, *, exists: bool) -> None:
    harness.runner.answer(
        (
            "az",
            "resource",
            "list",
            "--resource-group",
            "rg-docchat-dev",
            "--subscription",
            SUBSCRIPTION,
            "--resource-type",
            "Microsoft.OperationalInsights/workspaces",
            "--name",
            "log-docchat-dev",
            "--query",
            "[].id",
            "--output",
            "tsv",
        ),
        f"{DEV_WORKSPACE_ID}\n" if exists else "",
    )


@pytest.mark.parametrize("exists", [True, False], ids=["after dev's provision", "before it"])
def test_assignments_step_passes_principals_and_groups(harness: Harness, exists: bool) -> None:
    record_pipeline_identities(harness)
    record_group(harness.runner, "sg-docchat-operators", uid(300))
    record_group(harness.runner, "sg-docchat-dev-config-operators", uid(301))
    record_dev_workspace(harness, exists=exists)
    harness.runner.answer(
        (
            *assignments_deployment(),
            "--location",
            "eastus2",
            "--subscription",
            SUBSCRIPTION,
            "--parameters",
            harness.template("assignments.bicepparam"),
            "--output",
            "none",
        )
    )

    assert run(harness, "--step", "assignments") == 0

    (call,) = deployment_calls(harness)
    assert call.env["DOCCHAT_BOOTSTRAP_ENVIRONMENTS"] == "dev,prod,load"
    assert json.loads(call.env["DOCCHAT_BOOTSTRAP_PIPELINE_PRINCIPAL_IDS"]) == {
        "deployDev": DEPLOY["dev"],
        "deployProd": DEPLOY["prod"],
        "deployLoad": DEPLOY["load"],
        "pr": PR,
        "citest": CITEST,
    }
    assert call.env["DOCCHAT_BOOTSTRAP_OPERATORS_GROUP_ID"] == uid(300)
    assert call.env["DOCCHAT_BOOTSTRAP_DEV_CONFIG_OPERATORS_GROUP_ID"] == uid(301)
    # id-docchat-citest's Log Analytics Reader on log-docchat-dev needs the workspace, which only
    # dev's first provision creates (section 11.5 step 2).
    assert call.env["DOCCHAT_BOOTSTRAP_DEV_WORKSPACE_EXISTS"] == ("true" if exists else "false")
    waiting = "log-docchat-dev doesn't exist yet: run this step again after dev's first provision"
    assert (waiting in harness.out.getvalue()) is not exists
    assert harness.reports() == [{"step": "assignments", "status": "succeeded"}]
    assert harness.runner.called("az", "role", "assignment") == []


def test_assignments_step_removes_the_vault_firewall_editor_under_private_networking(
    harness: Harness,
) -> None:
    harness.write_config(config_data(ciPrivateNetworking=True))
    record_pipeline_identities(harness)
    record_group(harness.runner, "sg-docchat-operators", uid(300))
    record_group(harness.runner, "sg-docchat-dev-config-operators", uid(301))
    record_dev_workspace(harness, exists=True)
    harness.runner.answer(
        (
            *assignments_deployment(),
            "--location",
            "eastus2",
            "--subscription",
            SUBSCRIPTION,
            "--parameters",
            harness.template("assignments.bicepparam"),
            "--output",
            "none",
        )
    )
    harness.runner.answer_json(vault_lookup(), [{"id": VAULT_ID, "name": VAULT_NAME}])
    harness.runner.answer(
        (
            "az",
            "role",
            "assignment",
            "list",
            "--assignee",
            CITEST,
            "--role",
            "28936cba-1b39-4715-987a-4fc5adcdc5ab",
            "--scope",
            VAULT_ID,
            "--subscription",
            SUBSCRIPTION,
            "--query",
            "[].id",
            "--output",
            "tsv",
        ),
        f"{VAULT_ID}/providers/Microsoft.Authorization/roleAssignments/{uid(700)}\n",
    )
    harness.runner.answer(
        (
            "az",
            "role",
            "assignment",
            "delete",
            "--ids",
            f"{VAULT_ID}/providers/Microsoft.Authorization/roleAssignments/{uid(700)}",
            "--output",
            "none",
        )
    )

    assert run(harness, "--step", "assignments") == 0

    assert len(harness.runner.called("az", "role", "assignment", "delete")) == 1


def test_security_step_seeds_the_allowed_principals_and_deploys_the_alerts(
    harness: Harness,
) -> None:
    record_pipeline_identities(harness)
    record_group(harness.runner, "sg-docchat-operators", uid(300))
    for number, environment in enumerate(("dev", "prod", "load")):
        for offset, role in enumerate(
            ("conversation-readers", "user-data-operators", "config-operators")
        ):
            record_group(
                harness.runner, f"sg-docchat-{environment}-{role}", uid(310 + 3 * number + offset)
            )
    harness.runner.answer(
        (
            "az",
            "deployment",
            "sub",
            "create",
            "--name",
            "docchat-bootstrap-activity-log",
            "--location",
            "eastus2",
            "--subscription",
            SUBSCRIPTION,
            "--parameters",
            harness.template("activity-log.bicepparam"),
            "--output",
            "none",
        )
    )
    workspace = (
        f"/subscriptions/{SUBSCRIPTION}/resourceGroups/rg-docchat-identity"
        "/providers/Microsoft.OperationalInsights/workspaces/log-docchat-security"
    )
    logs = [{"category": c, "enabled": True} for c in bootstrap.ENTRA_LOG_CATEGORIES]
    harness.runner.answer(
        graph_write(
            "PUT",
            bootstrap.ENTRA_DIAGNOSTIC_SETTING_URL,
            {"properties": {"workspaceId": workspace, "logs": logs}},
        ),
        "{}",
    )
    harness.runner.answer(
        (
            "az",
            "deployment",
            "group",
            "create",
            "--name",
            "docchat-bootstrap-security",
            "--resource-group",
            "rg-docchat-identity",
            "--subscription",
            SUBSCRIPTION,
            "--parameters",
            harness.template("security.bicepparam"),
            "--output",
            "none",
        )
    )

    assert run(harness, "--step", "security") == 0

    seeded = harness.read_config()["allowedRoleAssigneePrincipalIds"]
    assert seeded == [uid(300), CITEST, uid(312), uid(315), uid(318)]
    security = deployment_calls(harness)[-1]
    assert security.args[5] == "docchat-bootstrap-security"
    identities = json.loads(security.env["DOCCHAT_BOOTSTRAP_DEPLOY_IDENTITY_IDS"])
    assert [identity["principalId"] for identity in identities] == list(DEPLOY.values())
    assert len(security.env["DOCCHAT_BOOTSTRAP_JIT_GROUP_IDS"].split(",")) == 9


def test_config_step_writes_the_tenant_ids_and_prints_the_diff(harness: Harness) -> None:
    for environment in ("dev", "prod", "load"):
        path = harness.repo / "infra" / "env" / f"{environment}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"subscriptionId": "", "tenantId": "", "location": "eastus2"}))
    record_group(harness.runner, "sg-docchat-operators", uid(300))
    for number, environment in enumerate(("dev", "prod", "load")):
        roles = (
            "sql-admins",
            "users",
            "admins",
            "config-operators",
            "conversation-readers",
            "user-data-operators",
        )
        for offset, role in enumerate(roles):
            record_group(
                harness.runner, f"sg-docchat-{environment}-{role}", uid(400 + 10 * number + offset)
            )

    assert run(harness, "--step", "config") == 0

    written = json.loads((harness.repo / "infra" / "env" / "prod.json").read_text())
    assert written["subscriptionId"] == SUBSCRIPTION
    assert written["tenantId"] == TENANT
    assert written["location"] == "eastus2"
    assert written["groups"] == {
        "operators": uid(300),
        "sqlAdmins": uid(410),
        "users": uid(411),
        "admins": uid(412),
        "configOperators": uid(413),
        "conversationReaders": uid(414),
        "userDataOperators": uid(415),
    }
    assert "+++ b/infra/env/dev.json" in harness.out.getvalue()


def test_config_step_needs_the_groups_step_first(harness: Harness) -> None:
    record_group(harness.runner, "sg-docchat-operators", None)

    assert run(harness, "--step", "config") == 2

    assert harness.refusal()["code"] == "missing_prerequisite"
    assert "groups step" in harness.refusal()["detail"]


def test_github_step_relays_the_child_scripts_refusal(harness: Harness) -> None:
    script = str(harness.repo / "scripts" / "bootstrap" / "configure_github.py")
    refusal = '{"code": "github_protection_mismatch"}\n'
    harness.runner.answer((bootstrap.sys.executable, script), returncode=2, stderr=refusal)

    assert run(harness, "--step", "github") == 2

    assert harness.err.getvalue() == refusal
    assert harness.reports() == []


def test_a_failing_step_stops_the_later_steps(harness: Harness) -> None:
    harness.write_config(config_data(operators=[APPROVERS["dev"][0]]))

    assert run(harness) == 2

    assert harness.reports() == []
    assert harness.runner.calls == []


def test_group_lookup_is_url_encoded() -> None:
    assert group_lookup("sg-docchat-operators")[5] == (
        f"{GRAPH}/groups?$filter=displayName%20eq%20%27sg-docchat-operators%27&$select=id,displayName"
    )
