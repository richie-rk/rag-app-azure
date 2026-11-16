"""The fact categories, their labels and order (`13-conversations-and-memory.md` section 7.2),
read against the table of the specification itself."""

from __future__ import annotations

from collections.abc import Callable

from docchat_core.cosmos.memories import Fact
from docchat_core.memory.categories import CATEGORY_LABELS, CATEGORY_ORDER, FactCategory

type SpecTable = Callable[[str, str], list[list[str]]]

HEADING = "### 7.2 Categories, caps and no eviction"


def test_the_categories_labels_and_order_equal_the_specification(spec_table: SpecTable) -> None:
    rows = spec_table("13-conversations-and-memory.md", HEADING)
    assert [row[0] for row in rows] == [category.value for category in CATEGORY_ORDER]
    assert {row[0]: row[2] for row in rows} == {
        category.value: label for category, label in CATEGORY_LABELS.items()
    }


def test_the_set_is_closed_and_ordered() -> None:
    assert CATEGORY_ORDER == (
        FactCategory.ROLE,
        FactCategory.TEAM,
        FactCategory.EXPERTISE,
        FactCategory.TOPIC,
        FactCategory.ANSWER_STYLE,
    )
    assert set(CATEGORY_LABELS) == set(FactCategory)


def test_the_document_model_uses_these_categories() -> None:
    assert Fact.model_fields["category"].annotation is FactCategory
