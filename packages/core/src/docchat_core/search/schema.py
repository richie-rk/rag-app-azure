"""The chunk index schema, version 1 (`80-data-model.md` sections 6.3 to 6.7; `14-ingestion.md`
section 8.13; ADR 0044).

Field names come only from `ChunkIndexFields.for_version(schema_version)`: the index builder takes
them from the record of `CHUNK_INDEX_SCHEMA_VERSION`, and Index and the chat search request from
the record of the physical index's own `schema_version`, so the three read one definition and no
other module holds a field-name literal. A schema change ships as a new version constant, a new
record and a new semantic configuration name, never in place (section 6.9).
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import ClassVar, Final, NamedTuple
from uuid import UUID

from azure.search.documents.indexes.models import (
    HnswAlgorithmConfiguration,
    HnswParameters,
    RankingOrder,
    SearchField,
    SearchFieldDataType,
    SearchIndex,
    SemanticConfiguration,
    SemanticField,
    SemanticPrioritizedFields,
    SemanticSearch,
    VectorSearch,
    VectorSearchAlgorithmMetric,
    VectorSearchProfile,
)

from docchat_core.ingestion.names import validate_physical_index_name
from docchat_core.ingestion.settings import EMBEDDING_DIMENSIONS

CHUNK_INDEX_SCHEMA_VERSION: Final = 1
"""The schema version every new physical index is built with (`physical_indexes.schema_version`)."""


@dataclass(frozen=True, slots=True)
class ChunkIndexFields:
    """One schema version's field names (section 6.6)."""

    key: str
    document_id: str
    document_version_id: str
    chunk_set_id: str
    chunk_ordinal: str
    title: str
    heading_path: str
    chunk_text: str
    page_number: str
    content_type: str
    vector: str

    _BY_VERSION: ClassVar[Mapping[int, ChunkIndexFields]]

    @classmethod
    def for_version(cls, schema_version: int) -> ChunkIndexFields:
        """The one immutable record of `schema_version`; `KeyError` for a version it doesn't
        know (a chat turn then ends with `retrieval_unavailable`)."""
        return cls._BY_VERSION[schema_version]


ChunkIndexFields._BY_VERSION = MappingProxyType(
    {
        1: ChunkIndexFields(
            key="id",
            document_id="documentId",
            document_version_id="documentVersionId",
            chunk_set_id="chunkSetId",
            chunk_ordinal="chunkOrdinal",
            title="title",
            heading_path="headingPath",
            chunk_text="chunkText",
            page_number="pageNumber",
            content_type="contentType",
            vector="chunkVector",
        )
    }
)

SEMANTIC_CONFIGURATION_NAME: Final[Mapping[int, str]] = MappingProxyType({1: "chunk-semantic-v1"})
"""The semantic configuration name of each schema version (section 6.4 item 5)."""

VECTOR_ALGORITHM_NAME: Final = "chunk-hnsw"
VECTOR_PROFILE_NAME: Final = "chunk-vector-profile"
HNSW_M: Final = 8
HNSW_EF_CONSTRUCTION: Final = 640
HNSW_EF_SEARCH: Final = 640


class AnalyzerOption(NamedTuple):
    """One language analyzer a logical index can choose, with the label the admin form shows."""

    value: str
    label: str


ANALYZER_ALLOWLIST: Final = (
    AnalyzerOption("standard.lucene", "Any language"),
    AnalyzerOption("en.microsoft", "English"),
    AnalyzerOption("en.lucene", "English, Lucene"),
    AnalyzerOption("fr.microsoft", "French"),
    AnalyzerOption("fr.lucene", "French, Lucene"),
    AnalyzerOption("de.microsoft", "German"),
    AnalyzerOption("de.lucene", "German, Lucene"),
    AnalyzerOption("es.microsoft", "Spanish"),
    AnalyzerOption("es.lucene", "Spanish, Lucene"),
    AnalyzerOption("it.microsoft", "Italian"),
    AnalyzerOption("it.lucene", "Italian, Lucene"),
    AnalyzerOption("nl.microsoft", "Dutch"),
    AnalyzerOption("nl.lucene", "Dutch, Lucene"),
    AnalyzerOption("pt-Pt.microsoft", "Portuguese, Portugal"),
    AnalyzerOption("pt-Br.microsoft", "Portuguese, Brazil"),
    AnalyzerOption("ja.microsoft", "Japanese"),
    AnalyzerOption("ko.microsoft", "Korean"),
    AnalyzerOption("zh-Hans.microsoft", "Chinese, Simplified"),
    AnalyzerOption("ar.microsoft", "Arabic"),
    AnalyzerOption("hi.microsoft", "Hindi"),
)
"""The analyzers of `title`, `headingPath` and `chunkText`, in the order the admin form lists
them; any other value is `422 unsupported_analyzer` (section 6.5)."""

DEFAULT_ANALYZER: Final = "standard.lucene"

ANALYZERS: Final = frozenset(option.value for option in ANALYZER_ALLOWLIST)

MAX_HEADINGS: Final = 8
CHUNK_KEY_MAX_LENGTH: Final = 79

_STRING: Final = SearchFieldDataType.STRING
_INT32: Final = SearchFieldDataType.INT32
_STRINGS: Final = "Collection(Edm.String)"
_SINGLES: Final = "Collection(Edm.Single)"


def _simple(name: str, data_type: str, *, filterable: bool, key: bool = False) -> SearchField:
    return SearchField(
        name=name,
        type=data_type,
        key=key,
        searchable=False,
        filterable=filterable,
        retrievable=True,
        stored=True,
        sortable=False,
        facetable=False,
    )


def _text(name: str, data_type: str, analyzer: str) -> SearchField:
    return SearchField(
        name=name,
        type=data_type,
        key=False,
        searchable=True,
        filterable=False,
        retrievable=True,
        stored=True,
        sortable=False,
        facetable=False,
        analyzer_name=analyzer,
    )


def build_chunk_index(name: str, dimensions: int, analyzer: str) -> SearchIndex:
    """The definition of schema version 1 for one physical index: exactly the eleven fields of
    section 6.3 in order, the HNSW profile and the semantic configuration of section 6.4, and
    nothing else (no compressions, vectorizers, scoring profiles, suggesters or CORS options)."""
    validate_physical_index_name(name)
    if type(dimensions) is not int or dimensions not in EMBEDDING_DIMENSIONS:
        raise ValueError("dimensions are 256, 1024 or 3072")
    if analyzer not in ANALYZERS:
        raise ValueError("not an allowlisted analyzer")
    fields = ChunkIndexFields.for_version(CHUNK_INDEX_SCHEMA_VERSION)
    semantic_name = SEMANTIC_CONFIGURATION_NAME[CHUNK_INDEX_SCHEMA_VERSION]
    return SearchIndex(
        name=name,
        fields=[
            _simple(fields.key, _STRING, filterable=True, key=True),
            _simple(fields.document_id, _STRING, filterable=True),
            _simple(fields.document_version_id, _STRING, filterable=True),
            _simple(fields.chunk_set_id, _STRING, filterable=True),
            _simple(fields.chunk_ordinal, _INT32, filterable=False),
            _text(fields.title, _STRING, analyzer),
            _text(fields.heading_path, _STRINGS, analyzer),
            _text(fields.chunk_text, _STRING, analyzer),
            _simple(fields.page_number, _INT32, filterable=True),
            _simple(fields.content_type, _STRING, filterable=True),
            SearchField(
                name=fields.vector,
                type=_SINGLES,
                key=False,
                searchable=True,
                filterable=False,
                retrievable=False,
                stored=False,
                sortable=False,
                facetable=False,
                vector_search_dimensions=dimensions,
                vector_search_profile_name=VECTOR_PROFILE_NAME,
            ),
        ],
        vector_search=VectorSearch(
            algorithms=[
                HnswAlgorithmConfiguration(
                    name=VECTOR_ALGORITHM_NAME,
                    parameters=HnswParameters(
                        m=HNSW_M,
                        ef_construction=HNSW_EF_CONSTRUCTION,
                        ef_search=HNSW_EF_SEARCH,
                        metric=VectorSearchAlgorithmMetric.COSINE,
                    ),
                )
            ],
            profiles=[
                VectorSearchProfile(
                    name=VECTOR_PROFILE_NAME, algorithm_configuration_name=VECTOR_ALGORITHM_NAME
                )
            ],
        ),
        semantic_search=SemanticSearch(
            default_configuration_name=semantic_name,
            configurations=[
                SemanticConfiguration(
                    name=semantic_name,
                    prioritized_fields=SemanticPrioritizedFields(
                        title_field=SemanticField(field_name=fields.title),
                        # The short heading path comes first, so the ranker's trimming cuts chunk
                        # text, never the heading path.
                        content_fields=[
                            SemanticField(field_name=fields.heading_path),
                            SemanticField(field_name=fields.chunk_text),
                        ],
                        keywords_fields=[],
                    ),
                    ranking_order=RankingOrder.RERANKER_SCORE,
                )
            ],
        ),
    )


# --- Chunk keys -----------------------------------------------------------------------------------

_KEY_RE: Final = re.compile(r"^[A-Za-z0-9-][A-Za-z0-9_-]{0,78}$")


def chunk_key(document_id: UUID, chunk_set_id: UUID, ordinal: int) -> str:
    """`{documentId}_{chunkSetId}_{ordinal}`: lower-case hyphenated UUIDs and the 0-based
    ordinal, at most 79 characters of the key alphabet and never starting with `_`. A new build
    never overwrites served chunks, because the key carries the chunk set ID (section 6.7)."""
    if not all(isinstance(value, UUID) for value in (document_id, chunk_set_id)):
        raise TypeError("a chunk key is built from UUIDs")
    if type(ordinal) is not int or ordinal < 0:
        raise ValueError("an ordinal is an integer from 0")
    key = f"{document_id}_{chunk_set_id}_{ordinal}"
    if not _KEY_RE.fullmatch(key):
        raise ValueError("a chunk key holds at most 79 key characters")
    return key


def chunk_keys(document_id: UUID, chunk_set_id: UUID, chunk_count: int) -> Iterator[str]:
    """Every key of a chunk set, ordinals 0 to `chunk_count` - 1, so its chunks are deleted by
    key with no search query (section 6.7 item 2)."""
    if type(chunk_count) is not int or chunk_count < 0:
        raise ValueError("a chunk count is an integer from 0")
    return (chunk_key(document_id, chunk_set_id, ordinal) for ordinal in range(chunk_count))
