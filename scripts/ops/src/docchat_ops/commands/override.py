"""`override list`, `override set` and `override clear` (`86-operations.md` section 2.8.3 and
runbook R04; `81-config.md` section 3; `80-data-model.md` sections 2.6 and 2.21).

An operator at level `config` reads and changes `app.user_index_overrides` under the SQL role
`docchat_config_operator`, which may read `users` and `project_indexes` to validate an override.
An override sends one user's chat in one project to another index mapped to that project for 1 to
30 days; the API reads it on the user's next request and ignores it once `expires_at` has passed,
so an expired row stays until `set` replaces it or `clear` removes it. `set` updates the row under
`UPDLOCK, HOLDLOCK`, or inserts it when the update found none, and `clear` deletes it, each with
its audit row in the same transaction, never retried. The operator's reason is stored only in the
row's `reason` column: no command prints it and no audit row carries it.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Final
from uuid import UUID

from sqlalchemy import case, delete, insert, select, update

from docchat_core.audit import write_audit_event
from docchat_core.audit.models import (
    UserIndexOverrideClearedMetadata,
    UserIndexOverrideSetMetadata,
)
from docchat_core.sql.engine import SqlError
from docchat_core.sql.repositories.common import ONE, ZERO, utc
from docchat_core.sql.tables.access import project_indexes, user_index_overrides, users
from docchat_core.sql.tables.schema import SYSUTCDATETIME
from docchat_ops.commands.ingestion import (
    RERUN,
    RowChangedError,
    apply_change,
    identifier,
    operator_session,
    utc_text,
)
from docchat_ops.framework.audit import operator_event
from docchat_ops.framework.exitcodes import Conflict, NotFound, UsageError

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from pydantic import JsonValue
    from sqlalchemy import Connection, Select

    from docchat_core.audit import AuditEvent
    from docchat_ops.cli import Run
    from docchat_ops.framework.audit import OpsDatabase

LIST_UNIT: Final = "override_list"
READ_UNIT: Final = "override_read"
SET_UNIT: Final = "override_set"
CLEAR_UNIT: Final = "override_clear"

DAYS_MIN: Final = 1
DAYS_MAX: Final = 30
DAYS_DEFAULT: Final = 7
REASON_MIN_LENGTH: Final = 10
"""`CK_user_index_overrides_reason`: `LEN(reason) >= 10`, and `LEN` doesn't count trailing
blanks."""
KEY_RANGE_LOCK: Final = "WITH (UPDLOCK, HOLDLOCK)"
"""`HOLDLOCK` also locks the absent key, so two `set` runs for one user and project serialise and
the second updates the row the first inserted."""
FOREIGN_KEY_CONFLICT: Final = 547
"""A statement conflicted with a constraint; after the command's own checks, only a foreign key:
the user's row or the index's mapping went away after it was read."""

LIST_COLUMNS: Final = (
    "userObjectId",
    "projectId",
    "indexId",
    "setBy",
    "setAt",
    "expiresAt",
    "expired",
)
NEXT_REQUEST: Final = "the override applies to the user's next chat request"
LIST_STEP: Final = "override list shows the overrides"

_overrides = user_index_overrides.c
# An override is in force while `expires_at > SYSUTCDATETIME()`, the API's own rule.
_EXPIRED: Final = case((_overrides.expires_at > SYSUTCDATETIME, ZERO), else_=ONE).label("expired")


def days(value: str) -> int:
    """`--days`: a whole number from 1 to 30."""
    try:
        number = int(value)
    except ValueError:
        number = 0
    if not DAYS_MIN <= number <= DAYS_MAX:
        raise argparse.ArgumentTypeError(f"--days is a whole number from {DAYS_MIN} to {DAYS_MAX}")
    return number


def list_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--user", type=identifier, help="one user's object ID; every user when absent"
    )
    parser.add_argument(
        "--project", type=identifier, help="one project's ID; every project when absent"
    )


def set_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--user", type=identifier, required=True, help="the user's object ID")
    parser.add_argument("--project", type=identifier, required=True, help="the project's ID")
    parser.add_argument(
        "--index", type=identifier, required=True, help="the logical index, mapped to the project"
    )
    parser.add_argument(
        "--days",
        type=days,
        default=DAYS_DEFAULT,
        help=f"{DAYS_MIN} to {DAYS_MAX} days from the change, default {DAYS_DEFAULT}",
    )


def clear_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--user", type=identifier, required=True, help="the user's object ID")
    parser.add_argument("--project", type=identifier, required=True, help="the project's ID")


def _lower(value: object) -> str | None:
    return None if value is None else str(value).lower()


# --- list -----------------------------------------------------------------------------------------


def _list_statement(user: UUID | None, project: UUID | None) -> Select[Any]:
    # Never the reason: it stays in the row.
    statement = select(
        _overrides.user_object_id,
        _overrides.project_id,
        _overrides.search_index_id,
        _overrides.set_by,
        _overrides.set_at,
        _overrides.expires_at,
        _EXPIRED,
    ).order_by(_overrides.user_object_id, _overrides.project_id)
    if user is not None:
        statement = statement.where(_overrides.user_object_id == user)
    if project is not None:
        statement = statement.where(_overrides.project_id == project)
    return statement


async def list_overrides(run: Run, args: argparse.Namespace) -> None:
    statement = _list_statement(args.user, args.project)
    target = run.azd().sql_target()

    def read(connection: Connection) -> list[Any]:
        return list(connection.execute(statement).all())

    async with run.toolkit.credential() as credential:
        database = run.toolkit.database(target, credential)
        try:
            rows = await database.run(read, name=LIST_UNIT, retry="transient")
        finally:
            await database.dispose()
    printed: list[JsonValue] = [
        {
            "userObjectId": _lower(row.user_object_id),
            "projectId": _lower(row.project_id),
            "indexId": _lower(row.search_index_id),
            "setBy": _lower(row.set_by),
            "setAt": utc_text(row.set_at),
            "expiresAt": utc_text(row.expires_at),
            "expired": row.expired == 1,
        }
        for row in rows
    ]
    run.report("listed", {"overrides": printed}, rows="overrides", columns=LIST_COLUMNS)


# --- set and clear --------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Current:
    """The override a user has in a project, as read before the change."""

    index_id: UUID
    expires_at: datetime
    expired: bool

    def line(self) -> str:
        state = "expired" if self.expired else "in force"
        expiry = utc_text(self.expires_at)
        return f"current override: index {self.index_id}, expires at {expiry} ({state})"


def _current_override(connection: Connection, user: UUID, project: UUID) -> _Current | None:
    row = connection.execute(
        select(_overrides.search_index_id, _overrides.expires_at, _EXPIRED).where(
            _overrides.user_object_id == user, _overrides.project_id == project
        )
    ).first()
    if row is None:
        return None
    return _Current(
        index_id=UUID(str(row.search_index_id)), expires_at=row.expires_at, expired=row.expired == 1
    )


@dataclass(frozen=True)
class _SetFacts:
    user_found: bool
    mapped: bool
    current: _Current | None


async def _read_set_facts(
    database: OpsDatabase, user: UUID, project: UUID, index: UUID
) -> _SetFacts:
    def read(connection: Connection) -> _SetFacts:
        user_found = (
            connection.execute(select(users.c.object_id).where(users.c.object_id == user)).first()
            is not None
        )
        mapped = (
            connection.execute(
                select(project_indexes.c.search_index_id).where(
                    project_indexes.c.project_id == project,
                    project_indexes.c.search_index_id == index,
                )
            ).first()
            is not None
        )
        return _SetFacts(user_found, mapped, _current_override(connection, user, project))

    return await database.run(read, name=READ_UNIT, retry="transient")


def _to_milliseconds(value: datetime) -> datetime:
    # `DATETIME2(3)` keeps milliseconds; truncating here makes the stored expiry and the audit
    # row's `expiresAt` the same instant, and keeps `expires_at - set_at` exactly the days given.
    naive = utc(value).replace(tzinfo=None)
    return naive.replace(microsecond=naive.microsecond // 1000 * 1000)


@dataclass(frozen=True)
class _Applied:
    expires_at: datetime
    replaced: bool


def _set_event(
    run: Run, *, actor: str, user: UUID, project: UUID, index: UUID, applied: _Applied
) -> AuditEvent:
    return operator_event(
        action="user_index_override.set",
        actor_object_id=actor,
        subject_object_id=str(user),
        target_type="user_index_override",
        target_id=str(user),
        project_id=str(project),
        metadata=UserIndexOverrideSetMetadata(
            search_index_id=index,
            expires_at=utc(applied.expires_at),
            replaced=applied.replaced,
            ticket=run.ticket or "",
        ),
    )


async def _apply[T](database: OpsDatabase, work: Callable[[Connection], T], name: str) -> T:
    """`apply_change`, with a foreign-key conflict also leaving nothing written (exit 6)."""
    try:
        return await apply_change(database, work, name)
    except SqlError as error:
        if error.number != FOREIGN_KEY_CONFLICT:
            raise
        raise Conflict(
            "the user's row or the index's mapping to the project changed since it was read; "
            "nothing was written",
            RERUN,
        ) from None


async def set_override(run: Run, args: argparse.Namespace) -> None:
    user: UUID = args.user
    project: UUID = args.project
    index: UUID = args.index
    lifetime: int = args.days
    reason = run.reason or ""
    if len(reason.rstrip(" ")) < REASON_MIN_LENGTH:
        raise UsageError(
            "the reason has fewer than 10 characters before its trailing blanks",
            "give a reason of 10 to 500 characters",
        )
    environment = run.azd()
    async with operator_session(run, environment) as session:
        facts = await _read_set_facts(session.database, user, project, index)
        if not facts.user_found:
            raise NotFound(
                f"no users row for {user}; nothing was written",
                "the user signs in once, then rerun the command",
            )
        if not facts.mapped:
            raise UsageError(
                f"index {index} isn't mapped to project {project}; nothing was written",
                "an admin maps the index to the project in the Projects screen first",
            )
        await run.confirm(
            [
                f"user: {user}",
                f"project: {project}",
                f"index: {index}",
                facts.current.line() if facts.current else "current override: none",
                f"change: the override expires {lifetime} days after the change"
                + ("; it replaces the current override" if facts.current else ""),
            ]
        )

        def work(connection: Connection) -> _Applied:
            now = _to_milliseconds(
                connection.execute(select(SYSUTCDATETIME.label("now"))).scalar_one()
            )
            expires_at = now + timedelta(days=lifetime)
            values = {
                "search_index_id": index,
                "reason": reason,
                "set_by": UUID(session.actor),
                "set_at": now,
                "expires_at": expires_at,
            }
            replaced = (
                connection.execute(
                    update(user_index_overrides)
                    .with_hint(KEY_RANGE_LOCK, dialect_name="mssql")
                    .where(_overrides.user_object_id == user, _overrides.project_id == project)
                    .values(**values)
                ).rowcount
                == 1
            )
            if not replaced:
                connection.execute(
                    insert(user_index_overrides).values(
                        user_object_id=user, project_id=project, **values
                    )
                )
            applied = _Applied(expires_at=expires_at, replaced=replaced)
            event = _set_event(
                run, actor=session.actor, user=user, project=project, index=index, applied=applied
            )
            write_audit_event(connection, event)
            return applied

        applied = await _apply(session.database, work, SET_UNIT)
    run.progress(NEXT_REQUEST)
    run.report(
        "set",
        {
            "userObjectId": str(user),
            "projectId": str(project),
            "indexId": str(index),
            "expiresAt": utc_text(applied.expires_at),
            "replaced": applied.replaced,
        },
    )


async def _read_current(database: OpsDatabase, user: UUID, project: UUID) -> _Current | None:
    return await database.run(
        lambda connection: _current_override(connection, user, project),
        name=READ_UNIT,
        retry="transient",
    )


def _summary(user: UUID, project: UUID, current: _Current) -> Sequence[str]:
    return [
        f"user: {user}",
        f"project: {project}",
        current.line(),
        "change: the override is deleted; the user's next request uses the project's default index",
    ]


async def clear_override(run: Run, args: argparse.Namespace) -> None:
    user: UUID = args.user
    project: UUID = args.project
    environment = run.azd()
    async with operator_session(run, environment) as session:
        current = await _read_current(session.database, user, project)
        if current is None:
            raise NotFound(f"no override for user {user} in project {project}", LIST_STEP)
        await run.confirm(_summary(user, project, current))
        event = operator_event(
            action="user_index_override.cleared",
            actor_object_id=session.actor,
            subject_object_id=str(user),
            target_type="user_index_override",
            target_id=str(user),
            project_id=str(project),
            metadata=UserIndexOverrideClearedMetadata(
                search_index_id=current.index_id, ticket=run.ticket or ""
            ),
        )
        # Conditioned on the override the summary showed, so the audit row names the index that
        # was really removed.
        statement = delete(user_index_overrides).where(
            _overrides.user_object_id == user,
            _overrides.project_id == project,
            _overrides.search_index_id == current.index_id,
            _overrides.expires_at == current.expires_at,
        )

        def work(connection: Connection) -> None:
            if connection.execute(statement).rowcount != 1:
                raise RowChangedError
            write_audit_event(connection, event)

        await apply_change(session.database, work, CLEAR_UNIT)
    run.report(
        "cleared",
        {"userObjectId": str(user), "projectId": str(project), "indexId": str(current.index_id)},
    )
