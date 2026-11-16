"""`config show`, `config set` and `config reset` (`86-operations.md` section 2.8.2; `81-config.md`
section 3.3; ADR 0028).

An operator at level `config` reads and changes `app.ops_config` under the SQL role
`docchat_config_operator`. A change is validated with the area's model against the area's current
values, cross-field rules included, through `docchat_core.opsconfig.validate_area`, the rule every
process loads its snapshot with, so the CLI can't store a value the apps would refuse; the `summary`
and `memory` areas are validated against the chat-small model's row of the capability table, as
the API loads them. The row is
updated on the `row_version` it was read with, and its audit row is inserted in the same
transaction; the running processes read the new value within 60 seconds.
"""

from __future__ import annotations

import argparse
import json
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final, Literal, NoReturn

from pydantic import ValidationError
from sqlalchemy import func, select, update

from docchat_core.audit import write_audit_event
from docchat_core.audit.models import OpsConfigResetMetadata, OpsConfigSetMetadata
from docchat_core.models import format_utc
from docchat_core.opsconfig import (
    CATALOGUE,
    EFFORT_AREAS,
    SNAPSHOT_MAX_AGE_SECONDS,
    StoredValueError,
    area_keys,
    decode_value,
    encode_value,
    failing_keys,
    validate_area,
)
from docchat_core.sql.tables.access import ops_config
from docchat_ops.commands.chat import chat_small_efforts
from docchat_ops.framework.audit import connect, operator_event
from docchat_ops.framework.exitcodes import Conflict, NotFound, UsageError

if TYPE_CHECKING:
    from collections.abc import Collection, Sequence
    from typing import Any

    from pydantic import JsonValue
    from sqlalchemy import Connection

    from docchat_core.audit import AuditEvent
    from docchat_core.opsconfig import Area
    from docchat_ops.cli import Run
    from docchat_ops.framework.audit import OpsDatabase

READ_UNIT: Final = "ops_config_read"
CHANGE_UNIT: Final = "ops_config_change"
SHOW_COLUMNS: Final = ("key", "value", "default", "updatedAt", "updatedBy")
CHANGED_BY_SOMEONE_ELSE: Final = "the row changed since it was read; nothing was written"


def key(value: str) -> str:
    """A key of the catalogue, the 73 keys the running code declares (exit 2 otherwise)."""
    if value not in CATALOGUE:
        raise argparse.ArgumentTypeError("an unknown ops-config key; config show lists them")
    return value


def _not_json(name: str) -> NoReturn:
    # `NaN` and `Infinity` aren't JSON, though Python's parser accepts them.
    raise ValueError(f"{name} is not JSON")


def json_value(value: str) -> JsonValue:
    """`--value`: a JSON value, parsed as data and never evaluated."""
    try:
        parsed: JsonValue = json.loads(value, parse_constant=_not_json)
    except ValueError:
        raise argparse.ArgumentTypeError('--value is a JSON value, such as 4 or "low"') from None
    return parsed


def show_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--key", type=key, help="one key; every key when absent")


def set_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--key", type=key, required=True, help="the dotted key")
    parser.add_argument("--value", type=json_value, required=True, help="the new JSON value")


def reset_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--key", type=key, required=True, help="the dotted key")


def _utc(value: object) -> str | None:
    if not isinstance(value, datetime):
        return None
    # DATETIME2 carries no zone; the column holds UTC.
    return format_utc(value if value.tzinfo else value.replace(tzinfo=UTC))


def _decoded(text: object) -> JsonValue:
    try:
        return decode_value(str(text))
    except StoredValueError:
        return None


async def show(run: Run, args: argparse.Namespace) -> None:
    wanted: str | None = args.key
    target = run.azd().sql_target()
    statement = select(
        ops_config.c.config_key,
        ops_config.c.value_json,
        ops_config.c.default_json,
        ops_config.c.updated_at,
        ops_config.c.updated_by,
    ).order_by(ops_config.c.config_key)
    if wanted is not None:
        statement = statement.where(ops_config.c.config_key == wanted)

    def read(connection: Connection) -> list[dict[str, Any]]:
        return [dict(row._mapping) for row in connection.execute(statement).all()]

    async with run.toolkit.credential() as credential:
        database = run.toolkit.database(target, credential)
        try:
            rows = await database.run(read, name=READ_UNIT, retry="transient")
        finally:
            await database.dispose()
    if wanted is not None and not rows:
        raise NotFound(f"no row for {wanted}", "apply the migrations that ship the key")
    printed: list[JsonValue] = [
        {
            "key": str(row["config_key"]),
            "value": _decoded(row["value_json"]),
            "default": _decoded(row["default_json"]),
            "updatedAt": _utc(row["updated_at"]),
            "updatedBy": None if row["updated_by"] is None else str(row["updated_by"]).lower(),
        }
        for row in rows
    ]
    run.report("listed", {"keys": printed}, rows="keys", columns=SHOW_COLUMNS)


def _area_rows(connection: Connection, area: Area) -> dict[str, dict[str, Any]]:
    statement = select(
        ops_config.c.config_key,
        ops_config.c.value_json,
        ops_config.c.default_json,
        ops_config.c.row_version,
    ).where(ops_config.c.config_key.in_(area_keys(area)))
    return {str(row.config_key): dict(row._mapping) for row in connection.execute(statement).all()}


def _validate(
    area: Area,
    rows: dict[str, dict[str, Any]],
    changed: str,
    new: JsonValue,
    efforts: Collection[str] | None,
) -> None:
    """The area's model, with the changed key's new value, accepts every key (exit 2 otherwise,
    quoting the allowed range). `efforts` is the chat-small model's row of the capability table,
    which the summary and memory areas are valid against."""
    values: dict[str, JsonValue] = {}
    for name, row in rows.items():
        try:
            values[name] = decode_value(str(row["value_json"]))
        except StoredValueError:
            continue
    values[changed] = new
    try:
        validate_area(area, values, chat_small_efforts=efforts)
    except ValidationError as error:
        quoted = [
            str(detail["msg"])
            for detail in error.errors(include_url=False, include_input=False)
            if detail["loc"] and detail["loc"][0] == changed
        ]
        keys = ", ".join(failing_keys(area, error))
        allowed = f" ({'; '.join(quoted)})" if quoted else ""
        raise UsageError(
            f"the {area} area refuses the value: {keys}{allowed}",
            "choose a value inside the allowed range and the area's cross-field rules",
        ) from None


class _RowChangedError(Exception):
    """Zero rows updated: another change won the row; the transaction rolls back."""


async def _change(
    run: Run,
    args: argparse.Namespace,
    action: Literal["ops_config.set", "ops_config.reset"],
) -> None:
    changed: str = args.key
    area = CATALOGUE[changed]
    target = run.azd().sql_target()
    efforts = chat_small_efforts(run.root(), run.environment) if area in EFFORT_AREAS else None
    await run.refuse_service_principal()
    async with run.toolkit.credential() as credential:
        database = run.toolkit.database(target, credential)
        try:
            actor = await connect(database)
            rows = await database.run(
                lambda connection: _area_rows(connection, area), name=READ_UNIT, retry="transient"
            )
            row = rows.get(changed)
            if row is None:
                raise NotFound(f"no row for {changed}", "apply the migrations that ship the key")
            previous = _decoded(row["value_json"])
            new: JsonValue = (
                args.value if action == "ops_config.set" else _decoded(row["default_json"])
            )
            _validate(area, rows, changed, new, efforts)
            await run.confirm(_summary(changed, area, previous, new))
            event = _event(
                run,
                action=action,
                actor=actor,
                changed=changed,
                area=area,
                previous=previous,
                new=new,
            )
            await _apply(
                database,
                changed=changed,
                row_version=row["row_version"],
                new=new,
                reason=run.reason or "",
                actor=actor,
                event=event,
            )
        finally:
            await database.dispose()
    run.progress(f"the value is live within {int(SNAPSHOT_MAX_AGE_SECONDS)} seconds")
    run.report(
        "set" if action == "ops_config.set" else "reset",
        {
            "key": changed,
            "area": area,
            "previous": previous,
            "new": new,
            "liveWithinSeconds": int(SNAPSHOT_MAX_AGE_SECONDS),
        },
    )


def _summary(changed: str, area: str, previous: JsonValue, new: JsonValue) -> Sequence[str]:
    return [
        f"key: {changed} (area {area})",
        f"previous: {json.dumps(previous, separators=(',', ':'))}",
        f"new: {json.dumps(new, separators=(',', ':'))}",
    ]


def _event(
    run: Run,
    *,
    action: Literal["ops_config.set", "ops_config.reset"],
    actor: str,
    changed: str,
    area: str,
    previous: JsonValue,
    new: JsonValue,
) -> AuditEvent:
    ticket = run.ticket or ""
    metadata = (
        OpsConfigSetMetadata.build(area=area, previous=previous, new=new, ticket=ticket)
        if action == "ops_config.set"
        else OpsConfigResetMetadata.build(area=area, previous=previous, ticket=ticket)
    )
    return operator_event(
        action=action,
        actor_object_id=actor,
        target_type="ops_config",
        target_id=changed,
        metadata=metadata,
    )


async def _apply(
    database: OpsDatabase,
    *,
    changed: str,
    row_version: bytes,
    new: JsonValue,
    reason: str,
    actor: str,
    event: AuditEvent,
) -> None:
    """One transaction, never retried: the update conditioned on the `row_version` read, then the
    audit row; zero rows updated rolls back and exits 6."""
    statement = (
        update(ops_config)
        .where(ops_config.c.config_key == changed, ops_config.c.row_version == row_version)
        .values(
            value_json=encode_value(new),
            change_reason=reason,
            updated_by=uuid.UUID(actor),
            updated_at=func.sysutcdatetime(),
        )
    )

    def work(connection: Connection) -> None:
        if connection.execute(statement).rowcount != 1:
            raise _RowChangedError
        write_audit_event(connection, event)

    try:
        await database.run(work, name=CHANGE_UNIT, retry="none")
    except _RowChangedError:
        raise Conflict(
            CHANGED_BY_SOMEONE_ELSE, "changed by someone else; read it and rerun the command"
        ) from None


async def set_value(run: Run, args: argparse.Namespace) -> None:
    await _change(run, args, "ops_config.set")


async def reset_value(run: Run, args: argparse.Namespace) -> None:
    await _change(run, args, "ops_config.reset")
