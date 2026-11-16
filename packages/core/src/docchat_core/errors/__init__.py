"""The `CODES` and `REASONS` registries, the ingestion item codes and the problem exception base
(`11-api.md` section 2; `14-ingestion.md` section 17; ADR 0012)."""

from docchat_core.errors.codes import (
    CODE_MAX_LENGTH,
    CODE_PATTERN,
    CODES,
    DETAIL_MAX_LENGTH,
    PROBLEM_TYPE_PREFIX,
    RETRY_AFTER_MAX,
    STREAM_ONLY_CODES,
    TITLE_MAX_LENGTH,
    CodeArea,
    CodeEntry,
    ProblemMember,
)
from docchat_core.errors.items import (
    ITEM_CODES,
    ITEM_MESSAGE_MAX_LENGTH,
    ItemCode,
    ItemCodeKind,
)
from docchat_core.errors.problems import (
    FIELD_PATH_MAX_LENGTH,
    MAX_FIELD_ERRORS,
    DocChatProblem,
    FieldIssue,
    problem_entry,
)
from docchat_core.errors.reasons import REASONS, ReasonEntry

__all__ = [
    "CODES",
    "CODE_MAX_LENGTH",
    "CODE_PATTERN",
    "DETAIL_MAX_LENGTH",
    "FIELD_PATH_MAX_LENGTH",
    "ITEM_CODES",
    "ITEM_MESSAGE_MAX_LENGTH",
    "MAX_FIELD_ERRORS",
    "PROBLEM_TYPE_PREFIX",
    "REASONS",
    "RETRY_AFTER_MAX",
    "STREAM_ONLY_CODES",
    "TITLE_MAX_LENGTH",
    "CodeArea",
    "CodeEntry",
    "DocChatProblem",
    "FieldIssue",
    "ItemCode",
    "ItemCodeKind",
    "ProblemMember",
    "ReasonEntry",
    "problem_entry",
]
