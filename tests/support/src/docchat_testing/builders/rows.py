"""What the SQL row builders share: merging overrides into defaults, checked against the table,
and the value forms the columns take."""

from __future__ import annotations

import hashlib
import json
import secrets
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import Table


def build_row(
    table: Table, defaults: Mapping[str, Any], overrides: Mapping[str, Any]
) -> dict[str, Any]:
    """`defaults` with `overrides` applied, every name checked against `table`, so a misspelt
    override fails the test that wrote it instead of being dropped by the insert."""
    unknown = sorted(set(defaults) - set(table.c.keys())) + sorted(
        set(overrides) - set(table.c.keys())
    )
    if unknown:
        raise KeyError(f"{table.name} has no column {', '.join(unknown)}")
    return {**defaults, **overrides}


def new_id() -> UUID:
    return uuid4()


def sql_now() -> datetime:
    """Now, as a `DATETIME2(3)` column stores it: UTC without an offset, to the millisecond."""
    now = datetime.now(UTC).replace(tzinfo=None)
    return now.replace(microsecond=now.microsecond // 1000 * 1000)


def compact_json(value: object) -> str:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def sha256_of(value: str) -> bytes:
    """A `BINARY(32)` hash of `value`'s UTF-8 bytes."""
    return hashlib.sha256(value.encode("utf-8")).digest()


def random_sha256() -> bytes:
    """A `BINARY(32)` value no other row has: the hash of bytes nobody uploaded."""
    return secrets.token_bytes(32)
