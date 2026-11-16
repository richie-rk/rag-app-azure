"""Optimistic concurrency: the one `If-Match` parser of the 14 operations that require it
(`11-api.md` section 1.9; `82-security.md` SEC-29; ADR 0094).

The header is read before the operation's lookups but answered at step 9 of section 1.3, after
every lookup (`404`) and before every state rule (`409`). So `if_match_header` never raises:
it returns an `IfMatchHeader`, and the operation calls `require_row_version` or `require_etag` once
its lookups have passed.

1. No `If-Match` header: `428 precondition_required`.
2. Repeated headers are joined with `,` and the value trimmed; `*`, a value starting with `W/`, or
   any value containing `,`: `428`.
3. The value must start and end with `"` with at least one character between: else `428`.
4. A row version: the text between the quotes must be 12 characters of standard base64
   (`^[A-Za-z0-9+/]{11}=$`), else `412 precondition_failed`, because it can never be current; its
   8 bytes are compared with the row's `row_version`. A memory `etag`: the whole value, quotes
   included, is compared with the document's `_etag`. A mismatch is `412`.

The API sends no `ETag` header: version tokens travel in bodies, because cross-origin scripts
can't read response headers.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

from fastapi import Request

from docchat_core.errors import DocChatProblem
from docchat_core.models import ROW_VERSION_PATTERN, decode_row_version

HEADER: Final = "if-match"
_ROW_VERSION_RE: Final = re.compile(ROW_VERSION_PATTERN)
_QUOTE: Final = '"'


class PreconditionRequired(DocChatProblem):
    """`428 precondition_required`: no usable `If-Match`."""

    code = "precondition_required"


class PreconditionFailed(DocChatProblem):
    """`412 precondition_failed`: the version sent isn't the current one."""

    code = "precondition_failed"


@dataclass(frozen=True, slots=True)
class IfMatchHeader:
    """The request's `If-Match`, joined and trimmed; `None` when absent."""

    value: str | None

    def require(self) -> str:
        """Steps 1 to 3: the strong, single, quoted tag, quotes included."""
        value = self.value
        if value is None or value == "*" or value.startswith("W/") or "," in value:
            raise PreconditionRequired()
        if len(value) < len(_QUOTE) * 2 + 1 or not (
            value.startswith(_QUOTE) and value.endswith(_QUOTE)
        ):
            raise PreconditionRequired()
        return value

    def require_row_version(self, current: bytes) -> None:
        """Step 4 for a SQL resource: the tag is the row's 8-byte `ROWVERSION`."""
        opaque = self.require()[1:-1]
        if not _ROW_VERSION_RE.fullmatch(opaque) or decode_row_version(opaque) != current:
            raise PreconditionFailed()

    def require_etag(self, current: str) -> None:
        """Step 4 for the memory document: the whole value equals its `_etag` as an entity tag
        (`entity_tag`)."""
        if self.require() != entity_tag(current):
            raise PreconditionFailed()


def entity_tag(etag: str) -> str:
    """A Cosmos DB `_etag` as the strong entity tag the memory operations return as `etag` and
    compare with `If-Match`: verbatim when it starts and ends with `"`, as Azure Cosmos DB's
    always does; otherwise wrapped in double quotes, because the Linux emulator (vNext) returns
    a bare GUID, which step 3 would refuse. A conditioned write still names the `_etag` exactly
    as the store read it."""
    if len(etag) > len(_QUOTE) and etag.startswith(_QUOTE) and etag.endswith(_QUOTE):
        return etag
    return f"{_QUOTE}{etag}{_QUOTE}"


def parse_if_match(values: list[str]) -> IfMatchHeader:
    """The header values joined with `,` and trimmed (step 2)."""
    if not values:
        return IfMatchHeader(None)
    return IfMatchHeader(",".join(values).strip())


def if_match_header(request: Request) -> IfMatchHeader:
    """The dependency of the `If-Match` operations: `Depends(if_match_header)`."""
    return parse_if_match(request.headers.getlist(HEADER))
