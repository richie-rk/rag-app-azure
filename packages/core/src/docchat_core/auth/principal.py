"""`Principal`: the caller as the validated token describes it (`11-api.md` section 1.2; ADR 0017
item 6; ADR 0021 item 5).

Only `TokenValidator` builds a `Principal` on a request path, from claims whose signature,
issuer, audience, tenant, client and scope it has checked; the raw token is never kept. Every
decision reads `oid` and `roles`; `display_name` and `email` are display data for the caller's
own `app.users` row and `getCurrentUser`, which no decision, log record or span uses.
"""

from __future__ import annotations

import unicodedata
from typing import Annotated, Final, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from docchat_core.auth.settings import Client

type Role = Literal["Admin", "User"]

ROLES: Final[frozenset[Role]] = frozenset({"Admin", "User"})
"""The two app roles; any other value of the `roles` claim is dropped."""

DISPLAY_NAME_MAX_LENGTH: Final = 256
EMAIL_MAX_LENGTH: Final = 320

# Unicode categories removed from a display name: controls, format characters (the
# bidirectional overrides and zero-width characters among them), surrogates, private use and
# unassigned code points, which could reorder or hide text on an administrator's screen.
_REMOVED_CATEGORIES: Final = frozenset({"Cc", "Cf", "Cs", "Co", "Cn"})


class Principal(BaseModel):
    """The validated caller. `oid` keys every per-user store: its lower-case hyphenated text is
    the caller's Cosmos DB partition key value (`partition_key`)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    oid: UUID
    tid: UUID
    azp: UUID
    client: Client
    roles: Annotated[frozenset[Role], Field(min_length=1)]
    # Personal data stays out of the repr, so a principal in a message or a log never shows it.
    display_name: Annotated[str, Field(max_length=DISPLAY_NAME_MAX_LENGTH, repr=False)] = ""
    email: Annotated[str, Field(max_length=EMAIL_MAX_LENGTH, repr=False)] = ""

    @property
    def is_admin(self) -> bool:
        """`Admin` in `roles`; the only source of administrator rights."""
        return "Admin" in self.roles

    @property
    def partition_key(self) -> str:
        """`str(oid)`, the user-level partition key of `conversations`, `memories` and `usage`."""
        return str(self.oid)

    @property
    def last_known_role(self) -> Role:
        """The display label of the caller's `app.users` row: `Admin` when the token grants it,
        else `User`."""
        return "Admin" if self.is_admin else "User"


def roles_from(values: list[str]) -> frozenset[Role]:
    """The app roles among the `roles` claim's values."""
    return frozenset(role for role in ROLES if role in values)


def _utf16_length(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def _fits(text: str, limit: int) -> bool:
    # `app.users` stores the value in `NVARCHAR(limit)`, which counts UTF-16 code units, so a
    # code point outside the Basic Multilingual Plane takes two of the limit.
    return len(text) <= limit and _utf16_length(text) <= limit


def display_name_from(claim: object) -> str:
    """The `name` claim as display text: Unicode NFC, every code point of category Cc, Cf, Cs,
    Co or Cn removed, trimmed, cut to 256 code points (fewer when supplementary code points would
    overflow the `NVARCHAR(256)` column); `""` when absent or not a string."""
    if not isinstance(claim, str):
        return ""
    normalised = unicodedata.normalize("NFC", claim)
    kept = "".join(
        char for char in normalised if unicodedata.category(char) not in _REMOVED_CATEGORIES
    )
    cut = kept.strip()[:DISPLAY_NAME_MAX_LENGTH]
    while not _fits(cut, DISPLAY_NAME_MAX_LENGTH):
        cut = cut[:-1]
    return cut


def _usable_preferred_username(value: str) -> bool:
    return value.count("@") == 1 and "#" not in value and not any(c.isspace() for c in value)


def email_from(email: object, preferred_username: object) -> str:
    """The display e-mail address: the `email` claim, else `preferred_username` when it holds
    exactly one `@`, no whitespace and no `#`; trimmed; `""` when neither is usable.

    A value that doesn't fit 320 characters isn't used, because cutting an address would display
    another address.
    """
    if isinstance(email, str):
        candidate = email.strip()
        if candidate and _fits(candidate, EMAIL_MAX_LENGTH):
            return candidate
    if isinstance(preferred_username, str):
        candidate = preferred_username.strip()
        if _fits(candidate, EMAIL_MAX_LENGTH) and _usable_preferred_username(candidate):
            return candidate
    return ""
