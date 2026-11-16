"""`jit grant`, `jit revoke` and `jit list` (`86-operations.md` section 2.8.5;
`13-conversations-and-memory.md` sections 10.1, 10.2 and 10.6; ADR 0024).

Script mode for tenants without PIM for Groups: a group owner, never the operator, changes the
membership of the level's group through the Azure CLI under group ownership. In the fallback mode
(`DOCCHAT_COSMOS_GROUP_DATA_ACCESS` is `false`) the levels `read` and `user_data` also hold a
per-principal Cosmos DB data-plane assignment, created and deleted through Resource Manager under
DocChat Cosmos Data Access Granter. Entra ID's audit log and the Activity Log record every change,
so no command writes an `app.audit_events` row; nothing removes a membership by itself.
"""

from __future__ import annotations

import argparse
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, Final

from docchat_core.models import format_utc, parse_uuid
from docchat_ops.framework.auth import az_json, az_text, signed_in_user
from docchat_ops.framework.endpoints import object_id, read_checkout_json
from docchat_ops.framework.exitcodes import (
    Conflict,
    NotFound,
    OpsError,
    UnexpectedError,
    UsageError,
)

if TYPE_CHECKING:
    from pydantic import JsonValue

    from docchat_ops.cli import Run
    from docchat_ops.framework.auth import AzRunner

MIN_HOURS: Final = 1
MAX_HOURS: Final = 4
GROUP_DATA_ACCESS: Final = "DOCCHAT_COSMOS_GROUP_DATA_ACCESS"
TEMPLATE_ASSIGNMENTS: Final = "DOCCHAT_COSMOS_ROLE_ASSIGNMENT_IDS"
DATA_READER: Final = "00000000-0000-0000-0000-000000000001"
DATA_CONTRIBUTOR: Final = "00000000-0000-0000-0000-000000000002"
GROUP_MODE: Final = "group"
FALLBACK_MODE: Final = "fallback"
RECEIPT_KEYS: Final = (
    "group",
    "member",
    "level",
    "grantedAt",
    "expiresAt",
    "ticket",
    "mode",
    "assignmentId",
)
"""The grant receipt's fields, which the ticket records."""


@dataclass(frozen=True)
class Level:
    name: str
    group_field: str
    """The field under `groups` of `infra/env/<env>.json`."""
    group_suffix: str
    role_definition_id: str | None
    """The Cosmos DB built-in role of the fallback assignment; `None` for SQL-only levels."""
    scope: str | None

    def group_name(self, environment: str) -> str:
        return f"sg-docchat-{environment}-{self.group_suffix}"


LEVELS: Final[dict[str, Level]] = {
    "read": Level(
        "read",
        "conversationReaders",
        "conversation-readers",
        DATA_READER,
        "/dbs/docchat/colls/conversations",
    ),
    "user_data": Level(
        "user_data", "userDataOperators", "user-data-operators", DATA_CONTRIBUTOR, "/dbs/docchat"
    ),
    "config": Level("config", "configOperators", "config-operators", None, None),
}


@dataclass(frozen=True)
class Target:
    """The environment's groups and Cosmos DB account."""

    environment: str
    group_ids: dict[str, str]
    fallback: bool
    account: str
    resource_group: str
    declared_assignments: frozenset[str]
    """The template-declared assignment IDs, lower-cased; none of them is a grant."""

    @property
    def mode(self) -> str:
        return FALLBACK_MODE if self.fallback else GROUP_MODE


def member(value: str) -> str:
    """An object ID argument, kept as lower-case canonical text."""
    try:
        return str(parse_uuid(value))
    except ValueError:
        raise argparse.ArgumentTypeError("an object ID is a hyphenated UUID") from None


def hours(value: str) -> int:
    if not value.isdigit() or not MIN_HOURS <= int(value) <= MAX_HOURS:
        raise argparse.ArgumentTypeError("--hours is a whole number from 1 to 4")
    return int(value)


def _level_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--level", choices=tuple(LEVELS), required=True, help="the access level")


def grant_arguments(parser: argparse.ArgumentParser) -> None:
    _level_argument(parser)
    parser.add_argument("--member", type=member, required=True, help="the operator's object ID")
    parser.add_argument("--hours", type=hours, required=True, help="1 to 4")


def revoke_arguments(parser: argparse.ArgumentParser) -> None:
    _level_argument(parser)
    parser.add_argument("--member", type=member, required=True, help="the operator's object ID")


def list_arguments(parser: argparse.ArgumentParser) -> None:
    """`jit list` takes no argument beyond the global options."""
    del parser


def target(run: Run) -> Target:
    """The groups from `infra/env/<env>.json` and the mode and account from the azd file."""
    azd = run.azd()
    environment = run.environment
    relative = f"infra/env/{environment}.json"
    document = read_checkout_json(run.root(), relative)
    groups = document.get("groups") if isinstance(document, dict) else None
    if not isinstance(groups, dict):
        raise UsageError(f"{relative} has no groups", "restore it from main")
    group_ids = {
        level.name: object_id(groups.get(level.group_field), f"groups.{level.group_field}")
        for level in LEVELS.values()
    }
    return Target(
        environment=environment,
        group_ids=group_ids,
        fallback=not azd.require_flag(GROUP_DATA_ACCESS),
        account=azd.require("DOCCHAT_COSMOS_ACCOUNT_NAME"),
        resource_group=azd.require("AZURE_RESOURCE_GROUP"),
        declared_assignments=frozenset(
            item.strip().lower()
            for item in azd.values.get(TEMPLATE_ASSIGNMENTS, "").split(",")
            if item.strip()
        ),
    )


async def is_member(az: AzRunner, group_id: str, member_id: str) -> bool:
    answer = await az_text(
        az,
        [
            "ad",
            "group",
            "member",
            "check",
            "--group",
            group_id,
            "--member-id",
            member_id,
            "--query",
            "value",
            "-o",
            "tsv",
        ],
        "checking the group membership",
    )
    return answer.lower() == "true"


async def group_members(az: AzRunner, group_id: str) -> list[str]:
    """The object IDs of a group's direct members; names stay out of the output."""
    members = await az_json(
        az,
        ["ad", "group", "member", "list", "--group", group_id, "--query", "[].id", "-o", "json"],
        "listing the group members",
    )
    if not isinstance(members, list):
        raise UnexpectedError("the member list isn't a list", "check the Azure CLI version")
    return sorted(str(item).lower() for item in members)


def _cosmos(target: Target, *args: str) -> list[str]:
    return [
        "cosmosdb",
        "sql",
        "role",
        "assignment",
        *args,
        "--account-name",
        target.account,
        "--resource-group",
        target.resource_group,
    ]


async def data_plane_assignments(az: AzRunner, target: Target) -> list[dict[str, str]]:
    """The account's Cosmos DB data-plane role assignments."""
    found = await az_json(az, [*_cosmos(target, "list"), "-o", "json"], "listing the assignments")
    if not isinstance(found, list):
        raise UnexpectedError("the assignment list isn't a list", "check the Azure CLI version")
    return [
        {
            key: str(item.get(key, ""))
            for key in ("id", "name", "principalId", "roleDefinitionId", "scope")
        }
        for item in found
        if isinstance(item, dict)
    ]


def _at_scope(assignment: dict[str, str], level: Level) -> bool:
    return level.scope is not None and assignment["scope"].lower().endswith(level.scope.lower())


def _relative_scope(assignment: dict[str, str]) -> str:
    scope = assignment["scope"]
    index = scope.lower().find("/dbs/")
    return scope[index:] if index >= 0 else scope


def _summary(target: Target, level: Level, member_id: str) -> list[str]:
    return [
        f"level: {level.name}",
        f"group: {level.group_name(target.environment)} ({target.group_ids[level.name]})",
        f"member: {member_id}",
        f"mode: {target.mode}",
    ]


async def grant(run: Run, args: argparse.Namespace) -> None:
    level = LEVELS[args.level]
    member_id: str = args.member
    granted_hours: int = args.hours
    where = target(run)
    az = run.toolkit.az
    await run.refuse_service_principal()
    if await signed_in_user(az) == member_id:
        raise UsageError(
            "a group owner can't grant a level to themselves",
            "ask another group owner (an approver) to run the grant",
        )
    group_id = where.group_ids[level.name]
    if await is_member(az, group_id, member_id):
        raise Conflict(
            f"{member_id} already belongs to {level.group_name(where.environment)}",
            "another grant is active; revoke it first or wait for it to end",
        )
    with_assignment = where.fallback and level.scope is not None
    summary = [*_summary(where, level, member_id), f"hours: {granted_hours}"]
    if with_assignment:
        summary.append(f"assignment: {level.role_definition_id} at {level.scope}")
    await run.confirm(summary)
    granted_at = run.toolkit.clock.now()
    await az_text(
        az,
        ["ad", "group", "member", "add", "--group", group_id, "--member-id", member_id],
        "adding the member",
    )
    assignment_id: str | None = None
    if with_assignment and level.role_definition_id is not None and level.scope is not None:
        assignment_id = str(uuid.uuid4())
        run.progress(f"added {member_id} to {level.group_name(where.environment)}")
        try:
            await az_text(
                az,
                [
                    *_cosmos(where, "create"),
                    "--role-assignment-id",
                    assignment_id,
                    "--role-definition-id",
                    level.role_definition_id,
                    "--scope",
                    level.scope,
                    "--principal-id",
                    member_id,
                ],
                "creating the data-plane assignment",
            )
        except OpsError as error:
            # The membership exists without its assignment, and a rerun would stop at the
            # existing membership, so the way back is named instead.
            raise type(error)(
                f"the member was added but the data-plane assignment failed: {error.cause}",
                f"run jit revoke --level {level.name} --member {member_id}, then grant again "
                f"({error.next_step})",
            ) from None
    run.report(
        "granted",
        {
            "group": level.group_name(where.environment),
            "member": member_id,
            "level": level.name,
            "grantedAt": format_utc(granted_at),
            "expiresAt": format_utc(granted_at + timedelta(hours=granted_hours)),
            "ticket": run.ticket,
            "mode": where.mode,
            "assignmentId": assignment_id,
        },
    )


async def revoke(run: Run, args: argparse.Namespace) -> None:
    level = LEVELS[args.level]
    member_id: str = args.member
    where = target(run)
    az = run.toolkit.az
    await run.refuse_service_principal()
    group_id = where.group_ids[level.name]
    in_group = await is_member(az, group_id, member_id)
    assignments: list[dict[str, str]] = []
    if where.fallback and level.scope is not None:
        assignments = [
            item
            for item in await data_plane_assignments(az, where)
            if item["principalId"].lower() == member_id and _at_scope(item, level)
        ]
    if not in_group and not assignments:
        raise NotFound(
            f"{member_id} is in neither {level.group_name(where.environment)} nor an assignment",
            "check the level and the member's object ID",
        )
    summary = _summary(where, level, member_id)
    summary.append(f"remove from group: {'yes' if in_group else 'no'}")
    summary.extend(f"delete assignment: {item['name']}" for item in assignments)
    await run.confirm(summary)
    if in_group:
        await az_text(
            az,
            ["ad", "group", "member", "remove", "--group", group_id, "--member-id", member_id],
            "removing the member",
        )
    for item in assignments:
        await az_text(
            az,
            [*_cosmos(where, "delete"), "--role-assignment-id", item["name"], "--yes"],
            "deleting the data-plane assignment",
        )
    deleted: list[JsonValue] = [item["name"] for item in assignments]
    run.report(
        "revoked",
        {
            "group": level.group_name(where.environment),
            "member": member_id,
            "level": level.name,
            "revokedAt": format_utc(run.toolkit.clock.now()),
            "ticket": run.ticket,
            "mode": where.mode,
            "removedFromGroup": in_group,
            "deletedAssignmentIds": deleted,
        },
    )


async def list_members(run: Run, args: argparse.Namespace) -> None:
    del args
    where = target(run)
    az = run.toolkit.az
    groups: list[JsonValue] = []
    for level in LEVELS.values():
        members: list[JsonValue] = list(await group_members(az, where.group_ids[level.name]))
        groups.append(
            {"level": level.name, "group": level.group_name(where.environment), "members": members}
        )
    fields: dict[str, JsonValue] = {"mode": where.mode, "groups": groups}
    if where.fallback:
        # The templates' own assignments (the API identity's among them) aren't grants; the
        # azd file lists them, so everything else is a per-principal assignment.
        per_principal: list[JsonValue] = [
            {
                "principalId": item["principalId"].lower(),
                "roleDefinitionId": item["roleDefinitionId"].rsplit("/", 1)[-1],
                "scope": _relative_scope(item),
                "assignmentId": item["name"],
            }
            for item in await data_plane_assignments(az, where)
            if item["id"].lower() not in where.declared_assignments
        ]
        fields["assignments"] = per_principal
    run.report("listed", fields, rows="groups", columns=("level", "group", "members"))
