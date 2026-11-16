# ADR 0006: Async end to end and stateless compute

- **Status:** Accepted
- **Date:** 2026-10-18
- **Decided by:** Product owner and architect
- **Specified in:** 00-overview.md, 01-repository.md, 11-api.md, 12-chat-pipeline.md, 13-conversations-and-memory.md, 14-ingestion.md, 81-config.md, 83-infrastructure.md, 84-testing.md, 85-observability.md
- **Related ADRs:** 0003, 0005, 0007, 0017, 0026, 0049, 0053, 0057, 0070, 0113, 0129, 0132, 0134, 0150

## Context

The product owner requires both Python apps, the API and the ingestion app, to be async end to end. A chat answer streams for up to three minutes while the API waits on AI Search, Azure OpenAI, Content Safety, Cosmos DB and SQL; an ingestion activity waits on Blob Storage, Document Intelligence and Azure OpenAI. One blocking call on an event loop stalls every stream that loop serves, and one thread held per stream caps an instance at its thread count. The API must carry 100 concurrent chat streams on one instance of the smallest production size (P1v3, 2 vCPU) with zero errors, while a cheap authenticated endpoint keeps p95 latency at or under 200 ms. Any instance must be able to serve any request, so that stopping one instance fails only the streams it held and autoscale can add or remove instances at any time.

Vendor facts bound the choice (checked 2026-10-14):

- Every Azure SDK this product uses ships an `.aio` client, and `openai` ships `AsyncOpenAI`; Prompt Shields has no GA SDK method, so it is called over REST.
- No GA async SQL Server driver exists: SQLAlchemy's `mssql+aioodbc` is a thread wrapper around pyodbc, aioodbc 0.5.0 is Development Status 3 - Alpha with no Python 3.13 classifier, and mssql-python is GA but synchronous.
- In Azure Functions, `async def` functions run on the Python worker's shared event loop and synchronous functions run in a thread pool; for CPU-bound apps Microsoft advises at least one worker process per core. Durable orchestrator functions must never be `async` and must do no I/O.
- `unstructured-inference` renders PDF pages under a module-level `threading.Lock`, so threads cannot parallelise page-wise parsing.

## Decision

1. **Every I/O call is an awaited async call.**

| Dependency | Client, always awaited |
|---|---|
| Azure SQL Database | `docchat_core.sql.engine`: SQLAlchemy 2.1 Core over pyodbc 5.3 and ODBC Driver 18, on the dedicated SQL thread pool of item 2 |
| Blob and Queue Storage | `azure.storage.blob.aio`, `azure.storage.queue.aio` |
| Cosmos DB | `azure.cosmos.aio.CosmosClient` |
| AI Search | `azure.search.documents.aio` `SearchClient` and `SearchIndexClient`, data-plane API 2026-04-01 |
| Azure OpenAI | `openai.AsyncOpenAI` against `{endpoint}/openai/v1/` |
| Content Safety Prompt Shields | `httpx.AsyncClient` `POST …/contentsafety/text:shieldPrompt?api-version=2024-09-01` |
| Document Intelligence | `azure.ai.documentintelligence.aio.DocumentIntelligenceClient`, API 2024-11-30 |
| Entra signing keys | `httpx.AsyncClient` (ADR 0017) |
| Managed identity tokens | `azure.identity.aio.ManagedIdentityCredential` |
| Child processes and CLI tools | `asyncio.create_subprocess_exec` |

2. **A blocking library runs on a bounded pool of its own.** SQL is the one such library: every SQL call runs on a `concurrent.futures.ThreadPoolExecutor` reserved for SQL (`DOCCHAT_SQL_THREADS`, default 8 in the API and 4 in ingestion, range 2 to 32, thread prefix `docchat-sql`), never on the loop and never on the default executor. A connection checkout waits at most `DOCCHAT_SQL_POOL_TIMEOUT_SECONDS` (default 10 s, range 1 to 60) and then fails as `SqlUnavailable` with reason `pool_timeout`. The Entra access token is awaited on the loop before work is dispatched, and retry delays are awaited on the loop (ADR 0026). Callers never hold a connection: the data-access layer checks one out per unit of work and returns it, rolled back when the work failed, on every exit path, so an error can never strand a connection. Alembic migrations and the SQL grant step run synchronously only because no event loop exists in those processes.
3. **CPU-bound work leaves the loop.** Every token count, the prompt assembly's and a turn's history count included, runs inline when its texts hold fewer than 20,000 characters together and in one `asyncio.to_thread` call at 20,000 characters and above; the chunker runs in `asyncio.to_thread` inside the `chunk_document` activity. Parsing, rendering and file inspection run in a fresh child process per job (ADR 0049), because the PDF renderer's process-wide lock serialises threads. Ingestion parallelism comes from worker processes and instances (`FUNCTIONS_WORKER_PROCESS_COUNT` 1, 2 and 4 on EP1, EP2 and EP3, ADR 0053), never from more threads.
4. **Clients are created once per process.** `docchat_core.clients.ClientRegistry` creates every Azure SDK client and the `AsyncOpenAI` client in each Gunicorn worker's FastAPI lifespan (after the fork) and in each Functions worker process at its first invocation, all sharing one `azure.identity.aio.ManagedIdentityCredential(client_id=DOCCHAT_CORE_IDENTITY_CLIENT_ID)`, and closes them at shutdown (ADR 0113). Outside Azure the same registry uses `azure.identity.aio.AzureCliCredential` only when `DOCCHAT_CORE_ENVIRONMENT` is `local`, and its emulator branch only when `DOCCHAT_LOCAL_EMULATORS` is `true`. AI Search is the one exception to "created at start-up", because physical index names are data: `search_client(physical_index_name)` creates a client on first use, keeps at most 16 per process in an LRU, and closes an evicted client with `await client.close()` once its in-flight requests finish. No module outside `docchat_core.clients` constructs an Azure SDK client or `AsyncOpenAI` (ADR 0150).
5. **Every await is bounded.** Every outbound client sets explicit connect and read timeouts and follows no redirects (values in ADR 0070 and ADR 0150), so no await waits without limit and no stream holds a worker indefinitely.
6. **Durable Functions code keeps its own rules.** Orchestrators are generator functions, never `async`, do no I/O and read time only from `context.current_utc_datetime`; activities and triggers are `async def`; activity code keeps no module-level mutable state.
7. **No instance keeps state between requests.** State that outlives a request lives only in shared stores: SQL (configuration, access, the ingestion ledger), Cosmos DB (conversations, memory, usage counters, rate windows and stream and download leases), Blob Storage (files and work files), AI Search (chunks) and the Durable task hub (orchestration state). Rate limits and concurrency leases are ETag-conditioned writes in the `usage` container, so every instance enforces the same limits. In-process memory holds only data whose loss changes no outcome: the ops-config snapshot and the index-record cache, each at most 60 s old (the index cache at most 256 entries); the signing-key set (24 h); the burst limiter's buckets and the last-seen write throttle; the per-worker post-turn re-check map (at most 10,000 users); the search-client LRU; and the worker's registry of its own running turn tasks and background tasks, which the shutdown drain ends (ADR 0007). Memberships, overrides, mappings and project status are never cached. Client affinity is off on both apps (`clientAffinityEnabled: false`).
8. **Tools enforce the rule.** (a) Ruff with every `ASYNC` rule as an error and no per-file ignore, and a banned-API list that rejects `requests`, `urllib.request`, `httpx.Client`, `openai.OpenAI`, `openai.AzureOpenAI`, `openai.AsyncAzureOpenAI` and the synchronous `azure.identity`, Storage, Cosmos DB, Search, Document Intelligence and Key Vault clients (ADR 0129); (b) mypy `unused-awaitable`; (c) every test runs with asyncio debug mode, warnings as errors, and the autouse guard `blocked_loop_guard` that fails a test in which one callback blocks the loop for more than 100 ms (ADR 0132); (d) import-linter contracts: `pyodbc` only in `docchat_core.sql.engine`, `azure.identity.aio` only in `docchat_core.clients`, `docchat_core.sql.engine`, `docchat_ops` and `docchat_testing`; (e) the AST check `scripts/ci/check_outbound_clients.py` (ADR 0150); (f) in production, histogram `docchat.runtime.event_loop_lag` sampled every `DOCCHAT_TELEMETRY_LOOP_LAG_INTERVAL_MS` (default 500 ms, range 100 to 5,000) in every API and ingestion worker, with alert A30; (g) load scenarios S1 (100 streams on one P1v3 instance with 2 workers, zero errors, cheap-endpoint p95 at most 200 ms) and S2 (autoscale moved from 3 to 2 instances mid-run, after which only the removed instance's requests may have failed) (ADR 0134).

## Alternatives considered

- **Synchronous handlers on a thread pool (FastAPI's threadpool for `def` routes, or a WSGI stack).** Lost: an open stream would hold a thread for up to the 180-second turn cap, so 100 streams per instance would need 100 threads per worker, and a blocking SDK call can't be cancelled when the client disconnects.
- **Synchronous SDK clients wrapped in `asyncio.to_thread`.** Lost: every in-flight call occupies a thread of the shared default executor, and cancelling the await leaves the blocking call running, so disconnects and deadlines can't stop work. Only SQL, which has no GA async driver, uses a thread pool, and that pool is its own and bounded.
- **An async SQL driver (`aioodbc`, `mssql+aioodbc`).** Lost: Alpha status, no Python 3.13 classifier, and itself a thread wrapper; the dedicated pool gives the same behaviour from GA components with a bound this product controls.
- **Clients created per request.** Lost: a TLS handshake, a connection pool and a token fetch per request, and a leaked connection whenever an error skips the close.
- **Threads for parsing parallelism.** Lost: the renderer's process-wide lock serialises them, and a crash in native parsing code would take the worker process down; child processes isolate memory and failures.
- **In-process session, counter or lease state with sticky sessions.** Lost: a restart or scale-in would drop limits and sessions, autoscale couldn't move load, and the requirement that any instance serves any request would fail.

## Consequences

- Easier: one P1v3 instance carries 100 concurrent streams on two event loops; a disconnect or deadline cancels the model and search calls in flight (ADR 0057, ADR 0070); any instance serves any request, so deploys and scale-in lose only the streams the stopped instance held.
- Harder: every new dependency needs an async client or a bounded offload decided in an ADR; synchronous SDK samples can't be used as written; every test runs on an event loop in debug mode.
- Obligations: the settings `DOCCHAT_SQL_THREADS`, `DOCCHAT_SQL_POOL_TIMEOUT_SECONDS` and `DOCCHAT_TELEMETRY_LOOP_LAG_INTERVAL_MS` (81-config.md); the loop guard's own tests (a 150 ms `time.sleep` inside a coroutine fails, 50 ms passes, `await asyncio.sleep(0.5)` passes); an integration test in which 200 concurrent `fetch_one` calls on 8 SQL threads never block the loop for more than 100 ms; an integration test in which cancelling the awaiting coroutine mid-transaction doesn't stop the unit of work, whose transaction commits and whose row exists; load scenarios S1 and S2; alert A30 on event-loop lag; CODEOWNERS routes `docchat_core/clients/` and `docchat_core/sql/engine.py` to `docchat-security`.
- Risk: a library that blocks inside an `async` API (a synchronous DNS lookup or a blocking callback) passes the static checks. The 100 ms guard catches it in tests, and the lag histogram and A30 catch it in production.
- Risk: SQL pool exhaustion shows as a pool timeout (`SqlUnavailable`, answered `503 dependency_unavailable`) rather than a blocked loop; the thread count is a setting, and the load test sizes it.
- Risk: in-process caches let ops-config values and index records lag the database by up to 60 s, the accepted bound for slow-changing configuration.

## References

- Python scale and performance in Azure Functions (async functions on the worker's event loop; worker processes for CPU-bound apps): https://learn.microsoft.com/en-us/azure/azure-functions/python-scale-performance-reference (checked 2026-10-14)
- Durable Functions bindings (orchestrators never `async`, no I/O): https://learn.microsoft.com/en-us/azure/azure-functions/durable/durable-functions-bindings (checked 2026-10-14)
- SQLAlchemy 2.1 SQL Server dialects (`mssql+aioodbc` is a thread wrapper around pyodbc): https://docs.sqlalchemy.org/en/21/dialects/mssql.html (checked 2026-10-14)
- aioodbc on PyPI (Alpha, classifiers up to Python 3.12): https://pypi.org/project/aioodbc/ (checked 2026-10-14)
- mssql-python asynchronous patterns (synchronous I/O only): https://learn.microsoft.com/en-us/sql/connect/python/mssql-python/asynchronous-patterns (checked 2026-10-14)
- unstructured-inference on PyPI (PDF rendering under a process-wide lock): https://pypi.org/project/unstructured-inference/ (checked 2026-10-14)
