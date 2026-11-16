"""T-425 for the bootstrap: `id-docchat-citest`'s rights in the compiled
`infra/bootstrap/assignments.bicep` against `83-infrastructure.md` section 6.8.

The CI test identity holds Key Vault Secrets User and, by default, DocChat Test Vault Firewall
Editor on the test vault, Log Analytics Reader and DocChat Load Test Operator at `rg-docchat-load`,
and Log Analytics Reader on `log-docchat-dev` alone, never at `rg-docchat-dev` itself, and nothing
in `rg-docchat-prod`. The workspace is `main.bicep`'s, so the bootstrap's first run, before `dev`'s
first provision, leaves that one assignment out (section 11.5 step 2).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Final

import pytest
from arm_template import SUBSCRIPTION_ID, Deployment, guid, load_deployment
from compiled_support import ENVIRONMENTS, load_json, of_type

ROLE_ASSIGNMENTS: Final = "Microsoft.Authorization/roleAssignments"
LOG_ANALYTICS_READER: Final = "73c42c96-874c-492b-b04d-ab87d138a893"
LOAD_TEST_OPERATOR: Final = "cc43bd13-3731-4abc-9386-d2a7c3e6fcb4"
SECRETS_USER: Final = "4633458b-17de-408a-b874-0445c86b69e6"
VAULT_FIREWALL_EDITOR: Final = "28936cba-1b39-4715-987a-4fc5adcdc5ab"

CITEST: Final = "20000000-0000-0000-0000-0000000000c1"
PRINCIPALS: Final = {
    "deployDev": "20000000-0000-0000-0000-0000000000d1",
    "deployProd": "20000000-0000-0000-0000-0000000000d2",
    "deployLoad": "20000000-0000-0000-0000-0000000000d3",
    "pr": "20000000-0000-0000-0000-0000000000e1",
    "citest": CITEST,
}
DEV_WORKSPACE: Final = (
    f"/subscriptions/{SUBSCRIPTION_ID}/resourceGroups/rg-docchat-dev"
    "/providers/Microsoft.OperationalInsights/workspaces/log-docchat-dev"
)


def assignments(compiled_dir: Path, **parameters: Any) -> Deployment:
    """The bootstrap's assignments step for a subscription holding every environment group and
    the home groups (the default of section 1.2)."""
    template = load_json(compiled_dir / "bootstrap" / "assignments.json")
    values = {"environmentNames": list(ENVIRONMENTS), "pipelinePrincipalIds": PRINCIPALS}
    return load_deployment(template, values | parameters, resource_group="subscription")


def role(assignment: Any) -> str:
    return str(assignment.properties["roleDefinitionId"]).rsplit("/", 1)[-1]


def held_by(deployment: Deployment, principal: str) -> set[tuple[str, str, str]]:
    """(resource group, scope, role) of every assignment to `principal`; the scope is `group`
    for the resource group itself, otherwise the scoped resource's ID."""
    return {
        (
            str(assignment.resource_group),
            str(assignment.field("scope") or "group"),
            role(assignment),
        )
        for assignment in of_type(deployment, ROLE_ASSIGNMENTS)
        if assignment.properties["principalId"] == principal
    }


def citest_in(held: set[tuple[str, str, str]], group: str) -> set[tuple[str, str]]:
    return {(scope, role_id) for in_group, scope, role_id in held if in_group == group}


def test_t425_the_ci_test_identity_reads_the_dev_workspace_alone(compiled_dir: Path) -> None:
    held = held_by(assignments(compiled_dir, devWorkspaceExists=True), CITEST)

    assert citest_in(held, "rg-docchat-dev") == {(DEV_WORKSPACE, LOG_ANALYTICS_READER)}
    assert citest_in(held, "rg-docchat-prod") == set()
    assert citest_in(held, "rg-docchat-load") == {
        ("group", LOG_ANALYTICS_READER),
        ("group", LOAD_TEST_OPERATOR),
    }
    shared = citest_in(held, "rg-docchat-shared")
    assert {role_id for _, role_id in shared} == {SECRETS_USER, VAULT_FIREWALL_EDITOR}
    assert all("/providers/Microsoft.KeyVault/vaults/kv-docchat-tst-" in s for s, _ in shared)


def test_the_dev_workspace_assignment_is_named_and_typed_as_section_6_3_says(
    compiled_dir: Path,
) -> None:
    deployment = assignments(compiled_dir, devWorkspaceExists=True)
    (assignment,) = [
        item
        for item in of_type(deployment, ROLE_ASSIGNMENTS)
        if item.properties["principalId"] == CITEST and item.resource_group == "rg-docchat-dev"
    ]
    assert str(assignment.field("scope")) == DEV_WORKSPACE
    assert str(assignment.name) == guid(DEV_WORKSPACE, CITEST, LOG_ANALYTICS_READER)
    assert assignment.properties["principalType"] == "ServicePrincipal"
    assert assignment.properties["roleDefinitionId"] == (
        f"/subscriptions/{SUBSCRIPTION_ID}/providers/Microsoft.Authorization/roleDefinitions/"
        f"{LOG_ANALYTICS_READER}"
    )
    assert assignment.properties.get("condition") is None


def test_the_first_run_before_dev_exists_leaves_the_workspace_assignment_out(
    compiled_dir: Path,
) -> None:
    held = held_by(assignments(compiled_dir), CITEST)

    assert citest_in(held, "rg-docchat-dev") == set()
    assert citest_in(held, "rg-docchat-load") == {
        ("group", LOG_ANALYTICS_READER),
        ("group", LOAD_TEST_OPERATOR),
    }


def test_a_subscription_without_dev_assigns_nothing_on_a_dev_workspace(
    compiled_dir: Path,
) -> None:
    deployment = assignments(
        compiled_dir, environmentNames=["prod"], devWorkspaceExists=True, homeSubscriptionId="x"
    )
    assert all(
        "log-docchat-dev" not in str(item.field("scope") or "")
        for item in of_type(deployment, ROLE_ASSIGNMENTS)
    )


@pytest.mark.parametrize("exists", [True, False])
def test_no_other_principal_gains_a_right_on_the_dev_workspace(
    compiled_dir: Path, exists: bool
) -> None:
    deployment = assignments(compiled_dir, devWorkspaceExists=exists)
    on_workspace = {
        str(item.properties["principalId"])
        for item in of_type(deployment, ROLE_ASSIGNMENTS)
        if str(item.field("scope") or "") == DEV_WORKSPACE
    }
    assert on_workspace == ({CITEST} if exists else set())
