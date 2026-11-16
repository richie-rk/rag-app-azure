"""Unit tests of the chunk index schema (T-104, T-107; `80-data-model.md` sections 6.3 to 6.7;
`14-ingestion.md` section 8.13; ADR 0044)."""

from __future__ import annotations

import ast
import copy
import json
import re
from collections.abc import Callable, Iterator
from dataclasses import FrozenInstanceError, astuple
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from docchat_core.search.schema import (
    ANALYZER_ALLOWLIST,
    ANALYZERS,
    CHUNK_INDEX_SCHEMA_VERSION,
    CHUNK_KEY_MAX_LENGTH,
    DEFAULT_ANALYZER,
    SEMANTIC_CONFIGURATION_NAME,
    ChunkIndexFields,
    build_chunk_index,
    chunk_key,
    chunk_keys,
)

type SpecTable = Callable[[str, str], list[list[str]]]

NAME = "docchat-3f2c0d9e8b7a41c6a5d4e3f2c1b0a998-g2"
SNAPSHOT = Path(__file__).with_name("snapshots") / "chunk_index_v1.json"
DOCUMENT = UUID("9b2e4c1a-7d3f-4e8b-a6c5-0f1e2d3c4b5a")
CHUNK_SET = UUID("0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d")
ANALYZED = {"title", "headingPath", "chunkText"}


def _snapshot(dimensions: int, analyzer: str) -> dict[str, Any]:
    # The snapshot is the definition for 1,024 dimensions and the default analyzer; only those
    # two values differ between the 60 definitions.
    expected: dict[str, Any] = copy.deepcopy(json.loads(SNAPSHOT.read_text(encoding="utf-8")))
    for field in expected["fields"]:
        if field["name"] in ANALYZED:
            field["analyzer"] = analyzer
        if field["name"] == "chunkVector":
            field["dimensions"] = dimensions
    return expected


@pytest.mark.parametrize("dimensions", [256, 1024, 3072])
@pytest.mark.parametrize("analyzer", [option.value for option in ANALYZER_ALLOWLIST])
def test_t104_every_definition_equals_its_snapshot(dimensions: int, analyzer: str) -> None:
    built = build_chunk_index(NAME, dimensions, analyzer).as_dict()
    assert built == _snapshot(dimensions, analyzer)


def _section_6_3_rows() -> list[list[str]]:
    # Read here rather than with `spec_table`, because the table's empty cells must stay cells.
    text = _data_model_text().split("### 6.3 Chunk index schema version 1", 1)[1]
    lines = [line for line in text.split("###", 1)[0].splitlines() if line.startswith("| `")]
    return [
        [cell.strip().strip("`") for cell in line.strip().strip("|").split("|")] for line in lines
    ]


def _data_model_text() -> str:
    return next(
        parent / "docs" / "spec" / "80-data-model.md"
        for parent in Path(__file__).resolve().parents
        if (parent / "docs" / "spec" / "80-data-model.md").is_file()
    ).read_text(encoding="utf-8")


def test_t104_the_snapshot_holds_the_eleven_fields_of_section_6_3() -> None:
    rows = _section_6_3_rows()
    assert len(rows) == 11
    fields = json.loads(SNAPSHOT.read_text(encoding="utf-8"))["fields"]
    assert [field["name"] for field in fields] == [row[0] for row in rows]
    yes = {"yes": True, "no": False, "": False}
    for field, (
        name,
        data_type,
        key,
        searchable,
        filterable,
        retrievable,
        stored,
        analyzer,
        _,
    ) in zip(fields, rows, strict=True):
        assert field["type"] == data_type, name
        assert field["key"] is yes[key], name
        assert field["searchable"] is yes[searchable], name
        assert field["filterable"] is yes[filterable], name
        assert field["retrievable"] is yes[retrievable], name
        assert field["stored"] is yes[stored], name
        assert field["sortable"] is False
        assert field["facetable"] is False
        assert ("analyzer" in field) == (analyzer == "the index's"), name


def test_t104_the_vector_field_is_neither_retrievable_nor_stored() -> None:
    vector = build_chunk_index(NAME, 3072, "en.microsoft").as_dict()["fields"][-1]
    assert vector["name"] == "chunkVector"
    assert vector["retrievable"] is False
    assert vector["stored"] is False
    assert vector["dimensions"] == 3072
    assert vector["vectorSearchProfile"] == "chunk-vector-profile"


def test_t104_vector_and_semantic_settings_of_section_6_4() -> None:
    built = build_chunk_index(NAME, 256, DEFAULT_ANALYZER).as_dict()
    assert built["vectorSearch"] == {
        "algorithms": [
            {
                "name": "chunk-hnsw",
                "kind": "hnsw",
                "hnswParameters": {
                    "m": 8,
                    "efConstruction": 640,
                    "efSearch": 640,
                    "metric": "cosine",
                },
            }
        ],
        "profiles": [{"name": "chunk-vector-profile", "algorithm": "chunk-hnsw"}],
    }
    assert built["semantic"] == {
        "defaultConfiguration": "chunk-semantic-v1",
        "configurations": [
            {
                "name": "chunk-semantic-v1",
                "prioritizedFields": {
                    "titleField": {"fieldName": "title"},
                    "prioritizedContentFields": [
                        {"fieldName": "headingPath"},
                        {"fieldName": "chunkText"},
                    ],
                    "prioritizedKeywordsFields": [],
                },
                "rankingOrder": "RerankerScore",
            }
        ],
    }
    assert set(built) == {"name", "fields", "vectorSearch", "semantic"}


@pytest.mark.parametrize(
    ("name", "dimensions", "analyzer"),
    [
        ("chunks", 1024, DEFAULT_ANALYZER),
        (NAME, 512, DEFAULT_ANALYZER),
        (NAME, 1024.0, DEFAULT_ANALYZER),
        (NAME, 1024, "fr.lucene.extra"),
        (NAME, 1024, "pt-BR.microsoft"),
    ],
)
def test_build_chunk_index_refuses_values_outside_the_schema(
    name: str, dimensions: Any, analyzer: str
) -> None:
    with pytest.raises(ValueError, match=r"name|dimensions|analyzer"):
        build_chunk_index(name, dimensions, analyzer)


def test_the_analyzer_allowlist_is_section_6_5_in_order() -> None:
    paragraph = _data_model_text().split("### 6.5 Language analyzers", 1)[1].split("###", 1)[0]
    listed = re.findall(r"`([A-Za-z.-]+)` \(([^)]+)\)", paragraph.split("in this order:", 1)[1])
    expected = [(value, label.removesuffix(", the default")) for value, label in listed]
    assert [tuple(option) for option in ANALYZER_ALLOWLIST] == expected
    assert len(ANALYZER_ALLOWLIST) == 20
    assert ANALYZER_ALLOWLIST[0].value == DEFAULT_ANALYZER
    assert frozenset(value for value, _ in expected) == ANALYZERS


# --- Field names (T-107) --------------------------------------------------------------------------


def test_t107_version_1_field_names() -> None:
    fields = ChunkIndexFields.for_version(1)
    assert astuple(fields) == (
        "id",
        "documentId",
        "documentVersionId",
        "chunkSetId",
        "chunkOrdinal",
        "title",
        "headingPath",
        "chunkText",
        "pageNumber",
        "contentType",
        "chunkVector",
    )
    assert ChunkIndexFields.for_version(CHUNK_INDEX_SCHEMA_VERSION) is fields
    assert SEMANTIC_CONFIGURATION_NAME[1] == "chunk-semantic-v1"
    with pytest.raises(FrozenInstanceError):
        fields.title = "other"  # type: ignore[misc]


def test_t107_an_unknown_version_raises_key_error() -> None:
    with pytest.raises(KeyError):
        ChunkIndexFields.for_version(2)


def test_t107_the_index_fields_come_from_the_record() -> None:
    names = [
        field["name"]
        for field in build_chunk_index(NAME, 1024, DEFAULT_ANALYZER).as_dict()["fields"]
    ]
    assert names == list(astuple(ChunkIndexFields.for_version(1)))


def _source_files() -> Iterator[Path]:
    root = next(
        parent for parent in Path(__file__).resolve().parents if (parent / "packages").is_dir()
    )
    for member in ("packages/core/src", "apps/api/src", "apps/ingestion/src", "scripts/ops/src"):
        yield from sorted((root / member).rglob("*.py"))


def test_t107_no_field_name_literal_outside_the_schema_module() -> None:
    # `id` and `title` are ordinary words used everywhere; the other nine names are the schema's.
    distinctive = set(astuple(ChunkIndexFields.for_version(1))) - {"id", "title"}
    found = [
        f"{path.name}:{node.lineno}: {node.value}"
        for path in _source_files()
        if not path.as_posix().endswith("docchat_core/search/schema.py")
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.value in distinctive
    ]
    assert found == []


# --- Chunk keys -----------------------------------------------------------------------------------


def test_chunk_keys_are_ids_and_the_ordinal() -> None:
    key = chunk_key(DOCUMENT, CHUNK_SET, 0)
    assert key == f"{DOCUMENT}_{CHUNK_SET}_0"
    longest = chunk_key(DOCUMENT, CHUNK_SET, 99_999)
    assert len(longest) == CHUNK_KEY_MAX_LENGTH == 79
    assert re.fullmatch(r"[A-Za-z0-9-][A-Za-z0-9_-]*", longest)
    assert list(chunk_keys(DOCUMENT, CHUNK_SET, 3)) == [
        f"{DOCUMENT}_{CHUNK_SET}_0",
        f"{DOCUMENT}_{CHUNK_SET}_1",
        f"{DOCUMENT}_{CHUNK_SET}_2",
    ]
    assert list(chunk_keys(DOCUMENT, CHUNK_SET, 0)) == []


@pytest.mark.parametrize(
    ("document", "chunk_set", "ordinal", "error"),
    [
        (str(DOCUMENT), CHUNK_SET, 0, TypeError),
        (DOCUMENT, CHUNK_SET.hex, 0, TypeError),
        (DOCUMENT, CHUNK_SET, -1, ValueError),
        (DOCUMENT, CHUNK_SET, True, ValueError),
        (DOCUMENT, CHUNK_SET, 100_000, ValueError),
    ],
)
def test_chunk_key_refusals(
    document: Any, chunk_set: Any, ordinal: Any, error: type[Exception]
) -> None:
    with pytest.raises(error):
        chunk_key(document, chunk_set, ordinal)


def test_chunk_keys_need_a_count() -> None:
    with pytest.raises(ValueError, match="count"):
        chunk_keys(DOCUMENT, CHUNK_SET, -1)
