"""Writes DocChat's GitHub environments, their protection and variables (ADR 0120).

83-infrastructure.md section 10.4, run by the bootstrap's `github` step (section 11.2 step 13) or
alone as `uv run --frozen python scripts/bootstrap/configure_github.py` by an owner of the GitHub
organization: checks the repository's OIDC subject template (section 6.1); writes the six
environments with their deployment branches, reviewers and variables, and the repository
variables; reads every environment back and stops with `github_protection_mismatch` unless `prod`
and `load-access` show exactly their reviewers, `prevent_self_review: true` and
`can_admins_bypass: false`; and writes the organization's name into `.github/CODEOWNERS`
(01-repository.md section 2.6). Nothing is stored as a secret.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

import bootstrap
from bootstrap import (
    CITEST_IDENTITY,
    DEPLOY_IDENTITIES,
    ENVIRONMENTS,
    GRAPH,
    PR_IDENTITY,
    Context,
    Runtime,
    ScriptError,
    StepError,
    as_dict,
    as_str,
    identity_ids,
    listing,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

GITHUB_ENVIRONMENTS: Final = ("dev", "prod", "load", "load-access", "dev-tests", "load-tests")
# The environments whose approval is a second person's, never bypassed by an administrator.
PROTECTED: Final = {"prod": "docchat-release-approvers", "load-access": "docchat-tenant-admins"}
DEPLOYMENT_BRANCH: Final = "main"
CODEOWNERS: Final = ".github/CODEOWNERS"
ORG_PLACEHOLDER: Final = "@<org>/"


@dataclass(frozen=True)
class AzureValues:
    """The tenant-side values the variables carry; none of them is a secret."""

    deploy_client_ids: dict[str, str]
    pr_client_id: str
    citest_client_id: str
    test_vault_uri: str
    test_user_domain: str


def repository_path(ctx: Context) -> str:
    return f"/repos/{ctx.config.github_organization}/{ctx.config.github_repository}"


def gather_azure_values(ctx: Context) -> AzureValues:
    vault_name, _ = bootstrap.find_test_vault(ctx)
    domains = ctx.tools.graph_list(f"{GRAPH}/domains?$select=id,isInitial")
    initial = [as_str(domain.get("id"), "domain") for domain in domains if domain.get("isInitial")]
    if len(initial) != 1:
        raise StepError("the tenant's initial domain can't be read\n")
    return AzureValues(
        deploy_client_ids={
            env: identity_ids(ctx, DEPLOY_IDENTITIES[env])[0] for env in ENVIRONMENTS
        },
        pr_client_id=identity_ids(ctx, PR_IDENTITY)[0],
        citest_client_id=identity_ids(ctx, CITEST_IDENTITY)[0],
        test_vault_uri=f"https://{vault_name}.vault.azure.net/",
        test_user_domain=initial[0],
    )


def variable_plan(ctx: Context, values: AzureValues) -> list[tuple[str | None, str, str]]:
    """(environment, or None for the repository; name; value) of every variable the bootstrap
    sets (section 10.4). The operators' own variables are left as they are."""
    config = ctx.config
    plan: list[tuple[str | None, str, str]] = []
    for environment in ENVIRONMENTS:
        plan += [
            (environment, "AZURE_CLIENT_ID", values.deploy_client_ids[environment]),
            (environment, "AZURE_TENANT_ID", config.tenant_id),
            (environment, "AZURE_SUBSCRIPTION_ID", config.subscriptions[environment]),
            (environment, "AZURE_LOCATION", config.location),
            (environment, "AZURE_RESOURCE_GROUP", f"rg-docchat-{environment}"),
        ]
    for environment, tested in (("dev-tests", "dev"), ("load-tests", "load")):
        plan += [
            (environment, "AZURE_TENANT_ID", config.tenant_id),
            (environment, "AZURE_SUBSCRIPTION_ID", config.subscriptions[tested]),
        ]
        if environment == "load-tests":
            plan += [
                (environment, "AZURE_LOCATION", config.location),
                (environment, "AZURE_RESOURCE_GROUP", "rg-docchat-load"),
            ]
        plan += [
            (environment, "DOCCHAT_CI_TEST_CLIENT_ID", values.citest_client_id),
            (environment, "DOCCHAT_TEST_VAULT_URI", values.test_vault_uri),
            (environment, "DOCCHAT_TEST_USER_DOMAIN", values.test_user_domain),
        ]
    plan += [
        (None, "AZURE_TENANT_ID", config.tenant_id),
        (None, "DOCCHAT_PR_CLIENT_ID", values.pr_client_id),
        (None, "DOCCHAT_DEV_SUBSCRIPTION_ID", config.subscriptions["dev"]),
        (
            None,
            "DOCCHAT_CI_PRIVATE_NETWORKING",
            "true" if config.ci_private_networking else "false",
        ),
    ]
    return plan


def team_id(ctx: Context, slug: str) -> Any:
    team = as_dict(
        ctx.tools.gh_json("api", f"/orgs/{ctx.config.github_organization}/teams/{slug}"), slug
    )
    return team.get("id")


def write_environment(ctx: Context, name: str) -> None:
    body: dict[str, Any] = {
        "deployment_branch_policy": {"protected_branches": False, "custom_branch_policies": True}
    }
    if name in PROTECTED:
        body |= {
            "wait_timer": 0,
            "prevent_self_review": True,
            "reviewers": [{"type": "Team", "id": team_id(ctx, PROTECTED[name])}],
            "can_admins_bypass": False,
        }
    path = f"{repository_path(ctx)}/environments/{name}"
    ctx.tools.gh("api", "--method", "PUT", path, "--input", "-", stdin=json.dumps(body))
    write_branch_policy(ctx, path)


def write_branch_policy(ctx: Context, environment_path: str) -> None:
    """Deployments from `main` only: the one custom branch policy `main`, any other removed."""
    policies_path = f"{environment_path}/deployment-branch-policies"
    policies = listing(
        ctx.tools.gh_json("api", policies_path), "branch_policies", "deployment branch policies"
    )
    if not any(policy.get("name") == DEPLOYMENT_BRANCH for policy in policies):
        ctx.tools.gh(
            "api",
            "--method",
            "POST",
            policies_path,
            "--input",
            "-",
            stdin=json.dumps({"name": DEPLOYMENT_BRANCH, "type": "branch"}),
        )
    for policy in policies:
        if policy.get("name") != DEPLOYMENT_BRANCH:
            ctx.tools.gh("api", "--method", "DELETE", f"{policies_path}/{policy.get('id')}")


def set_variables(ctx: Context, plan: Sequence[tuple[str | None, str, str]]) -> None:
    repository = f"{ctx.config.github_organization}/{ctx.config.github_repository}"
    for environment, name, value in plan:
        scope = ["--env", environment] if environment else []
        ctx.tools.gh("variable", "set", name, "--body", value, "--repo", repository, *scope)


def protection_differences(name: str, environment: dict[str, Any]) -> list[str]:
    rules = [rule for rule in environment.get("protection_rules") or [] if isinstance(rule, dict)]
    reviewer_rules = [rule for rule in rules if rule.get("type") == "required_reviewers"]
    reviewers = sorted(
        (str(item.get("type")), str(as_dict(item.get("reviewer") or {}, name).get("slug")))
        for rule in reviewer_rules
        for item in rule.get("reviewers") or []
        if isinstance(item, dict)
    )
    differences = []
    if reviewers != [("Team", PROTECTED[name])]:
        differences.append(f"{name} reviewers are {reviewers}, expected team {PROTECTED[name]}")
    if [rule.get("prevent_self_review") for rule in reviewer_rules] != [True]:
        differences.append(f"{name} doesn't show prevent_self_review: true")
    if environment.get("can_admins_bypass") is not False:
        differences.append(f"{name} doesn't show can_admins_bypass: false")
    return differences


def read_back(ctx: Context) -> None:
    differences: list[str] = []
    for name in GITHUB_ENVIRONMENTS:
        environment = as_dict(
            ctx.tools.gh_json("api", f"{repository_path(ctx)}/environments/{name}"), name
        )
        if name in PROTECTED:
            differences += protection_differences(name, environment)
    if differences:
        raise ScriptError("github_protection_mismatch", "; ".join(differences))


def write_codeowners(ctx: Context) -> None:
    path = ctx.repo_root / CODEOWNERS
    if not path.is_file():
        return
    text = path.read_text(encoding="utf-8")
    owner = f"@{ctx.config.github_organization}/"
    bootstrap.write_with_diff(path, text.replace(ORG_PLACEHOLDER, owner), CODEOWNERS, ctx.out)


def configure(ctx: Context) -> None:
    bootstrap.oidc_subject_prefix(ctx)
    values = gather_azure_values(ctx)
    for name in GITHUB_ENVIRONMENTS:
        write_environment(ctx, name)
    set_variables(ctx, variable_plan(ctx, values))
    read_back(ctx)
    write_codeowners(ctx)


def main(argv: Sequence[str] | None = None, runtime: Runtime | None = None) -> int:
    # No options: bootstrap.config.json names the organization, repository and every value.
    argparse.ArgumentParser(description="DocChat GitHub environments (section 10.4).").parse_args(
        argv
    )
    return bootstrap.run_script(configure, runtime or Runtime())


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
