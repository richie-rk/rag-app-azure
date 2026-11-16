from __future__ import annotations

from collections.abc import Callable

import pytest

from docchat_core.errors.codes import CODES
from docchat_core.errors.items import (
    ITEM_CODES,
    ITEM_MESSAGE_MAX_LENGTH,
    ItemCode,
    ItemCodeKind,
    _catalogue,
)

type SpecTable = Callable[[str, str], list[list[str]]]

_KINDS = {
    "failure": ItemCodeKind.FAILURE,
    "failure after retries": ItemCodeKind.FAILURE_AFTER_RETRIES,
    "skip": ItemCodeKind.SKIP,
    "skip or cancel": ItemCodeKind.SKIP_OR_CANCEL,
    "warning": ItemCodeKind.WARNING,
}


def _retried(cell: str) -> bool | None:
    if cell == "n/a":
        return None
    return cell.startswith("yes")


def test_item_codes_match_14_ingestion_section_17_2(spec_table: SpecTable) -> None:
    rows = spec_table("14-ingestion.md", "### 17.2 Ingestion item codes")

    assert [row[0] for row in rows] == list(ITEM_CODES)
    for code, kind, _when, message, retried in rows:
        entry = ITEM_CODES[code]
        assert entry.kind is _KINDS[kind], code
        assert entry.message == message, code
        assert entry.retried is _retried(retried), code


def test_catalogue_holds_57_item_codes() -> None:
    assert len(ITEM_CODES) == 57


def test_messages_fit_the_api_field() -> None:
    assert all(len(entry.message) <= ITEM_MESSAGE_MAX_LENGTH for entry in ITEM_CODES.values())


def test_codes_shared_with_problems_keep_their_own_item_messages() -> None:
    shared = set(ITEM_CODES) & set(CODES)

    assert shared == {
        "file_signature_mismatch",
        "search_index_capacity",
        "storage_unavailable",
        "internal_error",
    }
    assert ITEM_CODES["storage_unavailable"].message != CODES["storage_unavailable"].detail


@pytest.mark.parametrize(
    ("code", "kind", "message", "retried"),
    [
        ("Bad", ItemCodeKind.FAILURE, "A message.", False),
        ("x" * 65, ItemCodeKind.FAILURE, "A message.", False),
        ("fine", ItemCodeKind.FAILURE, "", False),
        ("fine", ItemCodeKind.FAILURE, "x" * 401, False),
        ("fine", ItemCodeKind.FAILURE, "A message.", None),
        ("fine", ItemCodeKind.WARNING, "A message.", False),
    ],
)
def test_item_code_refuses_an_invalid_entry(
    code: str, kind: ItemCodeKind, message: str, retried: bool | None
) -> None:
    with pytest.raises(ValueError, match=r"item code|fine"):
        ItemCode(code, kind, message, retried=retried)


def test_catalogue_refuses_a_code_registered_twice() -> None:
    entry = ItemCode("fine", ItemCodeKind.SKIP, "A message.", retried=None)

    with pytest.raises(ValueError, match="registered twice"):
        _catalogue((entry, entry))
