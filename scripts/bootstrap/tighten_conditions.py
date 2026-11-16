"""Tightens the deploy identity's role-assignment conditions (83-infrastructure.md 6.11 and 11.6).

Run by the bootstrap operator after the first provision of `dev` and of `prod`, and again whenever
a workload identity or a template-declared Cosmos DB assignment is re-created (runbook
"Re-tighten role-assignment conditions"):

    uv run --frozen python scripts/bootstrap/tighten_conditions.py --env dev|prod

Until then the deploy identity may grant any role of <R> to any service principal or group, itself
included; afterwards each role can go only to the principals of the tightened table. `load` is
never tightened: its identities are new on every run.
"""

from __future__ import annotations

import argparse
import io
import json
import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

import bootstrap
from bootstrap import (
    DEPLOY_IDENTITIES,
    SHARED_GROUP,
    ChildScriptError,
    Context,
    Runtime,
    ScriptError,
    StepError,
    as_dict,
    as_list,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

RBAC_ADMINISTRATOR: Final = "f58310d9-a9f6-439a-9e8d-f62e7b41a168"
CONDITION_VERSION: Final = "2.0"
COSMOS_OUTPUT: Final = "DOCCHAT_COSMOS_ROLE_ASSIGNMENT_IDS"

ROLES: Final = {
    "Storage Blob Data Contributor": "ba92f5b4-2d11-453d-a403-e96b0029c9fe",
    "Storage Blob Data Reader": "2a2b9908-6ea1-4ae2-8e65-a410df84e7d1",
    "Storage Blob Data Owner": "b7e6dc6d-f1e8-4753-8033-0f276bb0955b",
    "Storage Queue Data Contributor": "974c5e8b-45b9-4653-ba55-5f855dd0fb88",
    "Storage Queue Data Reader": "19e7f393-937e-4f77-808e-94535e297925",
    "Storage Queue Data Message Processor": "8a0f0c08-91a1-4084-bc3d-661d67233fed",
    "Storage Queue Data Message Sender": "c6a89b2d-59bc-44d0-9896-0f6e12d7b80a",
    "Storage Table Data Contributor": "0a9a7e1f-b9d0-4cc4-a60d-0319b160aaa3",
    "Search Index Data Reader": "1407120a-92aa-4202-b7e9-c0e197c71c8f",
    "Search Index Data Contributor": "8ebe5a00-799e-43f5-93ac-243d3dce84a7",
    "Cognitive Services OpenAI User": "5e0bd9bd-7b93-4f28-af87-19fc36ad61bd",
    "Cognitive Services User": "a97b65f3-24c7-4388-baec-2e87135dc908",
    "Monitoring Metrics Publisher": "3913510d-42f4-4e42-8a64-420c390055eb",
    "Key Vault Secrets User": "4633458b-17de-408a-b874-0445c86b69e6",
    "Reader": "acdd72a7-3385-48ef-bd42-f606fba81ae7",
    "Log Analytics Reader": "73c42c96-874c-492b-b04d-ab87d138a893",
    "Defender for Storage Data Scanner": "1e7ca9b1-60d1-4db8-a914-f2ca1ff27c40",
    "DocChat Search Index Manager": "e622b7ad-d5a9-48fb-8d1c-ae3d52a7cb9d",
    "DocChat Blob Tag Reader": "a5f5ab92-08e9-42d6-b9f2-31ca2b30a345",
    "AcrPull": "7f951dda-4ed3-4680-a7ca-43fe172d538d",
}

# The tightened table of section 6.11, in its row order.
API_ROLES: Final = (
    "Storage Blob Data Contributor",
    "Storage Blob Data Reader",
    "Storage Queue Data Message Sender",
    "Search Index Data Reader",
    "Cognitive Services OpenAI User",
    "Cognitive Services User",
    "Monitoring Metrics Publisher",
)
INGESTION_ROLES: Final = (
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
)
# Only under content-share fallback 1 (section 2.7): the ingestion identity reads the share secret.
FALLBACK_INGESTION_ROLE: Final = "Key Vault Secrets User"
OPERATORS_ROLES: Final = ("Reader", "Log Analytics Reader")
CONFIG_OPERATORS_ROLES: Final = ("Storage Queue Data Message Processor",)
USER_DATA_OPERATORS_ROLES: Final = ("Storage Blob Data Reader",)
SCANNER_ROLES: Final = ("Defender for Storage Data Scanner",)
REGISTRY_ROLES: Final = ("AcrPull",)

SQL_AUDIT_SETTING: Final = "sql-audit-security"
SQL_AUDIT_CATEGORIES: Final = ("SQLSecurityAuditEvents", "DevOpsOperationsAudit")


@dataclass(frozen=True)
class Principals:
    """Everything the tightened conditions and the alert parameters need, read before any change."""

    api: str
    ingestion: str
    operators: str
    config_operators: str
    user_data_operators: str
    scanner: str | None
    content_share_fallback: bool
    cosmos_assignment_ids: tuple[str, ...]
    sql_server_id: str


Pair = tuple[tuple[str, ...], str]


def _clause(action: str, attribute: str, pairs: Sequence[Pair]) -> list[str]:
    lines = [
        " (",
        f"  !(ActionMatches{{'Microsoft.Authorization/roleAssignments/{action}'}})",
        " )",
        " OR",
        " (",
    ]
    for index, (roles, principal) in enumerate(pairs):
        if index:
            lines.append("  OR")
        lines += [
            "  (",
            (
                f"   {attribute}[Microsoft.Authorization/roleAssignments:RoleDefinitionId] "
                f"ForAnyOfAnyValues:GuidEquals {{{', '.join(ROLES[role] for role in roles)}}}"
            ),
            "   AND",
            (
                f"   {attribute}[Microsoft.Authorization/roleAssignments:PrincipalId] "
                f"ForAnyOfAnyValues:GuidEquals {{{principal}}}"
            ),
            "  )",
        ]
    lines.append(" )")
    return lines


def render_condition(pairs: Sequence[Pair]) -> str:
    """Both clauses as a disjunction of (roles AND principal) pairs: the write clause tests the
    request's attributes, the delete clause the existing assignment's (section 6.11 step 4)."""
    return "\n".join(
        [
            "(",
            *_clause("write", "@Request", pairs),
            ")",
            "AND",
            "(",
            *_clause("delete", "@Resource", pairs),
            ")",
        ]
    )


def resource_group_pairs(principals: Principals) -> list[Pair]:
    ingestion_roles = INGESTION_ROLES + (
        (FALLBACK_INGESTION_ROLE,) if principals.content_share_fallback else ()
    )
    pairs: list[Pair] = [
        (API_ROLES, principals.api),
        (ingestion_roles, principals.ingestion),
        (OPERATORS_ROLES, principals.operators),
        (CONFIG_OPERATORS_ROLES, principals.config_operators),
        (USER_DATA_OPERATORS_ROLES, principals.user_data_operators),
    ]
    if principals.scanner:
        pairs.append((SCANNER_ROLES, principals.scanner))
    return pairs


def registry_pairs(principals: Principals) -> list[Pair]:
    return [(REGISTRY_ROLES, principals.api), (REGISTRY_ROLES, principals.ingestion)]


def _tsv_or_empty(ctx: Context, *args: str) -> str:
    """A read whose failure means the value is missing rather than a tool error."""
    result = ctx.tools.run(["az", *args, "--output", "tsv"])
    return result.stdout.strip() if result.returncode == 0 else ""


def _workload_principal(ctx: Context, environment: str, name: str) -> str:
    subscription = ctx.config.subscriptions[environment]
    identity = (
        f"/subscriptions/{subscription}/resourceGroups/rg-docchat-{environment}"
        f"/providers/Microsoft.ManagedIdentity/userAssignedIdentities/{name}"
    )
    return _tsv_or_empty(ctx, "identity", "show", "--ids", identity, "--query", "principalId")


def _environment_groups(ctx: Context, environment: str) -> dict[str, str]:
    label = f"infra/env/{environment}.json"
    try:
        data = json.loads((ctx.repo_root / label).read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise StepError(f"{label} can't be read ({type(error).__name__})\n") from error
    groups = data.get("groups") if isinstance(data, dict) else None
    groups = groups if isinstance(groups, dict) else {}
    return {
        key: str(groups.get(key, ""))
        for key in ("operators", "configOperators", "userDataOperators")
    }


def _cosmos_assignment_ids(ctx: Context, environment: str) -> tuple[str, ...]:
    """DOCCHAT_COSMOS_ROLE_ASSIGNMENT_IDS of the environment's newest successful provision."""
    deployments = ctx.tools.run(
        [
            "az",
            "deployment",
            "group",
            "list",
            "--resource-group",
            f"rg-docchat-{environment}",
            "--subscription",
            ctx.config.subscriptions[environment],
            "--query",
            (
                "[?properties.provisioningState=='Succeeded']"
                ".{timestamp: properties.timestamp, outputs: properties.outputs}"
            ),
            "--output",
            "json",
        ]
    )
    if deployments.returncode != 0:
        return ()
    candidates: list[tuple[str, str]] = []
    for item in as_list(
        bootstrap.parse_json(deployments.stdout, "deployments") or [], "deployments"
    ):
        deployment = as_dict(item, "deployment")
        outputs = {key.upper(): value for key, value in (deployment.get("outputs") or {}).items()}
        output = outputs.get(COSMOS_OUTPUT)
        if isinstance(output, dict) and isinstance(output.get("value"), str):
            candidates.append((str(deployment.get("timestamp", "")), output["value"]))
    if not candidates:
        return ()
    _, value = max(candidates)
    return tuple(item.strip() for item in value.split(",") if item.strip())


def _content_share_fallback(ctx: Context, environment: str) -> bool:
    """Fallback 1 is the one case where the Functions host account allows shared keys (2.7)."""
    flags = ctx.tools.az_json(
        "storage",
        "account",
        "list",
        "--resource-group",
        f"rg-docchat-{environment}",
        "--subscription",
        ctx.config.subscriptions[environment],
        "--query",
        f"[?starts_with(name, 'stdocchathost{environment}')].allowSharedKeyAccess",
    )
    return any(flag is True for flag in as_list(flags or [], "storage accounts"))


def read_principals(ctx: Context, environment: str) -> Principals:
    subscription = ctx.config.subscriptions[environment]
    groups = _environment_groups(ctx, environment)
    scanner = _tsv_or_empty(
        ctx,
        "resource",
        "show",
        "--ids",
        f"/subscriptions/{subscription}/providers/Microsoft.Security/datascanners/StorageDataScanner",
        "--query",
        "identity.principalId",
    )
    sql_servers = _tsv_or_empty(
        ctx,
        "resource",
        "list",
        "--resource-group",
        f"rg-docchat-{environment}",
        "--subscription",
        subscription,
        "--resource-type",
        "Microsoft.Sql/servers",
        "--query",
        f"[?starts_with(name, 'sql-docchat-{environment}-')].id",
    ).split()
    principals = Principals(
        api=_workload_principal(ctx, environment, f"id-docchat-api-{environment}"),
        ingestion=_workload_principal(ctx, environment, f"id-docchat-ingest-{environment}"),
        operators=groups["operators"],
        config_operators=groups["configOperators"],
        user_data_operators=groups["userDataOperators"],
        scanner=scanner or None,
        content_share_fallback=_content_share_fallback(ctx, environment),
        cosmos_assignment_ids=_cosmos_assignment_ids(ctx, environment),
        sql_server_id=sql_servers[0] if len(sql_servers) == 1 else "",
    )
    missing = [
        name
        for name, value in (
            (f"id-docchat-api-{environment}", principals.api),
            (f"id-docchat-ingest-{environment}", principals.ingestion),
            ("groups.operators", principals.operators),
            ("groups.configOperators", principals.config_operators),
            ("groups.userDataOperators", principals.user_data_operators),
            (COSMOS_OUTPUT, ",".join(principals.cosmos_assignment_ids)),
            (f"sql-docchat-{environment}-*", principals.sql_server_id),
        )
        if not value
    ]
    # prod's first provision turns scanning on, so its scanner must exist by now; dev's appears
    # only once dev scanning is turned on, and the script runs again before that (R23).
    if principals.scanner is None and environment == "prod":
        missing.append("StorageDataScanner")
    if missing:
        raise ScriptError("principal_ids_missing", f"{environment}: {', '.join(missing)}")
    return principals


def find_assignment(ctx: Context, principal: str, scope: str, subscription: str) -> dict[str, Any]:
    """The conditioned Role Based Access Control Administrator assignment at exactly `scope`:
    its constant name is guid(scope, principal, role), so scope, principal and role find it."""
    rows = as_list(
        ctx.tools.az_json(
            "role",
            "assignment",
            "list",
            "--assignee",
            principal,
            "--role",
            RBAC_ADMINISTRATOR,
            "--scope",
            scope,
            "--subscription",
            subscription,
        ),
        "role assignments",
    )
    matches = [
        as_dict(row, "role assignment")
        for row in rows
        if str(as_dict(row, "role assignment").get("scope", "")).lower() == scope.lower()
    ]
    if len(matches) != 1:
        raise ScriptError(
            "condition_update_failed",
            f"expected one Role Based Access Control Administrator assignment at {scope}, "
            f"found {len(matches)}",
        )
    return matches[0]


def update_condition(
    ctx: Context, principal: str, scope: str, subscription: str, condition: str
) -> None:
    assignment = find_assignment(ctx, principal, scope, subscription)
    updated = {**assignment, "condition": condition, "conditionVersion": CONDITION_VERSION}
    ctx.tools.az(
        "role", "assignment", "update", "--role-assignment", json.dumps(updated), "--output", "none"
    )
    stored = find_assignment(ctx, principal, scope, subscription).get("condition")
    if stored != condition:
        raise ScriptError(
            "condition_update_failed",
            f"the condition stored at {scope} differs from the rendered one",
        )


def registry_id(ctx: Context) -> str:
    ids = ctx.tools.az_tsv(
        "resource",
        "list",
        "--resource-group",
        SHARED_GROUP,
        "--subscription",
        ctx.config.home_subscription,
        "--resource-type",
        "Microsoft.ContainerRegistry/registries",
        "--query",
        "[?starts_with(name, 'crdocchat')].id",
    ).split()
    if len(ids) != 1:
        raise ScriptError(
            "condition_update_failed", f"expected one registry in {SHARED_GROUP}, found {len(ids)}"
        )
    return ids[0]


def rerun_security_step(ctx: Context) -> None:
    """The alerts of section 12.6 read the updated configuration only when redeployed."""
    script = ctx.repo_root / "scripts" / "bootstrap" / "bootstrap.py"
    result = ctx.tools.run([sys.executable, str(script), "--step", "security"])
    ctx.out.write(result.stdout)
    ctx.err.write(result.stderr)
    if result.returncode != 0:
        raise ChildScriptError(result.returncode)


def create_sql_audit_setting(ctx: Context, principals: Principals) -> None:
    ctx.tools.az(
        "monitor",
        "diagnostic-settings",
        "create",
        "--name",
        SQL_AUDIT_SETTING,
        "--resource",
        f"{principals.sql_server_id}/databases/master",
        "--workspace",
        bootstrap.security_workspace_id(ctx.config),
        "--logs",
        json.dumps([{"category": category, "enabled": True} for category in SQL_AUDIT_CATEGORIES]),
        "--export-to-resource-specific",
        "true",
        "--output",
        "none",
    )


def tighten(ctx: Context, environment: str) -> None:
    config = ctx.config
    subscription = config.subscriptions[environment]
    principals = read_principals(ctx, environment)
    deploy_principal = bootstrap.identity_ids(ctx, DEPLOY_IDENTITIES[environment])[1]
    registry = registry_id(ctx)
    update_condition(
        ctx,
        deploy_principal,
        f"/subscriptions/{subscription}/resourceGroups/rg-docchat-{environment}",
        subscription,
        render_condition(resource_group_pairs(principals)),
    )
    update_condition(
        ctx,
        deploy_principal,
        registry,
        config.home_subscription,
        render_condition(registry_pairs(principals)),
    )
    allowed = [
        principals.api,
        principals.ingestion,
        principals.config_operators,
        principals.user_data_operators,
        *([principals.scanner] if principals.scanner else []),
    ]
    raw = {
        **config.raw,
        "allowedRoleAssigneePrincipalIds": bootstrap.merge_ids(
            config.allowed_role_assignee_principal_ids, allowed
        ),
        "templateCosmosAssignmentIds": bootstrap.merge_ids(
            config.template_cosmos_assignment_ids, principals.cosmos_assignment_ids
        ),
    }
    diff = io.StringIO()
    bootstrap.write_with_diff(
        config.path, bootstrap.json_text(raw), str(bootstrap.CONFIG_PATH), diff
    )
    ctx.config = bootstrap.load_config(config.path)
    rerun_security_step(ctx)
    create_sql_audit_setting(ctx, principals)
    if principals.scanner is None:
        ctx.out.write(
            f"No Defender StorageDataScanner exists yet, so its pair was left out: run "
            f"tighten_conditions.py --env {environment} once more before {environment} scanning "
            "is turned on.\n"
        )
    ctx.out.write(diff.getvalue() or "bootstrap.config.json is unchanged.\n")


def main(argv: Sequence[str] | None = None, runtime: Runtime | None = None) -> int:
    parser = argparse.ArgumentParser(description="Tighten the role-assignment conditions (6.11).")
    parser.add_argument("--env", required=True, choices=["dev", "prod"])
    arguments = parser.parse_args(argv)
    return bootstrap.run_script(lambda ctx: tighten(ctx, arguments.env), runtime or Runtime())


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
