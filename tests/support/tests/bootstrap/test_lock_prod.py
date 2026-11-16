"""Unit tests of scripts/bootstrap/lock_prod.py (83-infrastructure.md section 12.3; T-470)."""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

from bootstrap_harness import SUBSCRIPTION

if TYPE_CHECKING:
    from bootstrap_harness import Harness

lock_prod = importlib.import_module("lock_prod")

GROUP = f"/subscriptions/{SUBSCRIPTION}/resourceGroups/rg-docchat-prod/providers"
SQL_SERVER = f"{GROUP}/Microsoft.Sql/servers/sql-docchat-prod-a1b2c3"
COSMOS = f"{GROUP}/Microsoft.DocumentDB/databaseAccounts/cosmos-docchat-prod-a1b2c3"
RESOURCES = [
    {"id": SQL_SERVER, "name": "sql-docchat-prod-a1b2c3", "type": "Microsoft.Sql/servers"},
    {
        "id": f"{SQL_SERVER}/databases/docchat",
        "name": "sql-docchat-prod-a1b2c3/docchat",
        "type": "Microsoft.Sql/servers/databases",
    },
    {
        "id": COSMOS,
        "name": "cosmos-docchat-prod-a1b2c3",
        "type": "Microsoft.DocumentDB/databaseAccounts",
    },
    {
        "id": f"{GROUP}/Microsoft.Storage/storageAccounts/stdocchatdataproda1b2c3",
        "name": "stdocchatdataproda1b2c3",
        "type": "Microsoft.Storage/storageAccounts",
    },
    {
        "id": f"{GROUP}/Microsoft.Storage/storageAccounts/stdocchathostproda1b2c3",
        "name": "stdocchathostproda1b2c3",
        "type": "Microsoft.Storage/storageAccounts",
    },
    {
        "id": f"{GROUP}/Microsoft.KeyVault/vaults/kv-docchat-prod-a1b2c3",
        "name": "kv-docchat-prod-a1b2c3",
        "type": "Microsoft.KeyVault/vaults",
    },
    {
        "id": f"{GROUP}/Microsoft.Search/searchServices/srch-docchat-prod-a1b2c3",
        "name": "srch-docchat-prod-a1b2c3",
        "type": "Microsoft.Search/searchServices",
    },
    {
        "id": f"{GROUP}/Microsoft.OperationalInsights/workspaces/log-docchat-prod",
        "name": "log-docchat-prod",
        "type": "Microsoft.OperationalInsights/workspaces",
    },
]
TARGETS = [
    f"{SQL_SERVER}/databases/docchat",
    f"{COSMOS}/sqlDatabases/docchat",
    f"{GROUP}/Microsoft.Storage/storageAccounts/stdocchatdataproda1b2c3",
    f"{GROUP}/Microsoft.KeyVault/vaults/kv-docchat-prod-a1b2c3",
    f"{GROUP}/Microsoft.Search/searchServices/srch-docchat-prod-a1b2c3",
    f"{GROUP}/Microsoft.OperationalInsights/workspaces/log-docchat-prod",
]
WHERE = ("--resource-group", "rg-docchat-prod", "--subscription", SUBSCRIPTION)


def lock(scope: str, name: str = "docchat-protect") -> dict[str, Any]:
    return {
        "id": f"{scope}/providers/Microsoft.Authorization/locks/{name}",
        "name": name,
        "level": "CanNotDelete",
    }


def record(
    harness: Harness, *, locks: list[dict[str, Any]], assignment_exists: bool = True
) -> None:
    runner = harness.runner
    runner.answer_json(
        (
            "az",
            "resource",
            "list",
            *WHERE,
            "--query",
            "[].{id: id, name: name, type: type}",
            "--output",
            "json",
        ),
        RESOURCES,
    )
    for target in TARGETS:
        runner.answer(
            (
                "az",
                "lock",
                "create",
                "--name",
                "docchat-protect",
                "--lock-type",
                "CanNotDelete",
                "--resource",
                target,
                "--notes",
                "DocChat prod store (83-infrastructure.md section 12.3)",
                "--output",
                "none",
            )
        )
    runner.answer_json(("az", "lock", "list", *WHERE, "--output", "json"), locks)
    show = (
        "az",
        "policy",
        "assignment",
        "show",
        "--name",
        "docchat-deny-store-deletion",
        *WHERE,
        "--output",
        "json",
    )
    if assignment_exists:
        runner.answer_json(show, {"name": "docchat-deny-store-deletion"})
    else:
        runner.answer(show, returncode=3, stderr="PolicyAssignmentNotFound\n")


def run(harness: Harness) -> int:
    code: int = lock_prod.main([], harness.runtime())
    return code


def test_t470_lock_prod_exits_0_when_the_six_locks_and_the_assignment_exist(
    harness: Harness,
) -> None:
    record(harness, locks=[lock(target) for target in TARGETS])

    assert run(harness) == 0

    created = [call.args[8] for call in harness.runner.called("az", "lock", "create")]
    assert created == TARGETS
    assert harness.err.getvalue() == ""


def test_t470_lock_prod_exits_2_when_a_lock_is_missing(harness: Harness) -> None:
    record(harness, locks=[lock(target) for target in TARGETS[1:]])

    assert run(harness) == 2

    refusal = harness.refusal()
    assert refusal["code"] == "prod_not_protected"
    assert "SQL database docchat" in refusal["detail"]


def test_t470_lock_prod_exits_2_when_the_delete_deny_assignment_is_missing(
    harness: Harness,
) -> None:
    record(harness, locks=[lock(target) for target in TARGETS], assignment_exists=False)

    assert run(harness) == 2

    refusal = harness.refusal()
    assert refusal["code"] == "prod_not_protected"
    assert "docchat-deny-store-deletion" in refusal["detail"]


def test_t469_lock_prod_exits_2_when_a_lock_sits_on_the_sql_server(harness: Harness) -> None:
    record(harness, locks=[*(lock(target) for target in TARGETS), lock(SQL_SERVER, "manual")])

    assert run(harness) == 2

    assert "a lock sits on" in harness.refusal()["detail"]


def test_t469_lock_prod_exits_2_on_an_extra_protect_lock(harness: Harness) -> None:
    extra = f"{GROUP}/Microsoft.Storage/storageAccounts/stdocchathostproda1b2c3"
    record(harness, locks=[*(lock(target) for target in TARGETS), lock(extra)])

    assert run(harness) == 2

    assert "extra docchat-protect lock" in harness.refusal()["detail"]


def test_lock_prod_exits_2_when_prod_has_no_search_service(harness: Harness) -> None:
    harness.runner.answer_json(
        (
            "az",
            "resource",
            "list",
            *WHERE,
            "--query",
            "[].{id: id, name: name, type: type}",
            "--output",
            "json",
        ),
        [item for item in RESOURCES if item["type"] != "Microsoft.Search/searchServices"],
    )

    assert run(harness) == 2

    assert harness.refusal()["code"] == "prod_not_protected"
    assert harness.runner.called("az", "lock") == []


def test_a_lock_scope_ignores_the_case_of_the_locks_segment() -> None:
    upper = {"id": f"{TARGETS[0]}/providers/MICROSOFT.AUTHORIZATION/locks/docchat-protect"}

    assert lock_prod.lock_scope(upper) == TARGETS[0].lower()
