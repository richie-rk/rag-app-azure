"""Canary values: every content value a test writes (`84-testing.md` section 1.1 item 5).

`canary(kind)` embeds `cnry-<kind>-<8 hex>`, and the session's telemetry audit fails when that
marker reaches a span, a log record or a metric (section 2.10), so a test that leaks content into
telemetry fails without asserting anything itself.
"""

from __future__ import annotations

import re
import secrets
from typing import Final, Literal, get_args

type CanaryKind = Literal[
    "question",
    "answer",
    "chunk",
    "filename",
    "fact",
    "email",
    "displayname",
    "instruction",
    "comment",
    "title",
    "summary",
]

CANARY_KINDS: Final[tuple[str, ...]] = get_args(CanaryKind.__value__)
CANARY_PREFIX: Final = "cnry-"
CANARY_PATTERN: Final = re.compile(r"cnry-([a-z]+)-([0-9a-f]{8})")
"""Finds the marker anywhere in a value, as the telemetry audit does."""

EMAIL_CANARY_DOMAIN: Final = "example.com"
"""A reserved domain (RFC 2606), so an e-mail canary is an address that reaches nobody."""


def canary(kind: CanaryKind) -> str:
    """A fresh value of `kind` holding `cnry-<kind>-<8 hex>`.

    An `email` canary is the marker followed by `@example.com`, so code that validates e-mail
    addresses accepts it and the audit's e-mail rule catches it as well as the marker rule.
    """
    if kind not in CANARY_KINDS:
        raise ValueError(f"unknown canary kind {kind!r}")
    marker = f"{CANARY_PREFIX}{kind}-{secrets.token_hex(4)}"
    return f"{marker}@{EMAIL_CANARY_DOMAIN}" if kind == "email" else marker


def is_canary(value: str) -> bool:
    """True when `value` holds a canary marker of a known kind."""
    return any(match.group(1) in CANARY_KINDS for match in CANARY_PATTERN.finditer(value))
