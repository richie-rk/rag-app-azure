"""`jit grant`, `jit revoke` and `jit list` (T-541, T-542; `86-operations.md` section 2.8.5;
`13-conversations-and-memory.md` sections 10.1 to 10.6)."""

from __future__ import annotations

import argparse
from typing import TYPE_CHECKING

import pytest

from docchat_ops.commands import jit
from docchat_ops.framework.exitcodes import ExitCode

if TYPE_CHECKING:
    from ops.tests.unit.conftest import OpsHarness, Outcome

ARGS = ("--ticket", "CHG-12", "--reason", "rotate the summary limit", "--yes")
ACCOUNT_ID = (
    "/subscriptions/s/resourceGroups/rg-docchat-dev/providers/Microsoft.DocumentDB/"
    "databaseAccounts/cosmos-docchat-dev-abc123"
)
CONVERSATIONS_SCOPE = f"{ACCOUNT_ID}/dbs/docchat/colls/conversations"
DATABASE_SCOPE = f"{ACCOUNT_ID}/dbs/docchat"


def _assignment(name: str, principal: str, role: str, scope: str) -> dict[str, str]:
    return {
        "id": f"{ACCOUNT_ID}/sqlRoleAssignments/{name}",
        "name": name,
        "principalId": principal,
        "roleDefinitionId": f"{ACCOUNT_ID}/sqlRoleDefinitions/{role}",
        "scope": scope,
    }


def _grant(ops: OpsHarness, level: str, hours: str = "2", environment: str = "dev") -> Outcome:
    return ops.run(
        "--env",
        environment,
        "--json",
        "jit",
        "grant",
        "--level",
        level,
        "--member",
        ops.member_oid,
        "--hours",
        hours,
        *ARGS,
    )


def _revoke(ops: OpsHarness, level: str) -> Outcome:
    return ops.run(
        "--env",
        "dev",
        "--json",
        "jit",
        "revoke",
        "--level",
        level,
        "--member",
        ops.member_oid,
        *ARGS,
    )


def _not_a_member(ops: OpsHarness) -> None:
    ops.az.on("ad", "group", "member", "check", stdout="false\n")
    ops.az.on("ad", "group", "member", "add")
    ops.az.on("ad", "group", "member", "remove")
    ops.az.on("cosmosdb", "sql", "role", "assignment", "create", stdout="{}")
    ops.az.on("cosmosdb", "sql", "role", "assignment", "delete")


def _fallback(ops: OpsHarness, assignment_ids: str = "") -> None:
    ops.write_azd(
        "dev",
        DOCCHAT_COSMOS_GROUP_DATA_ACCESS="false",
        DOCCHAT_COSMOS_ROLE_ASSIGNMENT_IDS=assignment_ids,
    )


def test_levels_name_the_spec_groups_roles_and_scopes() -> None:
    assert {name: level.group_name("dev") for name, level in jit.LEVELS.items()} == {
        "read": "sg-docchat-dev-conversation-readers",
        "user_data": "sg-docchat-dev-user-data-operators",
        "config": "sg-docchat-dev-config-operators",
    }
    assert jit.LEVELS["read"].role_definition_id == "00000000-0000-0000-0000-000000000001"
    assert jit.LEVELS["read"].scope == "/dbs/docchat/colls/conversations"
    assert jit.LEVELS["user_data"].role_definition_id == "00000000-0000-0000-0000-000000000002"
    assert jit.LEVELS["user_data"].scope == "/dbs/docchat"
    assert jit.LEVELS["config"].role_definition_id is None


def test_grant_in_group_mode_adds_the_member_and_prints_a_receipt(ops: OpsHarness) -> None:
    _not_a_member(ops)
    outcome = _grant(ops, "read", hours="3")
    assert outcome.code == ExitCode.SUCCESS, outcome.stderr
    group_id = ops.group_ids["conversationReaders"]
    assert ops.az.called("ad", "group", "member", "add") == [
        ("ad", "group", "member", "add", "--group", group_id, "--member-id", ops.member_oid)
    ]
    assert ops.az.called("cosmosdb") == []
    receipt = outcome.json()
    assert {key: receipt[key] for key in jit.RECEIPT_KEYS} == {
        "group": "sg-docchat-dev-conversation-readers",
        "member": ops.member_oid,
        "level": "read",
        "grantedAt": "2026-10-30T12:00:00.000Z",
        "expiresAt": "2026-10-30T15:00:00.000Z",
        "ticket": "CHG-12",
        "mode": "group",
        "assignmentId": None,
    }
    assert "rotate the summary limit" not in outcome.stdout


@pytest.mark.parametrize(
    ("level", "role", "scope"),
    [
        ("read", "00000000-0000-0000-0000-000000000001", "/dbs/docchat/colls/conversations"),
        ("user_data", "00000000-0000-0000-0000-000000000002", "/dbs/docchat"),
    ],
)
def test_t541_grant_in_fallback_mode_creates_the_assignment(
    ops: OpsHarness, level: str, role: str, scope: str
) -> None:
    _fallback(ops)
    _not_a_member(ops)
    outcome = _grant(ops, level)
    assert outcome.code == ExitCode.SUCCESS, outcome.stderr
    (call,) = ops.az.called("cosmosdb", "sql", "role", "assignment", "create")
    options = dict(zip(call[5::2], call[6::2], strict=True))
    assignment_id = outcome.json()["assignmentId"]
    assert options == {
        "--account-name": "cosmos-docchat-dev-abc123",
        "--resource-group": "rg-docchat-dev",
        "--role-assignment-id": assignment_id,
        "--role-definition-id": role,
        "--scope": scope,
        "--principal-id": ops.member_oid,
    }
    assert jit.member(assignment_id) == assignment_id
    assert outcome.json()["mode"] == "fallback"


def test_grant_of_config_in_fallback_mode_is_membership_alone(ops: OpsHarness) -> None:
    _fallback(ops)
    _not_a_member(ops)
    outcome = _grant(ops, "config")
    assert outcome.code == ExitCode.SUCCESS
    assert ops.az.called("cosmosdb") == []
    assert outcome.json()["assignmentId"] is None


def test_t541_self_grant_exits_2(ops: OpsHarness) -> None:
    _not_a_member(ops)
    ops.az.on("ad", "signed-in-user", "show", stdout=f"{ops.member_oid.upper()}\n")
    outcome = _grant(ops, "config")
    assert outcome.code == ExitCode.USAGE
    assert "themselves" in outcome.failure
    assert ops.az.called("ad", "group", "member", "add") == []


@pytest.mark.parametrize("hours", ["0", "5", "-1", "two", "1.5"])
def test_t541_hours_outside_1_to_4_exit_2(ops: OpsHarness, hours: str) -> None:
    outcome = _grant(ops, "read", hours=hours)
    assert outcome.code == ExitCode.USAGE
    assert ops.az.calls == []


def test_t541_t542_existing_member_exits_6(ops: OpsHarness) -> None:
    _not_a_member(ops)
    ops.az.on("ad", "group", "member", "check", stdout="true\n")
    outcome = _grant(ops, "user_data")
    assert outcome.code == ExitCode.CONFLICT
    assert ops.az.called("ad", "group", "member", "add") == []


def test_failed_assignment_after_the_membership_names_the_way_back(ops: OpsHarness) -> None:
    _fallback(ops)
    _not_a_member(ops)
    ops.az.fail(
        "cosmosdb",
        "sql",
        "role",
        "assignment",
        "create",
        stderr="ERROR: (AuthorizationFailed) The client 'owner@contoso.com' has no rights",
    )
    outcome = _grant(ops, "read")
    assert outcome.code == ExitCode.ACCESS_DENIED
    assert f"run jit revoke --level read --member {ops.member_oid}" in outcome.failure
    assert "owner@contoso.com" not in outcome.stderr
    assert ops.az.called("ad", "group", "member", "add")


def test_a_refused_membership_change_exits_3(ops: OpsHarness) -> None:
    _not_a_member(ops)
    ops.az.fail("ad", "group", "member", "add", stderr="Authorization_RequestDenied")
    assert _grant(ops, "config").code == ExitCode.ACCESS_DENIED


@pytest.mark.parametrize("value", ["not-a-uuid", "{33333333-3333-4333-8333-333333333333}", ""])
def test_member_must_be_an_object_id(value: str) -> None:
    with pytest.raises(argparse.ArgumentTypeError):
        jit.member(value)


def test_member_is_lower_cased() -> None:
    assert jit.member("AAAAAAAA-0000-4000-8000-00000000000A") == (
        "aaaaaaaa-0000-4000-8000-00000000000a"
    )


def test_revoke_removes_the_member(ops: OpsHarness) -> None:
    _not_a_member(ops)
    ops.az.on("ad", "group", "member", "check", stdout="true\n")
    outcome = _revoke(ops, "config")
    assert outcome.code == ExitCode.SUCCESS, outcome.stderr
    assert ops.az.called("ad", "group", "member", "remove") == [
        (
            "ad",
            "group",
            "member",
            "remove",
            "--group",
            ops.group_ids["configOperators"],
            "--member-id",
            ops.member_oid,
        )
    ]
    receipt = outcome.json()
    assert receipt["removedFromGroup"] is True
    assert receipt["deletedAssignmentIds"] == []
    assert receipt["revokedAt"] == "2026-10-30T12:00:00.000Z"


def test_t541_revoke_in_fallback_mode_deletes_the_level_assignments(ops: OpsHarness) -> None:
    _fallback(ops)
    _not_a_member(ops)
    ops.az.on("ad", "group", "member", "check", stdout="true\n")
    other = "99999999-9999-4999-8999-999999999999"
    ops.az.on_json(
        "cosmosdb",
        "sql",
        "role",
        "assignment",
        "list",
        value=[
            _assignment("a1", ops.member_oid, jit.DATA_READER, CONVERSATIONS_SCOPE),
            _assignment("a2", ops.member_oid.upper(), jit.DATA_READER, CONVERSATIONS_SCOPE),
            _assignment("a3", ops.member_oid, jit.DATA_CONTRIBUTOR, DATABASE_SCOPE),
            _assignment("a4", other, jit.DATA_READER, CONVERSATIONS_SCOPE),
            "not an assignment",
        ],
    )
    outcome = _revoke(ops, "read")
    assert outcome.code == ExitCode.SUCCESS, outcome.stderr
    deleted = [
        call[call.index("--role-assignment-id") + 1]
        for call in ops.az.called("cosmosdb", "sql", "role", "assignment", "delete")
    ]
    assert deleted == ["a1", "a2"]
    assert all(
        "--yes" in call for call in ops.az.called("cosmosdb", "sql", "role", "assignment", "delete")
    )
    assert outcome.json()["deletedAssignmentIds"] == ["a1", "a2"]


def test_revoke_of_an_assignment_left_without_membership(ops: OpsHarness) -> None:
    _fallback(ops)
    _not_a_member(ops)
    ops.az.on_json(
        "cosmosdb",
        "sql",
        "role",
        "assignment",
        "list",
        value=[_assignment("a3", ops.member_oid, jit.DATA_CONTRIBUTOR, DATABASE_SCOPE)],
    )
    outcome = _revoke(ops, "user_data")
    assert outcome.code == ExitCode.SUCCESS
    assert ops.az.called("ad", "group", "member", "remove") == []
    assert outcome.json()["removedFromGroup"] is False


def test_t541_revoke_of_nobody_exits_4(ops: OpsHarness) -> None:
    _not_a_member(ops)
    outcome = _revoke(ops, "read")
    assert outcome.code == ExitCode.NOT_FOUND
    assert ops.az.called("ad", "group", "member", "remove") == []


def test_list_prints_the_three_groups(ops: OpsHarness) -> None:
    ops.az.on_json("ad", "group", "member", "list", value=[])
    ops.az.on_json(
        "ad",
        "group",
        "member",
        "list",
        "--group",
        ops.group_ids["configOperators"],
        value=[ops.member_oid.upper()],
    )
    outcome = ops.run("--env", "dev", "--json", "jit", "list", *ARGS[:4])
    assert outcome.code == ExitCode.SUCCESS, outcome.stderr
    document = outcome.json()
    assert document["mode"] == "group"
    assert document["groups"] == [
        {"level": "read", "group": "sg-docchat-dev-conversation-readers", "members": []},
        {"level": "user_data", "group": "sg-docchat-dev-user-data-operators", "members": []},
        {
            "level": "config",
            "group": "sg-docchat-dev-config-operators",
            "members": [ops.member_oid],
        },
    ]
    assert "assignments" not in document
    assert ops.az.called("account") == []


def test_list_in_fallback_mode_shows_per_principal_assignments(ops: OpsHarness) -> None:
    declared = _assignment(
        "api", "aaaaaaaa-0000-4000-8000-000000000000", jit.DATA_CONTRIBUTOR, DATABASE_SCOPE
    )
    _fallback(ops, assignment_ids=declared["id"].upper())
    ops.az.on_json("ad", "group", "member", "list", value=[])
    ops.az.on_json(
        "cosmosdb",
        "sql",
        "role",
        "assignment",
        "list",
        value=[declared, _assignment("g1", ops.member_oid, jit.DATA_READER, CONVERSATIONS_SCOPE)],
    )
    outcome = ops.run("--env", "dev", "--json", "jit", "list", *ARGS[:4])
    assert outcome.code == ExitCode.SUCCESS, outcome.stderr
    assert outcome.json()["assignments"] == [
        {
            "principalId": ops.member_oid,
            "roleDefinitionId": jit.DATA_READER,
            "scope": "/dbs/docchat/colls/conversations",
            "assignmentId": "g1",
        }
    ]


@pytest.mark.parametrize("answer", ["{}", '"x"'])
def test_list_refuses_answers_that_are_not_lists(ops: OpsHarness, answer: str) -> None:
    ops.az.on("ad", "group", "member", "list", stdout=answer)
    assert ops.run("--env", "dev", "jit", "list", *ARGS[:4]).code == ExitCode.UNEXPECTED


def test_assignment_list_must_be_a_list(ops: OpsHarness) -> None:
    _fallback(ops)
    ops.az.on_json("ad", "group", "member", "list", value=[])
    ops.az.on_json("cosmosdb", "sql", "role", "assignment", "list", value={"value": []})
    assert ops.run("--env", "dev", "jit", "list", *ARGS[:4]).code == ExitCode.UNEXPECTED


def test_unset_group_ids_exit_2(ops: OpsHarness) -> None:
    ops.write_environment_file(
        "dev", {**ops.group_ids, "configOperators": "00000000-0000-0000-0000-000000000000"}
    )
    outcome = _grant(ops, "config")
    assert outcome.code == ExitCode.USAGE
    assert "groups.configOperators isn't set yet" in outcome.failure


def test_malformed_group_ids_exit_2(ops: OpsHarness) -> None:
    ops.write_environment_file("dev", {**ops.group_ids, "userDataOperators": "x"})
    assert _grant(ops, "config").code == ExitCode.USAGE


def test_environment_file_without_groups_exits_2(ops: OpsHarness) -> None:
    (ops.root / "infra" / "env" / "dev.json").write_text("[]", encoding="utf-8")
    assert _grant(ops, "config").code == ExitCode.USAGE


@pytest.mark.parametrize(
    ("name", "value"),
    [("DOCCHAT_COSMOS_GROUP_DATA_ACCESS", "maybe"), ("DOCCHAT_COSMOS_ACCOUNT_NAME", "")],
)
def test_jit_values_of_the_azd_file_are_required(ops: OpsHarness, name: str, value: str) -> None:
    ops.write_azd("dev", **{name: value})
    outcome = _grant(ops, "config")
    assert outcome.code == ExitCode.USAGE
    assert "azd env refresh -e dev" in outcome.failure
