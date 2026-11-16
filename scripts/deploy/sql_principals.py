"""The grants step: the five contained users and their role memberships (`80-data-model.md`
section 2.22; `83-infrastructure.md` section 6.12; ADR 0125).

Run by the azd `postprovision` hook right after the migrations, as the deploy identity (a member of
the server's Entra administrator group):

    uv run --frozen python scripts/deploy/sql_principals.py --env <env> [--sid-source client-id]

It is the only code that creates or drops a database principal, and it is idempotent: it creates a
missing user, drops and re-creates a user whose SID no longer matches its identity, adds each user
to its role and removes every other member of the five roles. DDL can't bind parameters, so every
ID is validated as a UUID before any SQL runs, and the statements come from
`docchat_core.sql.statements`. The session carries `APP=docchat-grants`; its token comes from the
Azure CLI's sign-in (`azure/login`, or the operator's `az login`).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

from azure.identity import AzureCliCredential
from sqlalchemy import Connection
from sqlalchemy.sql.elements import TextClause

from docchat_core.models import parse_uuid
from docchat_core.sql import statements
from docchat_core.sql.engine import TOKEN_SCOPE, create_sync_engine
from docchat_core.sql.tables import DATABASE_ROLES

type Environment = Literal["dev", "prod", "load"]
type SidSource = Literal["client-id", "object-id"]

ENVIRONMENTS: Final[tuple[Environment, ...]] = ("dev", "prod", "load")
REPOSITORY: Final = Path(__file__).resolve().parents[2]
EXIT_FAILURE: Final = 2

FAILURE_CODE: Final = "sql_principal_failed"
FAILURE_MESSAGE: Final = "A migration or a principal grant failed; nothing was deployed."

IDENTITY_OUTPUTS: Final[Mapping[SidSource, Mapping[str, str]]] = {
    "client-id": {
        "docchat_api": "DOCCHAT_API_IDENTITY_CLIENT_ID",
        "docchat_ingestion": "DOCCHAT_INGESTION_IDENTITY_CLIENT_ID",
    },
    "object-id": {
        "docchat_api": "DOCCHAT_API_IDENTITY_PRINCIPAL_ID",
        "docchat_ingestion": "DOCCHAT_INGESTION_IDENTITY_PRINCIPAL_ID",
    },
}
"""The azd outputs that name each workload identity, per SID source."""

GROUP_FIELDS: Final[Mapping[str, str]] = {
    "docchat_config_operator": "configOperators",
    "docchat_conversation_reader": "conversationReaders",
    "docchat_user_data_operator": "userDataOperators",
}
"""The `groups` fields of `infra/env/<env>.json` that name each just-in-time group."""


class StepError(Exception):
    """A failure the step reports with its code; `detail` names the step, never a secret."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


@dataclass(frozen=True)
class ContainedUser:
    """One expected database principal: its name, type (`E` identity, `X` group), SID literal
    (32 hexadecimal characters) and role."""

    name: str
    user_type: Literal["E", "X"]
    sid_hex: str
    role: str


@dataclass(frozen=True)
class Principal:
    """A row of `sys.database_principals`."""

    name: str
    principal_type: str
    sid: bytes | None


def user_names(environment: Environment) -> dict[str, tuple[str, Literal["E", "X"]]]:
    """Each role's contained user and its type (`80-data-model.md` section 2.22)."""
    return {
        "docchat_api": (f"id-docchat-api-{environment}", "E"),
        "docchat_ingestion": (f"id-docchat-ingest-{environment}", "E"),
        "docchat_config_operator": (f"sg-docchat-{environment}-config-operators", "X"),
        "docchat_conversation_reader": (f"sg-docchat-{environment}-conversation-readers", "X"),
        "docchat_user_data_operator": (f"sg-docchat-{environment}-user-data-operators", "X"),
    }


def sid_literal(identifier: str) -> str:
    """The SID of an Entra ID as SQL Server's `CONVERT(VARBINARY(16), <uniqueidentifier>)` gives
    it: the GUID's bytes in little-endian field order, as 32 hexadecimal characters."""
    try:
        return parse_uuid(identifier.strip()).bytes_le.hex()
    except ValueError:
        raise StepError("an identity or group ID isn't a UUID") from None


def read_azd_environment(
    environment: Environment,
    root: Path = REPOSITORY,
    environ: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """The azd environment's values: `.azure/<env>/.env`, overridden by the process environment,
    where azd places them for a hook."""
    values: dict[str, str] = {}
    path = root / ".azure" / environment / ".env"
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            key, separator, raw = line.strip().partition("=")
            if not separator or key.startswith("#"):
                continue
            values[key.strip()] = raw.strip().removeprefix('"').removesuffix('"')
    values.update(os.environ if environ is None else environ)
    return values


def read_groups(environment: Environment, root: Path = REPOSITORY) -> dict[str, str]:
    """The `groups` object of `infra/env/<env>.json`, which the bootstrap's `config` step writes."""
    path = root / "infra" / "env" / f"{environment}.json"
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raise StepError(f"infra/env/{environment}.json can't be read") from None
    groups = document.get("groups") if isinstance(document, dict) else None
    if not isinstance(groups, dict):
        raise StepError(f"infra/env/{environment}.json has no groups")
    return {str(key): str(value) for key, value in groups.items()}


def expected_users(
    environment: Environment,
    *,
    outputs: Mapping[str, str],
    groups: Mapping[str, str],
    sid_source: SidSource = "client-id",
) -> tuple[ContainedUser, ...]:
    """The five users with their SIDs; every ID is validated here, before any SQL runs."""
    names = user_names(environment)
    users: list[ContainedUser] = []
    for role in DATABASE_ROLES:
        name, user_type = names[role]
        if user_type == "E":
            source = IDENTITY_OUTPUTS[sid_source][role]
            identifier = outputs.get(source, "")
            if not identifier:
                raise StepError(f"the azd output {source} is missing")
        else:
            field = GROUP_FIELDS[role]
            identifier = groups.get(field, "")
            if not identifier:
                raise StepError(f"groups.{field} is missing")
        users.append(ContainedUser(name, user_type, sid_literal(identifier), role))
    return tuple(users)


def plan(
    expected: Sequence[ContainedUser],
    principals: Sequence[Principal],
    memberships: Sequence[tuple[str, str]],
) -> list[TextClause]:
    """The statements that bring the database to the expected principals; none when it is
    already there."""
    existing = {principal.name: principal for principal in principals}
    planned: list[TextClause] = []
    recreated: set[str] = set()
    for user in expected:
        current = existing.get(user.name)
        if current is not None:
            same_sid = current.sid is not None and current.sid.hex() == user.sid_hex
            if current.principal_type == user.user_type and same_sid:
                continue
            # An identity re-created under the same name has a new ID, so its user maps to
            # nobody until it is re-created with the new SID.
            planned.append(statements.drop_user(user.name))
        planned.append(statements.create_user(user.name, user.sid_hex, user.user_type))
        recreated.add(user.name)
    wanted = {(user.role, user.name) for user in expected}
    roles = set(DATABASE_ROLES)
    for role, member in memberships:
        if role in roles and (role, member) not in wanted:
            planned.append(statements.drop_role_member(role, member))
    present = {(role, member) for role, member in memberships if member not in recreated}
    planned.extend(
        statements.add_role_member(user.role, user.name)
        for user in expected
        if (user.role, user.name) not in present
    )
    return planned


def read_state(connection: Connection) -> tuple[list[Principal], list[tuple[str, str]]]:
    principals = [
        Principal(name=row.name, principal_type=row.type, sid=row.sid)
        for row in connection.execute(statements.DATABASE_PRINCIPALS)
    ]
    memberships = [
        (row.role_name, row.member_name) for row in connection.execute(statements.ROLE_MEMBERS)
    ]
    return principals, memberships


def apply(connection: Connection, expected: Sequence[ContainedUser]) -> int:
    """Plan against the current state and run the statements in the caller's transaction;
    returns how many ran."""
    principals, memberships = read_state(connection)
    planned = plan(expected, principals, memberships)
    for statement in planned:
        connection.execute(statement)
    return len(planned)


def access_token() -> str:
    token: str = AzureCliCredential().get_token(TOKEN_SCOPE).token
    return token


def report_failure(code: str | None, message: str, detail: str) -> None:
    """One JSON line on standard error, and the same in the job summary when there is one
    (`83-infrastructure.md` section 10.18); `code` is `null` for a failure that section's table
    gives no code."""
    line = json.dumps({"code": code, "message": message, "detail": detail})
    sys.stderr.write(line + "\n")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with Path(summary).open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")


def parse_arguments(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Create the contained users and memberships.")
    parser.add_argument("--env", required=True, choices=ENVIRONMENTS)
    parser.add_argument("--sid-source", choices=("client-id", "object-id"), default="client-id")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    arguments = parse_arguments(argv)
    environment: Environment = arguments.env
    step = "inputs"
    try:
        expected = expected_users(
            environment,
            outputs=read_azd_environment(environment),
            groups=read_groups(environment),
            sid_source=arguments.sid_source,
        )
        step = "grants"
        engine = create_sync_engine("grants", access_token=access_token())
        with engine.begin() as connection:
            count = apply(connection, expected)
        engine.dispose()
    except StepError as error:
        report_failure(FAILURE_CODE, FAILURE_MESSAGE, f"{step}: {error.detail}")
        return EXIT_FAILURE
    except Exception as error:  # noqa: BLE001 - the step's boundary: type only, never the text
        report_failure(FAILURE_CODE, FAILURE_MESSAGE, f"{step}: {type(error).__name__}")
        return EXIT_FAILURE
    sys.stdout.write(json.dumps({"statements": count, "users": len(expected)}) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
