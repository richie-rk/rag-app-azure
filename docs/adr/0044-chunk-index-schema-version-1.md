# ADR 0044: Chunk index schema version 1

- **Status:** Accepted
- **Date:** 2026-10-18
- **Decided by:** Architect
- **Specified in:** 14-ingestion.md, 12-chat-pipeline.md, 80-data-model.md, 11-api.md, 84-testing.md
- **Related ADRs:** 0027, 0045, 0046, 0047, 0048, 0061, 0100, 0152

## Context

The ingestion app creates every Azure AI Search index from a versioned schema defined in `packages/core`. A chunk's key is `{documentId}_{chunkSetId}_{ordinal}`, so a new build never overwrites the chunks being served. The schema needs filterable document, content version and chunk set IDs; searchable chunk text, title and heading path with a language analyzer chosen per index; the page number, chunk ordinal and source content type; and a vector whose dimensions come from the physical index record, searched with HNSW and cosine similarity and never returned. One semantic configuration covers title, heading path and chunk text. A change of embedding model, dimensions or schema is never applied to a live index; it builds a new physical index (ADR 0045).

Chat retrieval (ADR 0061) runs hybrid keyword and vector search with the semantic ranker over 50 candidates, keeps 8, and judges confidence by a reranker score of 2.0. Every vector the ingestion app pays for must be one that retrieval queries, and the language an index is built for must also be the language its queries are analysed in, because an English-only analyzer tokenises other languages with English word rules and stop words and ranks their passages low. Later features need room to grow: new fields for new content types, and chat restricted to one document, which needs a filterable document ID on every chunk.

Vendor facts (data-plane API 2026-04-01):

- HNSW takes `m` 4 to 10 (default 4), `efConstruction` 100 to 1,000 (default 400) and `efSearch` 100 to 1,000 (default 500); cosine is the metric to use with Azure OpenAI embeddings. Vector fields are searchable and can't be filterable, sortable or facetable; dimensions run 2 to 4,096. HNSW's memory overhead at `m` 4 is about 2% of a 768-dimension float vector (about 61 bytes) and grows in proportion to `m`, each link costing about 8 to 10 bytes.
- `stored` is settable only at creation and only on vector fields, and `stored: false` requires `retrievable: false`. A field's `analyzer` can be assigned only when the field is created; language analyzers are accepted only through `analyzer`, the default is `standard.lucene`, and Microsoft analyzers index 2 to 3 times slower than Lucene ones. Changing a field's name, type or attributes needs a rebuild; a semantic configuration can be added, changed or deleted in place.
- A semantic configuration names a `titleField`, ordered `prioritizedContentFields` (lower-priority fields are trimmed first) and a `rankingOrder` (`RerankerScore` or `BoostedRerankerScore`); the ranker reads up to 2,048 tokens per document (title 128, keywords 128, content the rest) and only the top 50 fused results.
- BM25 is the only similarity on services created after July 2020. A Basic partition holds 5 GB of vector index. An indexing request carries at most 16 MB or 1,000 documents. A document key is at most 1,024 characters of letters, digits, `-`, `_` and `=`, not starting with `_`.

## Decision

1. **One schema module.** `docchat_core.search.schema` defines `CHUNK_INDEX_SCHEMA_VERSION = 1`, `ChunkIndexFields`, `build_chunk_index(name, dimensions, analyzer) -> SearchIndex`, `SEMANTIC_CONFIGURATION_NAME` (`SEMANTIC_CONFIGURATION_NAME[1]` = `chunk-semantic-v1`) and `ANALYZER_ALLOWLIST`. The ingestion app creates each physical index with `SearchIndexClient.create_index()` (`azure-search-documents` 12.0.0, API 2026-04-01).
2. **One record of field names.** `ChunkIndexFields` is a frozen record (`@dataclass(frozen=True, slots=True)`) with the attributes `key`, `document_id`, `document_version_id`, `chunk_set_id`, `chunk_ordinal`, `title`, `heading_path`, `chunk_text`, `page_number`, `content_type` and `vector`. `ChunkIndexFields.for_version(schema_version: int)` returns the one record of that version and raises `KeyError` for any other. `build_chunk_index` (the fields, the semantic configuration and the vector profile assignment) and the chat search request (its `select` list, its vector field and every result field it reads) take field names only from the record for the physical index's `schema_version`; no other code holds a field-name literal, and a chat turn whose physical index has a schema version the record doesn't know ends with the stream error `retrieval_unavailable`.
3. **The eleven fields of version 1**, in this order (`sortable` and `facetable` are false on every field):

   | Field | Type | Key | Searchable | Filterable | Retrievable | Stored | Analyzer | Content |
   |---|---|---|---|---|---|---|---|---|
   | `id` | `Edm.String` | yes | no | yes | yes | yes | | `{documentId}_{chunkSetId}_{ordinal}`: lowercase hyphenated UUIDs and the 0-based ordinal, at most 79 characters |
   | `documentId` | `Edm.String` | | no | yes | yes | yes | | document UUID |
   | `documentVersionId` | `Edm.String` | | no | yes | yes | yes | | version UUID |
   | `chunkSetId` | `Edm.String` | | no | yes | yes | yes | | chunk set UUID |
   | `chunkOrdinal` | `Edm.Int32` | | no | no | yes | yes | | 0-based position in the chunk set |
   | `title` | `Edm.String` | | yes | no | yes | yes | the index's | the version's `source_name` without its extension, at most 300 characters |
   | `headingPath` | `Collection(Edm.String)` | | yes | no | yes | yes | the index's | 0 to 8 headings, outermost first, each at most 200 characters |
   | `chunkText` | `Edm.String` | | yes | no | yes | yes | the index's | the chunk text, exactly the embedding input |
   | `pageNumber` | `Edm.Int32` | | no | yes | yes | yes | | 1-based page of the version's PDF where the chunk's own text starts, set on every chunk in both methods; for a DOCX or PPTX, a page of its converted PDF (ADR 0152) |
   | `contentType` | `Edm.String` | | no | yes | yes | yes | | the version's media type |
   | `chunkVector` | `Collection(Edm.Single)` | | yes | no | no | no | | `dimensions` = the physical index's dimensions (256, 1,024 or 3,072); `vectorSearchProfile` = `chunk-vector-profile` |

4. **Vector search.** `vectorSearch.algorithms` = [`{"name": "chunk-hnsw", "kind": "hnsw", "hnswParameters": {"m": 8, "efConstruction": 640, "efSearch": 640, "metric": "cosine"}}`]; `vectorSearch.profiles` = [`{"name": "chunk-vector-profile", "algorithm": "chunk-hnsw"}`]; no `compressions` and no `vectorizers`, because the API embeds each query itself with the physical index's query deployment and dimensions. Each chunk's embedding input is exactly its `chunkText`, embedded with `dimensions` always sent.
5. **Semantic configuration.** `semantic` = `{"defaultConfiguration": "chunk-semantic-v1", "configurations": [{"name": "chunk-semantic-v1", "prioritizedFields": {"titleField": {"fieldName": "title"}, "prioritizedContentFields": [{"fieldName": "headingPath"}, {"fieldName": "chunkText"}], "prioritizedKeywordsFields": []}, "rankingOrder": "RerankerScore"}]}`. The short heading path comes first, so the ranker's trimming cuts chunk text, never the heading path; the low-confidence threshold (`retrieval.reranker_threshold`, default 2.0) is compared with the reranker score.
6. **Nothing else.** No scoring profiles, suggesters or CORS options; similarity is the service's BM25.
7. **Language analyzer.** `title`, `headingPath` and `chunkText` take the logical index's `language_analyzer` through the field `analyzer` property, chosen at index creation from `ANALYZER_ALLOWLIST` (value, label): `standard.lucene` (Any language, the default), `en.microsoft` (English), `en.lucene` (English, Lucene), `fr.microsoft` (French), `fr.lucene` (French, Lucene), `de.microsoft` (German), `de.lucene` (German, Lucene), `es.microsoft` (Spanish), `es.lucene` (Spanish, Lucene), `it.microsoft` (Italian), `it.lucene` (Italian, Lucene), `nl.microsoft` (Dutch), `nl.lucene` (Dutch, Lucene), `pt-Pt.microsoft` (Portuguese, Portugal), `pt-Br.microsoft` (Portuguese, Brazil), `ja.microsoft` (Japanese), `ko.microsoft` (Korean), `zh-Hans.microsoft` (Chinese, Simplified), `ar.microsoft` (Arabic), `hi.microsoft` (Hindi). `adminGetIndexOptions` lists them in this order; any other value is `422 unsupported_analyzer`. The field analyzer also analyses the query text, so indexing and retrieval use one language, and the search request sends no query language or speller.
8. **Filters.** OData filter text comes only from `docchat_core.search.filters`: `FilterField` is the enum of the schema's filterable fields (`documentId`, `documentVersionId`, `chunkSetId`, `pageNumber`, `contentType`), so field names never come from input; `eq(field, value)` takes a `UUID`, an `int` or a `ContentType` member; `any_of(field, values)` takes 1 to 100 `UUID` values and renders `search.in(<field>, '<v1>,<v2>', ',')`; `all_of(*filters)` joins with ` and `. Every string literal is quoted with embedded single quotes doubled, UUIDs included, and a unit test fails when any `filter=` argument in the code base comes from elsewhere.
9. **Changing the schema.** A new schema ships as a new `CHUNK_INDEX_SCHEMA_VERSION`, a new `ChunkIndexFields.for_version` record and semantic configuration name, and a migration that marks every logical index in `ready` or `reingest_required` whose active physical index has an older schema version `reingest_required` (an index in `provisioning` serves nothing yet; one in `reingesting` or `rebuilding` reaches its status through `compute_index_status` when its run ends; a `failed` one keeps `failed`; `deleting` and `deleted` are left out; 80-data-model.md section 2.24); the next re-ingest builds a new physical index (ADR 0045). No schema change is applied to an index in place, a semantic-configuration change included although the service would accept one, so a physical index's `schema_version` always names its exact definition. Version 1 is the first version, so no index exists under any other field names.

## Alternatives considered

- **The service's HNSW defaults** (`m` 4, `efConstruction` 400, `efSearch` 500). Rejected: `m` 8 gives each vector more graph links for higher recall and adds about 60 bytes per vector, under 2% of a 1,024-dimension float vector's 4 KiB; `efConstruction` 640 builds a better graph and costs only indexing time, which runs offline; `efSearch` 640 stays far above the 50 neighbours a query asks for while latency stays low at this template's scale (under 1 million vectors per index).
- **Exhaustive k-nearest-neighbour search.** Rejected: it takes no vector quota, but every query scans every vector, so latency grows with the index, while HNSW keeps it flat at this scale.
- **Scalar or binary quantisation.** Rejected for now: at 1,024 dimensions a Basic partition's 5 GB vector quota holds about 1.2 million chunks, and quantisation would add rescoring settings with no present need.
- **An integrated vectorizer**, the search service embedding queries. Rejected: the API already embeds each query with the physical index's own query deployment (ADR 0061), and a vectorizer would need the search service to reach Azure OpenAI with its own identity and network path.
- **Stored or retrievable vectors.** Rejected: nothing reads vectors back, and `stored: false` saves their storage; it can be set only at creation, so version 1 sets it.
- **One analyzer for every index.** Rejected: indexes hold documents in different languages, and an English-only analyzer ranks other languages' passages low.
- **Free-text analyzer names.** Rejected: a closed allowlist lets the admin screen offer a picker and the API refuse a bad value with a clear code before the service rejects the index.
- **`chunkText` before `headingPath`.** Rejected: a long chunk would fill the ranker's content budget and push the heading path out.
- **Field-name literals at each call site.** Rejected: the index builder and the search request could drift apart, and the service refuses a `select` or vector query that names a field the index lacks.

## Consequences

- The index builder and the search request can't disagree on a field name, because both read one record per schema version; every chunk carries a filterable document ID, which keeps chat restricted to one document possible later.
- Any change to a field, an attribute, an HNSW value or the semantic configuration is a new schema version and a rebuild of every logical index (ADR 0045), so such changes are rare and deliberate.
- `standard.lucene` works for any language but doesn't stem; an index holding one language gets better recall from that language's analyzer, chosen at creation, and changing it later is a rebuild. Microsoft analyzers make indexing 2 to 3 times slower.
- Capacity: a 1,024-dimension float vector takes 4 KiB, so a Basic partition's 5 GB holds about 1.2 million chunks; 3,072 dimensions hold about a third of that, which the admin screen's dimension choice carries.
- The reranker threshold depends on the ranker's score distribution, which Microsoft says shifts slightly with model updates, so it lives in `ops_config` (`retrieval.reranker_threshold`, 0.0 to 4.0), where operators can move it.
- Tests (84-testing.md): the generated index JSON snapshotted per analyzer and per dimension (256, 1,024, 3,072), with the HNSW values, the semantic configuration's field order and `chunkVector` neither retrievable nor stored, and `ChunkIndexFields.for_version(1)` matching the generated `fields[].name` in order while `for_version(2)` raises `KeyError`; the filter builder doubling quotes and refusing 101 IDs; the code-base check on `filter=`; the embedding input equal to `chunkText` with `dimensions` always sent.

## References

- Create or update index, field attributes and semantic settings (API 2026-04-01): https://learn.microsoft.com/en-us/rest/api/searchservice/indexes/create-or-update?view=rest-searchservice-2026-04-01 (checked 2026-10-14)
- HNSW parameters, metrics and vector field rules: https://learn.microsoft.com/en-us/azure/search/vector-search-how-to-create-index (checked 2026-10-14)
- HNSW memory overhead and vector index size: https://learn.microsoft.com/en-us/azure/search/vector-search-index-size (checked 2026-10-18)
- Changes that need a rebuild: https://learn.microsoft.com/en-us/azure/search/search-howto-reindex (checked 2026-10-18)
- Language analyzers: https://learn.microsoft.com/en-us/azure/search/index-add-language-analyzers (checked 2026-10-14)
- Semantic ranker inputs and scores: https://learn.microsoft.com/en-us/azure/search/semantic-search-overview (checked 2026-10-14)
- Hybrid queries with semantic ranking: https://learn.microsoft.com/en-us/azure/search/hybrid-search-how-to-query (checked 2026-10-14)
- BM25 similarity: https://learn.microsoft.com/en-us/azure/search/index-similarity-and-scoring (checked 2026-10-14)
- Limits and vector quotas: https://learn.microsoft.com/en-us/azure/search/search-limits-quotas-capacity (checked 2026-10-14)
- Naming rules (document keys): https://learn.microsoft.com/en-us/rest/api/searchservice/naming-rules (checked 2026-10-14)
- azure-search-documents 12.0.0: https://pypi.org/project/azure-search-documents/ (checked 2026-10-14)
