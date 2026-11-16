"""`DocChatProblem`: the exception type every problem code is raised as (`11-api.md` sections 1.3
and 1.5; ADR 0012).

Each subclass names one code of `CODES`; the API's exception handler turns it into that code's
`application/problem+json` body, so a refusal raised anywhere in `docchat_core` or `docchat_api`
answers with the catalogue's status, title and text. The checks here keep a problem from carrying
an extension member its code doesn't define, and keep a code that has no HTTP status (a stream or
stored code) from ever becoming a problem.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import ClassVar, Final, cast, override
from uuid import UUID

from docchat_core.errors.codes import CODES, CodeEntry
from docchat_core.errors.reasons import REASONS

FIELD_PATH_MAX_LENGTH: Final = 256
MAX_FIELD_ERRORS: Final = 50


@dataclass(frozen=True, slots=True)
class FieldIssue:
    """One `errors[]` entry of a `422`: where the input failed and why, never the input value."""

    path: str
    reason: str

    def __post_init__(self) -> None:
        if not 1 <= len(self.path) <= FIELD_PATH_MAX_LENGTH:
            raise ValueError(f"a field path has 1 to {FIELD_PATH_MAX_LENGTH} characters")
        if self.reason not in REASONS:
            raise ValueError(f"reason {self.reason!r} isn't in REASONS")


def problem_entry(code: str) -> CodeEntry:
    """The catalogue entry of a problem code; refuses a code that can't be a problem."""
    entry = CODES.get(code)
    if entry is None:
        raise TypeError(f"code {code!r} isn't registered in CODES")
    if entry.status is None:
        raise TypeError(f"code {code!r} has no HTTP status and is never a problem")
    return entry


class DocChatProblem(Exception):  # noqa: N818 - the name 11-api.md section 1.3 gives the type
    """Base of every problem exception. A subclass sets `code`; everything else comes from
    the catalogue or from the keyword arguments of the raise site."""

    code: ClassVar[str]

    @override
    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        # An intermediate base may leave `code` unset; a subclass that sets one must name a
        # problem code, so a stream-only or stored-only code fails when the class is defined.
        if "code" in cls.__dict__:
            problem_entry(cls.__dict__["code"])

    def __init__(
        self,
        *,
        retry_after_seconds: int | None = None,
        errors: Sequence[FieldIssue] = (),
        session_id: UUID | None = None,
        turn_id: UUID | None = None,
        detail_values: Mapping[str, int] | None = None,
    ) -> None:
        code = getattr(type(self), "code", None)
        if code is None:
            raise TypeError(f"{type(self).__qualname__} names no problem code")
        entry = problem_entry(code)
        self._entry = entry
        self._retry_after_seconds = _retry_after(entry, retry_after_seconds)
        self._errors = _field_issues(entry, errors)
        self._session_id = _member(entry, "sessionId", session_id)
        self._turn_id = _member(entry, "turnId", turn_id)
        self._detail = entry.render_detail(detail_values)
        # The exception's own text is the code alone, so logging it can't leak input values.
        super().__init__(code)

    @property
    def entry(self) -> CodeEntry:
        return self._entry

    @property
    def status(self) -> int:
        # `problem_entry` refused every entry without a status.
        return cast("int", self._entry.status)

    @property
    def title(self) -> str:
        return self._entry.title

    @property
    def detail(self) -> str:
        return self._detail

    @property
    def retry_after_seconds(self) -> int | None:
        return self._retry_after_seconds

    @property
    def errors(self) -> tuple[FieldIssue, ...]:
        return self._errors

    @property
    def session_id(self) -> UUID | None:
        return self._session_id

    @property
    def turn_id(self) -> UUID | None:
        return self._turn_id


def _retry_after(entry: CodeEntry, value: int | None) -> int | None:
    # For a code with a status, `CodeEntry` declares a range exactly when the code's problem
    # carries `retryAfterSeconds`.
    if entry.retry_after is None:
        if value is not None:
            raise ValueError(f"{entry.code} carries no retryAfterSeconds")
        return None
    low, high = entry.retry_after
    if value is None:
        if low != high:
            raise ValueError(f"{entry.code} needs retry_after_seconds")
        return low
    if isinstance(value, bool) or not low <= value <= high:
        raise ValueError(f"{entry.code}: retry_after_seconds must lie within {low} to {high}")
    return value


def _field_issues(entry: CodeEntry, errors: Sequence[FieldIssue]) -> tuple[FieldIssue, ...]:
    if "errors" not in entry.extensions:
        if errors:
            raise ValueError(f"{entry.code} carries no errors")
        return ()
    if not errors:
        raise ValueError(f"{entry.code} needs at least one field error")
    # The body keeps the first 50 in the order the validator reported them (11-api.md 1.5).
    return tuple(errors[:MAX_FIELD_ERRORS])


def _member(entry: CodeEntry, member: str, value: UUID | None) -> UUID | None:
    declared = member in entry.extensions
    if declared and value is None:
        raise ValueError(f"{entry.code} needs {member}")
    if not declared and value is not None:
        raise ValueError(f"{entry.code} carries no {member}")
    return value
