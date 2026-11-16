"""Unit tests of scripts/bootstrap/configure_github.py (83-infrastructure.md 10.4; T-458)."""

from __future__ import annotations

import importlib
import json
from typing import TYPE_CHECKING, Any

from bootstrap_harness import (
    GRAPH,
    ORGANIZATION,
    REPOSITORY,
    SUBSCRIPTION,
    TENANT,
    VAULT_ID,
    VAULT_NAME,
    config_data,
    graph_get,
    oidc_template,
    record_identity,
    uid,
    vault_lookup,
)

if TYPE_CHECKING:
    from bootstrap_harness import Harness

configure_github = importlib.import_module("configure_github")

REPO = f"/repos/{ORGANIZATION}/{REPOSITORY}"
TEAMS = {"docchat-release-approvers": 31, "docchat-tenant-admins": 32}
CLIENT_IDS = {
    "id-docchat-deploy-dev": uid(801),
    "id-docchat-deploy-prod": uid(802),
    "id-docchat-deploy-load": uid(803),
    "id-docchat-pr": uid(804),
    "id-docchat-citest": uid(805),
}
DOMAIN = "contoso.onmicrosoft.com"
ENVIRONMENTS = ("dev", "prod", "load", "load-access", "dev-tests", "load-tests")


def run(harness: Harness) -> int:
    code: int = configure_github.main([], harness.runtime())
    return code


def expected_variables(private_networking: str = "false") -> list[tuple[str, ...]]:
    def variable(name: str, value: str, environment: str | None = None) -> tuple[str, ...]:
        scope = ("--env", environment) if environment else ()
        repo = f"{ORGANIZATION}/{REPOSITORY}"
        return ("gh", "variable", "set", name, "--body", value, "--repo", repo, *scope)

    calls = []
    for environment in ("dev", "prod", "load"):
        calls += [
            variable(
                "AZURE_CLIENT_ID", CLIENT_IDS[f"id-docchat-deploy-{environment}"], environment
            ),
            variable("AZURE_TENANT_ID", TENANT, environment),
            variable("AZURE_SUBSCRIPTION_ID", SUBSCRIPTION, environment),
            variable("AZURE_LOCATION", "eastus2", environment),
            variable("AZURE_RESOURCE_GROUP", f"rg-docchat-{environment}", environment),
        ]
    for environment in ("dev-tests", "load-tests"):
        calls += [
            variable("AZURE_TENANT_ID", TENANT, environment),
            variable("AZURE_SUBSCRIPTION_ID", SUBSCRIPTION, environment),
        ]
        if environment == "load-tests":
            calls += [
                variable("AZURE_LOCATION", "eastus2", environment),
                variable("AZURE_RESOURCE_GROUP", "rg-docchat-load", environment),
            ]
        calls += [
            variable("DOCCHAT_CI_TEST_CLIENT_ID", CLIENT_IDS["id-docchat-citest"], environment),
            variable(
                "DOCCHAT_TEST_VAULT_URI", f"https://{VAULT_NAME}.vault.azure.net/", environment
            ),
            variable("DOCCHAT_TEST_USER_DOMAIN", DOMAIN, environment),
        ]
    calls += [
        variable("AZURE_TENANT_ID", TENANT),
        variable("DOCCHAT_PR_CLIENT_ID", CLIENT_IDS["id-docchat-pr"]),
        variable("DOCCHAT_DEV_SUBSCRIPTION_ID", SUBSCRIPTION),
        variable("DOCCHAT_CI_PRIVATE_NETWORKING", private_networking),
    ]
    return calls


def protected_read_back(slug: str, **changes: Any) -> dict[str, Any]:
    environment: dict[str, Any] = {
        "name": "x",
        "can_admins_bypass": False,
        "protection_rules": [
            {"type": "wait_timer", "wait_timer": 0},
            {
                "type": "required_reviewers",
                "prevent_self_review": True,
                "reviewers": [{"type": "Team", "reviewer": {"slug": slug, "id": TEAMS[slug]}}],
            },
            {"type": "branch_policy"},
        ],
    }
    return environment | changes


def record_run(
    harness: Harness,
    *,
    prod_read_back: dict[str, Any] | None = None,
    load_access_read_back: dict[str, Any] | None = None,
    private_networking: str = "false",
) -> None:
    runner = harness.runner
    oidc_template(runner, {"use_default": True})
    runner.answer_json(vault_lookup(), [{"id": VAULT_ID, "name": VAULT_NAME}])
    runner.answer_json(
        graph_get(f"{GRAPH}/domains?$select=id,isInitial"),
        {"value": [{"id": "contoso.com", "isInitial": False}, {"id": DOMAIN, "isInitial": True}]},
    )
    for name, client_id in CLIENT_IDS.items():
        record_identity(runner, name, client_id, uid(900))
    for slug, team in TEAMS.items():
        runner.answer_json(("gh", "api", f"/orgs/{ORGANIZATION}/teams/{slug}"), {"id": team})
    for name in ENVIRONMENTS:
        path = f"{REPO}/environments/{name}"
        runner.answer(("gh", "api", "--method", "PUT", path, "--input", "-"), "{}")
        policies = [{"id": 5, "name": "release/*", "type": "branch"}] if name == "dev" else []
        runner.answer_json(
            ("gh", "api", f"{path}/deployment-branch-policies"),
            {"total_count": len(policies), "branch_policies": policies},
        )
        runner.answer(
            ("gh", "api", "--method", "POST", f"{path}/deployment-branch-policies", "--input", "-"),
            "{}",
        )
        runner.answer(("gh", "api", "--method", "DELETE", f"{path}/deployment-branch-policies/5"))
        read_back: dict[str, Any] = {"name": name, "protection_rules": []}
        if name == "prod":
            read_back = prod_read_back or protected_read_back("docchat-release-approvers")
        if name == "load-access":
            read_back = load_access_read_back or protected_read_back("docchat-tenant-admins")
        runner.answer_json(("gh", "api", path), read_back)
    for call in expected_variables(private_networking):
        runner.answer(call)


def test_t458_configure_github_stops_when_prod_reads_back_with_admin_bypass(
    harness: Harness,
) -> None:
    record_run(
        harness,
        prod_read_back=protected_read_back("docchat-release-approvers", can_admins_bypass=True),
    )

    assert run(harness) == 2

    refusal = harness.refusal()
    assert refusal["code"] == "github_protection_mismatch"
    assert refusal["message"] == configure_github.bootstrap.MESSAGES["github_protection_mismatch"]
    assert "prod" in refusal["detail"]
    assert "can_admins_bypass" in refusal["detail"]


def test_t458_configure_github_stops_when_load_access_shows_other_reviewers(
    harness: Harness,
) -> None:
    other = protected_read_back("docchat-tenant-admins")
    other["protection_rules"][1]["reviewers"].append(
        {"type": "User", "reviewer": {"login": "octocat"}}
    )
    record_run(harness, load_access_read_back=other)

    assert run(harness) == 2

    assert harness.refusal()["code"] == "github_protection_mismatch"
    assert "load-access reviewers" in harness.refusal()["detail"]


def test_configure_github_stops_without_the_self_review_block(harness: Harness) -> None:
    prod = protected_read_back("docchat-release-approvers")
    prod["protection_rules"][1]["prevent_self_review"] = False
    record_run(harness, prod_read_back=prod)

    assert run(harness) == 2

    assert "prevent_self_review" in harness.refusal()["detail"]


def test_configure_github_writes_environments_and_variables_and_reads_them_back(
    harness: Harness,
) -> None:
    record_run(harness)

    assert run(harness) == 0

    puts = {
        call.args[4].rsplit("/", 1)[1]: json.loads(call.stdin or "{}")
        for call in harness.runner.called("gh", "api", "--method", "PUT")
    }
    assert list(puts) == list(ENVIRONMENTS)
    branch_rule = {"protected_branches": False, "custom_branch_policies": True}
    assert puts["prod"] == {
        "deployment_branch_policy": branch_rule,
        "wait_timer": 0,
        "prevent_self_review": True,
        "reviewers": [{"type": "Team", "id": 31}],
        "can_admins_bypass": False,
    }
    assert puts["load-access"]["reviewers"] == [{"type": "Team", "id": 32}]
    assert puts["dev"] == {"deployment_branch_policy": branch_rule}
    posted = harness.runner.called("gh", "api", "--method", "POST")
    assert {json.loads(call.stdin or "{}")["name"] for call in posted} == {"main"}
    deleted = harness.runner.called("gh", "api", "--method", "DELETE")
    assert [call.args[4] for call in deleted] == [
        f"{REPO}/environments/dev/deployment-branch-policies/5"
    ]
    variables = [call.args for call in harness.runner.called("gh", "variable", "set")]
    assert variables == expected_variables()


def test_the_repository_variable_mirrors_ci_private_networking(harness: Harness) -> None:
    harness.write_config(config_data(ciPrivateNetworking=True))
    record_run(harness, private_networking="true")

    assert run(harness) == 0

    (mirrored,) = harness.runner.called("gh", "variable", "set", "DOCCHAT_CI_PRIVATE_NETWORKING")
    assert mirrored.args[5] == "true"
    assert "--env" not in mirrored.args


def test_test_environments_alone_hold_the_test_identity_variables(harness: Harness) -> None:
    record_run(harness)

    assert run(harness) == 0

    for call in harness.runner.called("gh", "variable", "set", "DOCCHAT_CI_TEST_CLIENT_ID"):
        assert call.args[-1] in {"dev-tests", "load-tests"}
    scopes = {
        call.args[-1] for call in harness.runner.called("gh", "variable", "set", "AZURE_CLIENT_ID")
    }
    assert scopes == {"dev", "prod", "load"}


def test_a_customised_subject_template_stops_before_any_write(harness: Harness) -> None:
    oidc_template(harness.runner, {"use_default": False, "include_claim_keys": ["repo"]})

    assert run(harness) == 2

    assert harness.refusal()["code"] == "oidc_subject_customised"
    assert harness.runner.called("gh", "api", "--method") == []


def test_the_organization_replaces_the_codeowners_placeholder(harness: Harness) -> None:
    codeowners = harness.repo / ".github" / "CODEOWNERS"
    codeowners.parent.mkdir()
    codeowners.write_text("*  @<org>/docchat-maintainers\n/infra/  @<org>/docchat-platform\n")
    record_run(harness)

    assert run(harness) == 0

    assert codeowners.read_text() == (
        "*  @contoso/docchat-maintainers\n/infra/  @contoso/docchat-platform\n"
    )
    assert "+++ b/.github/CODEOWNERS" in harness.out.getvalue()
