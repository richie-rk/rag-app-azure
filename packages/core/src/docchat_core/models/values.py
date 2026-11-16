"""Value types shared by several `docchat_core` subpackages (`11-api.md` sections 1.7, 1.9 and 4.1;
`80-data-model.md` section 1.4)."""

from __future__ import annotations

import base64
import re
from typing import Annotated, Final

from pydantic import Field, StringConstraints

COUNT_MAX: Final = 2_147_483_647
SHA256_PATTERN: Final = r"^[0-9a-f]{64}$"
ROW_VERSION_PATTERN: Final = r"^[A-Za-z0-9+/]{11}=$"
ROW_VERSION_BYTES: Final = 8

_ROW_VERSION_RE: Final = re.compile(ROW_VERSION_PATTERN)

Count = Annotated[int, Field(ge=0, le=COUNT_MAX)]
"""A count: an integer from 0 to 2,147,483,647, the range of SQL `INT`."""

Sha256Hex = Annotated[str, StringConstraints(pattern=SHA256_PATTERN)]
"""A SHA-256 value as 64 lower-case hexadecimal characters (`BINARY(32)` in SQL)."""

RowVersion = Annotated[str, StringConstraints(pattern=ROW_VERSION_PATTERN)]
"""An 8-byte SQL `ROWVERSION` as 12 characters of standard base64 with padding."""


def encode_row_version(value: bytes) -> str:
    """The `rowVersion` text of a SQL `ROWVERSION` value."""
    if len(value) != ROW_VERSION_BYTES:
        raise ValueError("a row version has exactly 8 bytes")
    return base64.b64encode(value).decode("ascii")


def decode_row_version(text: str) -> bytes:
    """The 8 bytes a `rowVersion` text names; refuses any other shape."""
    if not _ROW_VERSION_RE.fullmatch(text):
        raise ValueError("not a row version")
    # The pattern admits only 11 base64 characters and one pad, which always decode to 8 bytes.
    return base64.b64decode(text, validate=True)
