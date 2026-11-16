"""The problems of the index, document and run operations (`11-api.md` sections 2.2, 3.10 to
3.12; `14-ingestion.md` section 17.1) and their translation from the ledger's refusals.

The ledger functions refuse with `IngestionRefusedError(code)` and the index repository with
`IndexSettingsRefusedError(code)`; each operation translates only the codes its section lists,
so an unlisted refusal reaches the failure boundary as a defect (`500 internal_error`) instead of
an undocumented answer. Every body comes from the code alone (one title, one `detail`), so a
foreign identifier and a random one answer byte-identical bodies apart from `correlationId`.

The catalogue refusals carry the `errors[]` entry their section names: `body.methodKey` with
`not_found` or `not_allowed`; `body.embeddingModel`, `body.embeddingDimensions` and
`body.languageAnalyzer` with `not_allowed`; one entry per failing method parameter at
`body.methodParameters.<name>` with the parameter model's reason.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping
from typing import Any, Final, NoReturn

from docchat_api.http.preconditions import PreconditionFailed
from docchat_api.http.problems import reason_for
from docchat_core.errors import DocChatProblem, FieldIssue
from docchat_core.ingestion.models import IngestionRefusedError
from docchat_core.sql.engine import SqlUniqueViolation
from docchat_core.sql.repositories.indexes import IndexSettingsRefusedError

DISPLAY_NAME_INDEX: Final = "UX_search_indexes_display_name"
PARAMETERS_PATH: Final = "body.methodParameters"


class IndexNotFound(DocChatProblem):
    """`404 index_not_found`: the logical index is missing or `deleted`."""

    code = "index_not_found"


class DocumentNotFound(DocChatProblem):
    """`404 document_not_found`: no live document with this ID in this index."""

    code = "document_not_found"


class RunNotFound(DocChatProblem):
    """`404 run_not_found`."""

    code = "run_not_found"


class DisplayNameTaken(DocChatProblem):
    """`409 display_name_taken`: another index that isn't deleted has this name."""

    code = "display_name_taken"


class IndexBusy(DocChatProblem):
    """`409 index_busy`: the index's status doesn't allow the change."""

    code = "index_busy"


class IndexMapped(DocChatProblem):
    """`409 index_mapped`: a project maps the index."""

    code = "index_mapped"


class SearchIndexCapacity(DocChatProblem):
    """`409 search_index_capacity`: no room for another physical index."""

    code = "search_index_capacity"


class NoCurrentVersion(DocChatProblem):
    """`409 no_current_version`: the document has no ingested version to reprocess."""

    code = "no_current_version"


class MethodNotFound(DocChatProblem):
    code = "method_not_found"


class MethodInactive(DocChatProblem):
    code = "method_inactive"


class InvalidMethodParameters(DocChatProblem):
    code = "invalid_method_parameters"


class UnsupportedEmbeddingModel(DocChatProblem):
    code = "unsupported_embedding_model"


class UnsupportedDimensions(DocChatProblem):
    code = "unsupported_dimensions"


class UnsupportedAnalyzer(DocChatProblem):
    code = "unsupported_analyzer"


class EmptyUpdate(DocChatProblem):
    """`422 empty_update`: an `IndexUpdate` without a field."""

    code = "empty_update"


_REFUSALS: Final[Mapping[str, type[DocChatProblem]]] = {
    "index_not_found": IndexNotFound,
    "document_not_found": DocumentNotFound,
    "precondition_failed": PreconditionFailed,
    "index_busy": IndexBusy,
    "index_mapped": IndexMapped,
    "search_index_capacity": SearchIndexCapacity,
    "no_current_version": NoCurrentVersion,
}

_SETTINGS: Final[Mapping[str, tuple[type[DocChatProblem], str, str]]] = {
    "method_not_found": (MethodNotFound, "body.methodKey", "not_found"),
    "method_inactive": (MethodInactive, "body.methodKey", "not_allowed"),
    "unsupported_embedding_model": (
        UnsupportedEmbeddingModel,
        "body.embeddingModel",
        "not_allowed",
    ),
    "unsupported_dimensions": (UnsupportedDimensions, "body.embeddingDimensions", "not_allowed"),
    "unsupported_analyzer": (UnsupportedAnalyzer, "body.languageAnalyzer", "not_allowed"),
}

# A step outside a range of allowed values (`overlapPercent` in steps of 5) is a value not
# allowed, one of the reasons the parameter refusal names; section 1.7's mapping would make it
# `invalid_format`.
_PARAMETER_REASONS: Final[Mapping[str, str]] = {"multiple_of": "not_allowed"}


def raise_refusal(refusal: IngestionRefusedError, answers: Collection[str]) -> NoReturn:
    """Raise the problem of a ledger refusal when its code is one of `answers` (the operation's
    own codes); otherwise re-raise the refusal, which the failure boundary answers as a defect."""
    problem = _REFUSALS.get(refusal.code)
    if problem is None or refusal.code not in answers:
        raise refusal
    raise problem() from None


def parameter_issues(errors: Iterable[Mapping[str, Any]]) -> list[FieldIssue]:
    """One entry per failing parameter, at `body.methodParameters.<name>`, in the model's order;
    an error without a parameter name is at `body.methodParameters`."""
    issues: dict[str, FieldIssue] = {}
    for error in errors:
        location = error.get("loc", ())
        name = str(location[0]) if isinstance(location, tuple | list) and location else None
        path = PARAMETERS_PATH if name is None else f"{PARAMETERS_PATH}.{name}"
        reason = _PARAMETER_REASONS.get(str(error.get("type", ""))) or reason_for(error)
        issues.setdefault(path, FieldIssue(path, reason))
    return list(issues.values()) or [FieldIssue(PARAMETERS_PATH, "invalid_format")]


def settings_problem(refusal: IndexSettingsRefusedError) -> DocChatProblem:
    """The `422` of a catalogue refusal, with its `errors[]` entries."""
    if refusal.code == "invalid_method_parameters":
        return InvalidMethodParameters(errors=parameter_issues(refusal.parameter_errors))
    problem, path, reason = _SETTINGS[refusal.code]
    return problem(errors=[FieldIssue(path, reason)])


def is_display_name_taken(violation: SqlUniqueViolation) -> bool:
    """Whether a unique violation is the display name's filtered unique index."""
    return violation.constraint == DISPLAY_NAME_INDEX
