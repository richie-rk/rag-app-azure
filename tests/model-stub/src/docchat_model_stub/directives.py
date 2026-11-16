"""The `[[stub:<directive>]]` tokens that change one request (84-testing.md section 2.3).

A directive followed by `@<kind>` applies only to that kind of request; without a qualifier it
applies to every request that carries it. A request only sees the directives that apply to its
own kind, so an unknown directive qualified for another kind never refuses this request.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

type Kind = Literal["stream", "json", "text", "embed", "shield"]

DIRECTIVE_PATTERN = re.compile(
    r"\[\[stub:([a-z_]+)(?:=([0-9a-z_]+))?(?:@(stream|json|text|embed|shield))?\]\]"
)

_MIN_ERROR_STATUS = 400
_MAX_ERROR_STATUS = 599


class UnknownDirectiveError(ValueError):
    """A directive token whose name, or whose value for that name, the stub doesn't define."""

    def __init__(self, token: str) -> None:
        super().__init__(f"unknown stub directive {token}")
        self.token = token


@dataclass(frozen=True)
class Directives:
    """The directives that apply to one request; a later token of the same name wins."""

    status: int | None = None
    content_filter: Literal["prompt", "completion"] | None = None
    cut: int | None = None
    no_usage: bool = False
    tokens: int | None = None
    delay_ms: int | None = None
    first_ms: int | None = None
    hang: bool = False
    shield_attack: bool = False


def _count(token: str, value: str | None, *, minimum: int, maximum: int | None = None) -> int:
    # The token pattern admits `_` in values, which int() would accept as a digit separator.
    if value is None or not value.isdigit():
        raise UnknownDirectiveError(token)
    count = int(value)
    if count < minimum or (maximum is not None and count > maximum):
        raise UnknownDirectiveError(token)
    return count


def _flag(token: str, value: str | None) -> bool:
    if value is not None:
        raise UnknownDirectiveError(token)
    return True


def _choice(token: str, value: str | None, choices: tuple[str, ...]) -> str:
    if value is None or value not in choices:
        raise UnknownDirectiveError(token)
    return value


type _Parser = Callable[[str, str | None], dict[str, Any]]

# Each directive's name, and how its token and value become fields of `Directives`.
_PARSERS: dict[str, _Parser] = {
    "status": lambda token, value: {
        "status": _count(token, value, minimum=_MIN_ERROR_STATUS, maximum=_MAX_ERROR_STATUS)
    },
    "filter": lambda token, value: {
        "content_filter": _choice(token, value, ("prompt", "completion"))
    },
    "cut": lambda token, value: {"cut": _count(token, value, minimum=0)},
    "no_usage": lambda token, value: {"no_usage": _flag(token, value)},
    "tokens": lambda token, value: {"tokens": _count(token, value, minimum=1)},
    "delay_ms": lambda token, value: {"delay_ms": _count(token, value, minimum=0)},
    "first_ms": lambda token, value: {"first_ms": _count(token, value, minimum=0)},
    "hang": lambda token, value: {"hang": _flag(token, value)},
    "shield": lambda token, value: {"shield_attack": bool(_choice(token, value, ("attack",)))},
}


def collect(texts: Iterable[str], kind: Kind) -> Directives:
    """Parse the directives of `texts` that apply to a request of `kind`.

    Raises `UnknownDirectiveError` for an applicable token the stub doesn't define.
    """
    directives = Directives()
    for text in texts:
        for match in DIRECTIVE_PATTERN.finditer(text):
            token = match.group(0)
            name, value, qualifier = match.groups()
            if qualifier is not None and qualifier != kind:
                continue
            parser = _PARSERS.get(name)
            if parser is None:
                raise UnknownDirectiveError(token)
            directives = replace(directives, **parser(token, value))
    return directives


def marks_attack(text: str) -> bool:
    """Whether one screening text carries `shield=attack` for screening requests."""
    return collect((text,), "shield").shield_attack
