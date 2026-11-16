"""User memory's shared rules (`13-conversations-and-memory.md` sections 7.2, 7.9 and 7.11).

The fact categories with their labels and order (`categories`), and the fact normalisation, the
fact validators and the suggested-question test (`validators`). The conversation store, the
memory routes and the extraction live in the API; the document models in
`docchat_core.cosmos.memories`.
"""

from docchat_core.memory.categories import CATEGORY_LABELS, CATEGORY_ORDER, FactCategory
from docchat_core.memory.validators import (
    FACT_MAX_CODE_POINTS,
    FactTextRejection,
    comparison_form,
    duplicate_key,
    is_suggested_question,
    stored_form,
    suggestion_form,
    validate_fact_text,
)

__all__ = [
    "CATEGORY_LABELS",
    "CATEGORY_ORDER",
    "FACT_MAX_CODE_POINTS",
    "FactCategory",
    "FactTextRejection",
    "comparison_form",
    "duplicate_key",
    "is_suggested_question",
    "stored_form",
    "suggestion_form",
    "validate_fact_text",
]
