"""Unit tests of the OData filter builder (T-108, T-109; `80-data-model.md` section 6.8;
ADR 0044 item 8)."""

from __future__ import annotations

import ast
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from docchat_core.ingestion.models import ContentType
from docchat_core.search.filters import (
    ANY_OF_MAX_VALUES,
    FilterField,
    ODataFilter,
    all_of,
    any_of,
    eq,
    quote,
)
from docchat_core.search.schema import ChunkIndexFields

FIRST = UUID("9b2e4c1a-7d3f-4e8b-a6c5-0f1e2d3c4b5a")
SECOND = UUID("0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d")


def test_filter_fields_are_the_filterable_fields_read_from_the_record() -> None:
    fields = ChunkIndexFields.for_version(1)
    assert [field.value for field in FilterField] == [
        fields.document_id,
        fields.document_version_id,
        fields.chunk_set_id,
        fields.page_number,
        fields.content_type,
    ]


# --- T-108 ----------------------------------------------------------------------------------------


def test_t108_every_embedded_quote_is_doubled() -> None:
    assert quote("o'brien") == "'o''brien'"
    assert quote("''") == "''''''"
    assert quote("") == "''"


def test_t108_eq_renders_typed_values() -> None:
    assert eq(FilterField.DOCUMENT_ID, FIRST).text == f"documentId eq '{FIRST}'"
    assert eq(FilterField.PAGE_NUMBER, 7).text == "pageNumber eq 7"
    assert eq(FilterField.CONTENT_TYPE, ContentType.PDF).text == "contentType eq 'application/pdf'"
    assert str(eq(FilterField.CHUNK_SET_ID, SECOND)) == f"chunkSetId eq '{SECOND}'"


def test_t108_any_of_renders_search_in() -> None:
    assert any_of(FilterField.CHUNK_SET_ID, [FIRST, SECOND]).text == (
        f"search.in(chunkSetId, '{FIRST},{SECOND}', ',')"
    )
    hundred = any_of(FilterField.DOCUMENT_ID, [FIRST] * ANY_OF_MAX_VALUES).text
    assert hundred.count(str(FIRST)) == 100


@pytest.mark.parametrize("count", [0, 101])
def test_t108_any_of_refuses_0_and_101_values(count: int) -> None:
    with pytest.raises(ValueError, match="1 to 100"):
        any_of(FilterField.DOCUMENT_ID, [FIRST] * count)


def test_all_of_joins_with_and() -> None:
    combined = all_of(any_of(FilterField.CHUNK_SET_ID, [FIRST]), eq(FilterField.PAGE_NUMBER, 2))
    assert combined.text == f"search.in(chunkSetId, '{FIRST}', ',') and pageNumber eq 2"
    single = eq(FilterField.PAGE_NUMBER, 1)
    assert all_of(single) == single
    with pytest.raises(ValueError, match="at least one"):
        all_of()
    with pytest.raises(TypeError):
        all_of("pageNumber eq 1")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        (FilterField.DOCUMENT_ID, str(FIRST)),
        (FilterField.DOCUMENT_ID, 3),
        (FilterField.PAGE_NUMBER, "3"),
        (FilterField.PAGE_NUMBER, True),
        (FilterField.PAGE_NUMBER, FIRST),
        (FilterField.CONTENT_TYPE, "application/pdf"),
        (FilterField.CONTENT_TYPE, FIRST),
    ],
)
def test_eq_refuses_a_value_of_another_type(field: FilterField, value: Any) -> None:
    with pytest.raises(TypeError):
        eq(field, value)


def test_any_of_takes_uuids_of_an_id_field() -> None:
    with pytest.raises(TypeError):
        any_of(FilterField.PAGE_NUMBER, [FIRST])
    with pytest.raises(TypeError):
        any_of(FilterField.DOCUMENT_ID, [str(FIRST)])  # type: ignore[list-item]


def test_a_field_name_never_comes_from_input() -> None:
    with pytest.raises(ValueError, match="FilterField"):
        eq("documentId eq '' or true", FIRST)  # type: ignore[arg-type]


# --- T-109 ----------------------------------------------------------------------------------------

FILTER_BUILDERS = frozenset({"eq", "any_of", "all_of"})


def _source_files() -> Iterator[Path]:
    root = next(
        parent for parent in Path(__file__).resolve().parents if (parent / "packages").is_dir()
    )
    for member in ("packages/core/src", "apps/api/src", "apps/ingestion/src", "scripts/ops/src"):
        yield from sorted((root / member).rglob("*.py"))


def _builder_call(node: ast.expr) -> bool:
    if not isinstance(node, ast.Call):
        return False
    function = node.func
    name = function.attr if isinstance(function, ast.Attribute) else getattr(function, "id", None)
    return name in FILTER_BUILDERS


def filter_violations(source: str) -> list[int]:
    """The lines of `filter=` arguments not built by the filter builder: the value must be a
    builder call, `str()` or `.text` of one, or a name assigned only from builder calls."""
    tree = ast.parse(source)
    built_names: dict[str, bool] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign | ast.AnnAssign) and node.value is not None:
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name):
                    ok = _builder_call(node.value)
                    built_names[target.id] = built_names.get(target.id, True) and ok

    def allowed(value: ast.expr) -> bool:
        if isinstance(value, ast.Constant) and value.value is None:
            return True
        if _builder_call(value):
            return True
        if isinstance(value, ast.Name):
            return built_names.get(value.id, False)
        if isinstance(value, ast.Attribute) and value.attr == "text":
            return allowed(value.value)
        if (
            isinstance(value, ast.Call)
            and isinstance(value.func, ast.Name)
            and value.func.id == "str"
            and len(value.args) == 1
        ):
            return allowed(value.args[0])
        return False

    return [
        keyword.value.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        for keyword in node.keywords
        if keyword.arg == "filter" and not allowed(keyword.value)
    ]


def test_t109_every_filter_argument_comes_from_the_builder() -> None:
    found = [
        f"{path.name}:{line}"
        for path in _source_files()
        for line in filter_violations(path.read_text(encoding="utf-8"))
    ]
    assert found == []


def test_t109_the_scan_finds_hand_written_filters() -> None:
    source = """
from docchat_core.search import filters
built = filters.all_of(filters.eq(field, value))
mixed = filters.eq(field, value)
mixed = "x"
client.search(filter=f"documentId eq '{value}'")
client.search(filter="pageNumber eq 1")
client.search(filter="a" + b)
client.search(filter=mixed)
client.search(filter=unknown)
client.search(filter=str(built))
client.search(filter=built.text)
client.search(filter=filters.any_of(field, values).text)
client.search(filter=eq(field, value))
client.search(filter=None)
"""
    assert filter_violations(source) == [6, 7, 8, 9, 10]


def test_an_odata_filter_is_its_text() -> None:
    assert str(ODataFilter("pageNumber eq 1")) == "pageNumber eq 1"
