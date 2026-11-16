# ADR 0061: Hybrid semantic retrieval with low-confidence broadening

- **Status:** Accepted
- **Date:** 2026-10-18
- **Decided by:** Product owner and architect
- **Specified in:** 12-chat-pipeline.md, 11-api.md, 80-data-model.md, 81-config.md, 83-infrastructure.md, 84-testing.md, 85-observability.md, 86-operations.md
- **Related ADRs:** 0020, 0028, 0040, 0044, 0045, 0055, 0059, 0062, 0063, 0068, 0070, 0101, 0113, 0147, 0150

## Context

Retrieval decides what an answer can be grounded in. The product needs:

- hybrid search by default (keyword and vector, fused by the search service) plus semantic reranking, against the active physical index of the index resolved for the caller and project (ADR 0020), with the query embedded by that physical index's query-embedding deployment at its dimensions, because the query vector must match the indexed vectors;
- 50 candidates and 8 kept sources, both operational settings;
- only each document's served chunk set reaching the prompt, filtered before the cut, so 8 valid results remain whenever 8 exist and a document update or delete is atomic for readers;
- a broader search only when confidence is low: fewer than a minimum number of kept results (starting at 1) reaching a reranker score of 2.0 on the service's 0-to-4 scale. The chat-small model then writes a hypothetical answer passage (HyDE), and one more hybrid request (the standalone query as text, plus a query vector and a passage vector) replaces the first results. The user sees "Searching more broadly for better matches" and never the technique's name; if this step fails, the turn continues with the first results;
- a standalone search query from the chat-small model, because a follow-up question often depends on earlier turns ("and the second option?"); a session's first turn uses the question as written, and so does a turn whose standalone query fails;
- nothing language-specific in code or in the request, because each deployment serves its own languages and an index's language is chosen when the index is created (ADR 0044);
- every client created once per process and closed at shutdown, although physical index names are data that a rebuild changes (ADR 0045).

Vendor facts that bound the choice:

- Only the top 50 fused results reach the semantic ranker, so with semantic ranking each vector query's `k` is 50; `top` sets how many results return.
- `@search.rerankerScore` runs from 4 (highly relevant) to 0 (irrelevant), 2 meaning a partial answer; its distribution shifts slightly with infrastructure and model updates, so thresholds must stay coarse. The ranker uses multilingual models.
- Semantic ranking is throttled per search unit (Basic 2 concurrent requests with a queue of 4, S1 3 and 6). With `semanticErrorHandling` `partial`, a partial response (reason `maxWaitExceeded`, `capacityOverloaded` or `transient`) can return base results without reranker scores, and the azure-search-documents 12.0.0 async pager exposes no partial-response reason. `semanticMaxWaitInMilliseconds` has a minimum of 700.
- Several vector queries in one request are fused with RRF, each with a `weight` (default 1.0, greater than 0), and always return `k` neighbours; `sessionId` gives more consistent scoring across replicas.
- Data-plane API 2026-04-01 is the latest GA version and azure-search-documents 12.0.0's default; its request properties include no query language or speller. Semantic queries are billed when `queryType` is `semantic` and the search string isn't empty.
- Strict JSON-schema outputs can't carry `maxLength`, so length bounds on a model's structured output are checked after the call.

## Decision

**Search query.** The turn searches with the standalone query from the chat-small call `rewrite` when it is accepted, and otherwise with the question as written. The call is skipped (`rewriteOutcome` `skipped`) when the session has no rolling summary and `read_recent_turns` returned no turn. Inputs: the rolling summary, the last `chat.rewrite_history_turns` (default 4) of the returned turns with each question and answer cut to its first 300 tokens, and the question. Output: strict JSON `{"query": string}` (schema `standalone_query`), accepted (`rewritten`) when it holds 1 to 1,200 characters after trimming and contains neither `[[BEGIN` nor `[[END`. A rejected output, a timeout of `chat.rewrite_timeout_seconds` (default 8 s), a filtered call or an error uses the question as written (`fallback`); there is no retry. The trace stores `rewriteOutcome`, `standaloneQuery` (null when no standalone query was accepted) and `searchQuery` (the text actually searched).

**Query vector.** The search query is embedded with the active physical index's `query_embedding_deployment`, passing `dimensions` = its `embedding_dimensions`, one input per call, timeout 10 s, 2 attempts (retried on a connection error, `408`, `429` with a retry delay of at most 5 s, and `5xx`). A failure ends the turn with the `error` event `retrieval_unavailable`.

**First pass.** One `SearchClient.search` call (POST `/indexes('{physicalIndexName}')/docs/search.post.search`, API 2026-04-01) with exactly:

- `search_text` = the search query; `query_type="semantic"`; `semantic_configuration_name` = `SEMANTIC_CONFIGURATION_NAME[schema_version]` (`chunk-semantic-v1` for schema version 1); `semantic_error_mode="partial"`; `semantic_max_wait_in_milliseconds` = `retrieval.semantic_max_wait_ms` (default 2,000);
- `vector_queries` = `[VectorizedQuery(vector=<query vector>, k_nearest_neighbors=retrieval.candidate_count, fields=F.vector, exhaustive=False)]`; `top` = `retrieval.candidate_count` (default 50);
- `select` = `F.key`, `F.document_id`, `F.document_version_id`, `F.chunk_set_id`, `F.chunk_ordinal`, `F.title`, `F.heading_path`, `F.page_number`, `F.content_type`, `F.chunk_text` (schema version 1: `id`, `documentId`, `documentVersionId`, `chunkSetId`, `chunkOrdinal`, `title`, `headingPath`, `pageNumber`, `contentType`, `chunkText`; never the vector field `chunkVector`, which isn't retrievable);
- `session_id` = the chat session ID; no `filter`, `search_fields`, captions, answers, count, facets, debug, query language or speller.

`F` is `docchat_core.search.schema.ChunkIndexFields.for_version(schema_version)` for the schema version in the authorisation module's `ResolvedIndex` (ADR 0020), and every field the pipeline reads from a result goes through the same `F` (the chunk key and the document, version and chunk set IDs for the filter, the trace and the stored citation; title, heading path and chunk text for the source block; page number and content type for the citation), so chat code holds no index field-name literal. A schema version that `for_version` doesn't know ends the turn with `retrieval_unavailable` before any search call. Keyword matching uses the analyzer the index's text fields were created with (the logical index's `language_analyzer`, default `standard.lucene`, ADR 0044), which also analyses the query. Timeout `DOCCHAT_SEARCH_TIMEOUT_SECONDS` (default 10 s); the client's retry policy allows 2 retries with exponential backoff from 0.5 s to at most 4 s; a failure ends the turn with `retrieval_unavailable`. Results arrive ordered by the reranker score (`@search.reranker_score` in the SDK's results, `@search.rerankerScore` on the wire), highest first.

**Partial semantic response.** A response in which no result carries a non-null `@search.reranker_score` is partial: the turn keeps the service's fused order, records `rerankPartial = true` and `broadenOutcome = "skipped_partial"`, skips the confidence test, never broadens and increments `docchat.search.rerank_partial`.

**Served chunk sets before the cut.** After each pass, one SQL query reads the served chunk set of each distinct document among the results (at most 50 IDs, bound as an expanding parameter list), index-backed by the primary keys of `served_chunk_sets` (`document_id`, `physical_index_id`) and `documents` (`id`):

```sql
SELECT s.document_id, s.chunk_set_id, s.document_version_id, d.source_name
FROM app.served_chunk_sets AS s
JOIN app.documents AS d ON d.id = s.document_id
WHERE s.physical_index_id = :physical_index_id
  AND s.document_id IN :document_ids
  AND d.is_deleted = 0
```

A result survives only when its chunk set ID equals its document's served chunk set. The first `retrieval.keep_count` (default 8) survivors, in result order, are kept and ranked from 1. A failure of this query after the first pass ends the turn with `retrieval_unavailable`.

**Low confidence.** Confidence is low when fewer than `retrieval.min_confident_results` (default 1) kept results have `@search.reranker_score` ≥ `retrieval.reranker_threshold` (default 2.0).

**Broadened search.** When confidence is low and the response wasn't partial, the API:

1. writes `{"type":"status","status":"search_broadened"}` (the only `status` value; the technique is never named to the client);
2. calls the chat-small deployment (call `search_passage`) with only the search query, in a `SEARCH_QUERY` block, for a passage of 60 to 120 words of plain prose in the query's language that reads like a document excerpt answering it (no greeting, no mention of the query, no headings, lists or links; general wording rather than invented names, figures or dates), under the strict JSON schema `search_passage` (`{"passage": string}`), accepted when it holds 1 to 1,200 characters after trimming; `max_completion_tokens` = `chat.small_call_max_completion_tokens` (default 800); reasoning effort `DOCCHAT_OPENAI_SMALL_REASONING_EFFORT` (default `none`); whole-call timeout `retrieval.broaden_timeout_seconds` (default 8 s); no retry;
3. embeds the passage with the same deployment and dimensions (call `passage_embedding`, with the query vector's timeout and attempts);
4. runs one more search identical to the first pass except that `vector_queries` holds two `VectorizedQuery` items, the query vector and the passage vector, each on `F.vector` with `k_nearest_neighbors` = `retrieval.candidate_count` and weight 1.0.

Its results pass the same served-chunk-set filter and cut, and replace the first set whatever their scores (`broadenOutcome = "replaced"`, `broadened = true`). If any of steps 2 to 4 or the second filter query fails, the first set stands (`broadenOutcome = "failed"`); when confidence isn't low the trace records `not_needed`. The trace also records `broadenAttempted` and `searchPassage`; the passage is never shown to anyone. A turn broadens at most once.

**Search clients.** `docchat_core.clients.search_client(physical_index_name)` returns the process's `azure.search.documents.aio.SearchClient` for that index, created on first use with the process's one credential (token scope `https://search.azure.com/.default`), `api_version="2026-04-01"`, `connection_timeout=10` and `read_timeout` = `DOCCHAT_SEARCH_TIMEOUT_SECONDS`. At most 16 clients stay in an LRU keyed by physical index name; each counts its requests in flight; an evicted client is closed with `await client.close()` once they finish; every cached client is closed in the lifespan shutdown. The API identity holds Search Index Data Reader (`1407120a-92aa-4202-b7e9-c0e197c71c8f`) on the search service, whose semantic ranker runs on the `standard` plan in every environment, because the `free` plan returns billing errors once its monthly allowance is spent, which would stop retrieval.

**Settings.** ops_config rows typed by `RetrievalOps` and `ChatOps` (ADR 0028); a turn uses the snapshot it started with: `retrieval.candidate_count` 50 (10 to 50); `retrieval.keep_count` 8 (1 to 16, at most `retrieval.candidate_count`); `retrieval.reranker_threshold` 2.0 (0.0 to 4.0); `retrieval.min_confident_results` 1 (1 to `retrieval.keep_count`); `retrieval.semantic_max_wait_ms` 2,000 (700 to 10,000; the service's minimum is 700); `retrieval.broaden_timeout_seconds` 8 (2 to 20); `chat.rewrite_history_turns` 4 (1 to 8); `chat.rewrite_timeout_seconds` 8 (2 to 20); `chat.small_call_max_completion_tokens` 800 (200 to 8,000). App settings: `DOCCHAT_SEARCH_ENDPOINT`; `DOCCHAT_SEARCH_TIMEOUT_SECONDS` 10 (2 to 30).

**Records.** The trace's `retrieval` object holds `searchIndexId`, `physicalIndexId`, `physicalIndexName`, `indexSource`, `queryEmbeddingDeployment`, `embeddingModel`, `embeddingDimensions`, `candidateCount`, `keepCount`, `rerankerThreshold`, `minConfidentResults`, `rerankPartial`, `broadenAttempted`, `broadened`, `broadenOutcome`, `searchPassage`, `firstPass` and `broadenedPass` (per result: `chunkKey`, `documentId`, `chunkSetId`, `searchScore`, `rerankerScore`, `served`) and `kept` (per kept result: `rank`, `sourceId`, `chunkKey`, `documentId`, `documentVersionId`, `chunkSetId`, `rerankerScore`, `outcome`). Each pass adds 1 to `usage.billable.semanticQueries` (ADR 0059). Telemetry, metadata only: spans `docchat.chat.rewrite`, `docchat.chat.search_passage`, `docchat.chat.embed` (`docchat.pass` `first` or `broadened`), `docchat.chat.search` and `docchat.chat.served_filter`; metrics `docchat.chat.broadened_searches` (`outcome` `replaced` or `failed`), `docchat.search.duration` (`pass`) and `docchat.search.rerank_partial`; the `broadened` attribute on `docchat.chat.time_to_first_token`.

## Alternatives considered

- **Vector-only or keyword-only search.** Lost: keyword search misses paraphrases and vector search misses exact names, codes and numbers. Hybrid is the product's default, and the service fuses both, so the API runs no fusion code.
- **Hybrid without the semantic ranker.** Lost: fused RRF scores mean nothing across queries, while the reranker's 0-to-4 score gives the low-confidence rule a calibrated signal.
- **HyDE on every turn.** Lost: the product uses it only when confidence is low, because on every turn it would add a chat-small call, an embedding and a second ranker query before the first answer token.
- **Merging the broadened results with the first set.** Lost: the product replaces the set; scores from two requests can't be compared without fusion code, and the second request already carries the query text and the query vector.
- **Broadening after a partial response.** Lost: a partial response comes from a throttled or slow ranker, so a second query would add load exactly when the service is saturated.
- **Filtering chunk sets in the search request** (`search.in` over served chunk set IDs, or a served flag stored in the index). Lost: the first needs every served set of the index before each query; the second needs a second write at every flip that can't commit with the SQL flip. Checking at most 50 candidates against SQL keeps one source of truth and needs no search filter.
- **The service's integrated vectorizer** (`VectorizableTextQuery`). Lost: the search service has no identity and its indexes no vectorizer, so it can't call Azure OpenAI, and the API already resolves the query-embedding deployment from the physical index record.
- **The ranker's generated query variants and the hybrid `threshold` option.** Lost: both are preview features (the threshold only in 2026-08-01-preview), outside the GA API this product pins, and the ranker's variants see only the query text, never the conversation that the standalone query must resolve.
- **A client per request, or one per index created at start-up.** Lost: a client per request opens a connection pool per turn; start-up creation can't know indexes a later rebuild creates; an unbounded map keeps every retired index's pool open.
- **A query language or speller in the request, or a language stored beside the index for the query builder.** Lost: language belongs to the index and is set when its fields are created (ADR 0044); a request setting could disagree with the index it targets, the GA request offers none, and the multilingual ranker needs no hint.

## Consequences

- One deterministic request per pass, built from the physical index's schema version: a schema change ships as a new version and a new physical index, and a repoint takes effect on the next turn without a restart.
- Updates and deletes are atomic for readers: a flip of a `served_chunk_sets` row (ADR 0040) or a document's `is_deleted` flag applies to the next search. While stale chunk sets remain in the index between a flip and clean-up, fewer than 8 results can survive.
- Cost and latency: each pass is one billable semantic query; a broadened turn adds a chat-small call charged to the user's quota (its allowance counts in the chat-small capacity share, ADR 0147), an embedding and a second semantic query, and delays the first answer token by the passage call (at most 8 s), one embedding and the second search.
- The `status` event is written before the passage call, so a broadening that then fails still shows the broader-search status while the answer uses the first results; the trace records `failed`.
- Tests: the field names and `select` list of both passes for schema version 1, and an unknown version ending with `retrieval_unavailable` and no search call; 12 served results among 50 keep exactly 8 in order, and a deleted document's chunks never reach the prompt; low confidence writes `search_broadened`, the second request carries two vector queries and its results replace the first; a failed passage call keeps the first set with `failed`; a partial response never broadens; the standalone-query call is skipped on a first turn and a 9-second delay falls back; 17 index names leave 16 open clients, the least recently used closing after its in-flight request and the rest at shutdown; a per-user override sends retrieval to its index, and a member of project A never receives chunks of an index mapped only to project B.
- Monitoring: the broadened-search rate (`docchat.chat.broadened_searches` ÷ `docchat.chat.turns`) and time to first token with and without broadening on the chat dashboard; alert when time-to-first-token p95 stays above 20 s for 15 minutes. The region preflight fails with `semantic_ranker_unavailable` where the ranker isn't offered (ADR 0101).
- Runbook (86-operations.md): once evaluation results exist, tune `retrieval.reranker_threshold` and `retrieval.min_confident_results` with `docchat-ops config set`, then watch the broadened-search rate and the time to first token.
- Risks: reranker distributions drift with service model updates, which the ops settings absorb; under heavy load partial responses lower ranking quality (`docchat.search.rerank_partial` counts them); the broadened set replaces the first even when its scores are lower, and the trace keeps both passes for evaluation; with more than 16 indexes in use at once, an evicted index's next query pays one new connection.

## References

- Hybrid query, `k` 50 with semantic ranking: https://learn.microsoft.com/en-us/azure/search/hybrid-search-how-to-query (checked 2026-10-14)
- Semantic ranking (top 50, scores 4 to 0, multilingual models, coarse thresholds, query variants in preview): https://learn.microsoft.com/en-us/azure/search/semantic-search-overview (ms.date 2026-08-05, checked 2026-10-18)
- Multiple vector queries and weights: https://learn.microsoft.com/en-us/azure/search/vector-search-how-to-query (checked 2026-10-14)
- Search Documents, API 2026-04-01 (`semanticErrorHandling`, `semanticMaxWaitInMilliseconds` minimum 700, `sessionId`, partial-response fields, the request property list): https://learn.microsoft.com/en-us/rest/api/searchservice/documents/search-post?view=rest-searchservice-2026-04-01 (checked 2026-10-14)
- Semantic ranker throttling per search unit: https://learn.microsoft.com/en-us/azure/search/search-limits-quotas-capacity (checked 2026-10-14)
- Scoring consistency and `sessionId`: https://learn.microsoft.com/en-us/azure/search/index-similarity-and-scoring (checked 2026-10-14)
- Semantic ranker billing plans: https://learn.microsoft.com/en-us/azure/search/semantic-how-to-enable-disable (checked 2026-10-14)
- Async `SearchClient`: https://learn.microsoft.com/en-us/python/api/azure-search-documents/azure.search.documents.aio.searchclient?view=azure-python; azure-search-documents 12.0.0: https://pypi.org/project/azure-search-documents/ (checked 2026-10-14)
- Language analyzers: https://learn.microsoft.com/en-us/azure/search/index-add-language-analyzers (checked 2026-10-14)
- Search data-plane roles: https://learn.microsoft.com/en-us/azure/search/search-security-rbac (checked 2026-10-14)
- Structured outputs (strict schemas; `maxLength` unsupported): https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/structured-outputs (ms.date 2026-08-24, checked 2026-10-18)
