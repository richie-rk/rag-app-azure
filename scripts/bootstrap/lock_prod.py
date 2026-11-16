"""Locks `prod`'s stores against deletion (83-infrastructure.md section 12.3; ADR 0111).

Run by the bootstrap operator after `prod`'s first provision, and again after any runbook that
removed a lock: `uv run --frozen python scripts/bootstrap/lock_prod.py`. It adds a `CanNotDelete`
lock named `docchat-protect` to six resources, then exits 2 with `prod_not_protected` unless
`az lock list` shows exactly those six `docchat-protect` locks, no lock sits on the SQL server or
the Cosmos DB account, and the assignment `docchat-deny-store-deletion` exists at rg-docchat-prod.
Idempotent: an existing lock of the same name is left as it is.
"""

from __future__ import annotations

import argparse
import sys
from typing import TYPE_CHECKING, Any, Final

import bootstrap
from bootstrap import Context, Runtime, ScriptError, as_dict, as_list

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

PROD_GROUP: Final = "rg-docchat-prod"
LOCK_NAME: Final = "docchat-protect"
LOCK_LEVEL: Final = "CanNotDelete"
STORE_DELETION_ASSIGNMENT: Final = "docchat-deny-store-deletion"
_LOCKS_SEGMENT: Final = "/providers/microsoft.authorization/locks/"

# (label, resource type, name or name prefix, child path under it). The SQL database and the
# Cosmos DB database are locked rather than their parents: a lock reaches every child, and each
# prod provision deletes its runner rule under the server and just-in-time revocation deletes
# data-plane assignments under the account (section 12.3).
LOCKED: Final = (
    ("SQL database docchat", "Microsoft.Sql/servers", "sql-docchat-prod-", "/databases/docchat"),
    (
        "Cosmos DB database docchat",
        "Microsoft.DocumentDB/databaseAccounts",
        "cosmos-docchat-prod-",
        "/sqlDatabases/docchat",
    ),
    ("data storage account", "Microsoft.Storage/storageAccounts", "stdocchatdataprod", ""),
    ("Key Vault", "Microsoft.KeyVault/vaults", "kv-docchat-prod-", ""),
    ("AI Search", "Microsoft.Search/searchServices", "srch-docchat-prod-", ""),
    ("Log Analytics workspace", "Microsoft.OperationalInsights/workspaces", "log-docchat-prod", ""),
)


def find_parent(resources: Sequence[Mapping[str, Any]], resource_type: str, prefix: str) -> str:
    matches = [
        str(resource.get("id"))
        for resource in resources
        if str(resource.get("type", "")).lower() == resource_type.lower()
        and str(resource.get("name", "")).startswith(prefix)
    ]
    if len(matches) != 1:
        raise ScriptError(
            "prod_not_protected",
            f"expected one {resource_type} named {prefix}* in {PROD_GROUP}, found {len(matches)}",
        )
    return matches[0]


def lock_scope(lock: Mapping[str, Any]) -> str:
    """The resource a lock sits on: its ID without the `/providers/.../locks/<name>` suffix."""
    lock_id = str(lock.get("id", "")).lower()
    return lock_id.split(_LOCKS_SEGMENT, 1)[0]


def protect(ctx: Context) -> None:
    subscription = ctx.config.subscriptions["prod"]
    where = ("--resource-group", PROD_GROUP, "--subscription", subscription)
    resources = [
        as_dict(item, PROD_GROUP)
        for item in as_list(
            ctx.tools.az_json(
                "resource", "list", *where, "--query", "[].{id: id, name: name, type: type}"
            ),
            PROD_GROUP,
        )
    ]
    targets = {
        label: find_parent(resources, resource_type, prefix) + child
        for label, resource_type, prefix, child in LOCKED
    }
    # The SQL server and the Cosmos DB account, the parents of the first two locked resources.
    parents = [find_parent(resources, kind, prefix) for _, kind, prefix, _ in LOCKED[:2]]
    for target in targets.values():
        ctx.tools.az(
            "lock",
            "create",
            "--name",
            LOCK_NAME,
            "--lock-type",
            LOCK_LEVEL,
            "--resource",
            target,
            "--notes",
            "DocChat prod store (83-infrastructure.md section 12.3)",
            "--output",
            "none",
        )
    verify(ctx, where, targets, parents)


def verify(
    ctx: Context, where: Sequence[str], targets: Mapping[str, str], parents: Sequence[str]
) -> None:
    locks = [
        as_dict(item, "lock")
        for item in as_list(ctx.tools.az_json("lock", "list", *where), "locks")
    ]
    protected = {
        lock_scope(lock)
        for lock in locks
        if str(lock.get("name", "")).lower() == LOCK_NAME and lock.get("level") == LOCK_LEVEL
    }
    expected = {target.lower(): label for label, target in targets.items()}
    problems = [
        f"no {LOCK_NAME} lock on the {expected[scope]}"
        for scope in expected
        if scope not in protected
    ]
    problems += [
        f"an extra {LOCK_NAME} lock on {scope}" for scope in sorted(protected - set(expected))
    ]
    locked_parents = {lock_scope(lock) for lock in locks} & {parent.lower() for parent in parents}
    problems += [f"a lock sits on {parent}" for parent in sorted(locked_parents)]
    assignment = ctx.tools.run(
        [
            "az",
            "policy",
            "assignment",
            "show",
            "--name",
            STORE_DELETION_ASSIGNMENT,
            *where,
            "--output",
            "json",
        ]
    )
    if assignment.returncode != 0:
        problems.append(f"the assignment {STORE_DELETION_ASSIGNMENT} doesn't exist")
    if problems:
        raise ScriptError("prod_not_protected", "; ".join(problems))


def main(argv: Sequence[str] | None = None, runtime: Runtime | None = None) -> int:
    argparse.ArgumentParser(description="Lock prod's stores (section 12.3).").parse_args(argv)
    return bootstrap.run_script(protect, runtime or Runtime())


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
