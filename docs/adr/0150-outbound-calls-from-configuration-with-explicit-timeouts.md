# ADR 0150: Outbound calls from configuration with explicit timeouts

- **Status:** Accepted
- **Date:** 2026-10-21
- **Decided by:** Architect
- **Specified in:** 82-security.md, 01-repository.md, 81-config.md, 84-testing.md
- **Related ADRs:** 0006, 0009, 0062, 0068, 0070, 0073, 0113, 0129, 0130, 0135, 0144

## Context

Both apps and the operator tools call Azure services, Microsoft Entra ID and a few public APIs, async end to end, with clients created once per process, shared and closed at shutdown. Two failure classes follow from a careless outbound call: a target taken from data is a server-side request forgery path (a caller who controls a host, port or URL makes the server fetch it with the server's network position and sometimes its token), and a call without a timeout can hold a worker, a stream, an activity slot or a SQL-pool thread indefinitely. No operation of this product needs to fetch a URL a caller supplies, and every wait on a dependency must end in bounded time.

Vendor facts: azure-core's transports default to `connection_timeout` 300 s and `read_timeout` 300 s, and its redirect policy (`permit_redirects`) defaults to on; HTTPX times out after 5 s of network inactivity unless told otherwise, and `timeout=None` disables it; `urllib.request.urlopen` without `timeout=` uses the socket default, which blocks indefinitely unless a process sets one.

## Decision

1. **Targets come from configuration, never from data.** Every outbound base URL comes only from an app setting validated at start-up (outside local-emulator mode, an `http://` or loopback endpoint stops the process with `insecure_endpoint`), or from the Entra key-set URL built from `DOCCHAT_AUTH_TENANT_ID`. No value read from a request, a SQL or Cosmos DB row, a blob, a document or a model output is ever used as, or inside, the scheme, host or port of an outbound call, and none becomes part of a path except a typed identifier (a UUID or an integer, validated by its model) or a name that `docchat_core` generates and re-validates before use: blob paths and physical index names come only from `docchat_core.ingestion.names` (`source_path`, `rendition_page_path`, `rendition_marker_path`, `work_prefix`, `physical_index_name`) over UUIDs and integers. The tools outside the apps follow the same rule: `docchat-ops`, `docchat_testing` and `scripts/ci/` call only `login.microsoftonline.com`, `graph.microsoft.com`, `api.loganalytics.io`, `api.github.com`, `api.osv.dev`, the k6 release files named by `tests/load/k6.lock.json` (its tarball and checksums file on `github.com`), the endpoints in `.azure/<env>/.env` and the GitHub variables; `tests/load/aci/run.py` calls only the k6 URL of the lock.
2. **Explicit timeouts, no redirects in first-party HTTP.** Every `httpx.AsyncClient` is created with an explicit `httpx.Timeout` and `follow_redirects=False`; every `urllib.request.urlopen` call in the standard-library tools passes `timeout=`; every Azure SDK client that `docchat_core.clients` creates passes explicit `connection_timeout` and `read_timeout`; every Cosmos DB call passes a per-operation `timeout` (the whole operation, retries included). The values, each owned by the component that calls the service:

   | Client | Connect | Read, or whole operation |
   |---|---|---|
   | Blob and Queue, both apps (`azure.storage.blob.aio`, `azure.storage.queue.aio`) | 10 s | read 60 s (per network read; streamed uploads and downloads read in chunks) |
   | AI Search in the API (`SearchClient`) | 10 s | read `DOCCHAT_SEARCH_TIMEOUT_SECONDS` (default 10 s) |
   | AI Search in ingestion (`SearchClient`, `SearchIndexClient`) | 10 s | read 120 s |
   | Document Intelligence (`DocumentIntelligenceClient`) | 10 s | read 300 s per request; the analysis as a whole is bounded by the activity deadline |
   | Cosmos DB (`CosmosClient`) | 5 s (`connection_timeout`) | per operation on the chat path: point read 2 s, query page 5 s, batch, patch, replace and create 5 s; elsewhere (`docchat-ops user` commands) 10 s |
   | Key Vault in `docchat_testing` and `docchat-ops` (`SecretClient`) | 5 s | read 30 s |
   | `AsyncOpenAI`, API profile | 10 s (`DOCCHAT_OPENAI_CONNECT_TIMEOUT_SECONDS`) | read 60 s (`DOCCHAT_OPENAI_READ_TIMEOUT_SECONDS`), write 10 s, pool 10 s; `max_retries=0` |
   | `AsyncOpenAI`, ingestion profile | 5 s | `httpx2.Timeout(60.0, connect=5.0)`; `max_retries=3` |
   | `httpx.AsyncClient`, Entra signing keys | 4 s | `httpx.Timeout(8.0, connect=4.0)` inside `asyncio.timeout(8.0)` |
   | `httpx.AsyncClient`, Prompt Shields | 5 s | `DOCCHAT_CONTENTSAFETY_TIMEOUT_SECONDS` (default 5 s) |
   | `httpx.AsyncClient`, other callers | 5 s | ROPC in tests 15 s; the GitHub OIDC token request 10 s; Microsoft Graph and the Logs query API 30 s |

3. **One place builds clients.** In `docchat_core`, `docchat_api` and `docchat_ingestion`, an Azure SDK client class or `openai.AsyncOpenAI` is instantiated only inside `docchat_core.clients`, once per process, and closed at shutdown (ADR 0113); `httpx2` is imported only in `docchat_core.clients.openai` (ADR 0130). An `httpx.AsyncClient` may be built by the module that owns its call (the signing-key provider builds its own), always under rule 2.
4. **Checked in CI.** `scripts/ci/check_outbound_clients.py`, an AST check in the Python standard library run by the `lint` job, parses every workspace member's `src/` tree, `scripts/ci/` and `tests/load/aci/run.py`, and fails, naming file and line, when an `httpx.AsyncClient(` call has no `timeout=` keyword or passes `follow_redirects=True`; when a `urlopen(` call has no `timeout=`; when an Azure SDK client constructor inside `docchat_core.clients` lacks `connection_timeout=` or `read_timeout=` (Cosmos DB: `connection_timeout=`); or when, in `docchat_core`, `docchat_api` or `docchat_ingestion`, a class imported from `azure.storage`, `azure.cosmos`, `azure.search.documents`, `azure.ai.documentintelligence` or `azure.keyvault`, or `openai.AsyncOpenAI`, is instantiated outside `docchat_core.clients`. Ruff's banned-API list stops the synchronous clients (`requests`, `httpx.Client`, the non-`aio` Azure clients) from being imported at all (ADR 0129).

## Alternatives considered

- **An allow-list of hosts checked at call time.** Rejected: it guards the symptom; with no target ever taken from data, there is nothing to check at call time, and the AST check proves it mechanically.
- **The SDKs' default timeouts.** Rejected: azure-core's 300-second connect and read defaults would let one silent service hold a chat stream, an activity slot or a SQL thread for minutes.
- **One timeout for every client.** Rejected: a search query on the chat path must give up within seconds, while a layout analysis legitimately reads for minutes; each value belongs to the component that knows its call.
- **Code review alone.** Rejected: a missing `timeout=` is easy to miss and costly to find in production; the AST pass in the `lint` job catches it on every pull request.
- **Building clients wherever they are used.** Rejected: clients created per request leak connections and skip the shared credential and timeouts; one factory creates each once.

## Consequences

- No operation fetches a caller-supplied URL, and no stored or generated value can redirect a call to another host, so the request-forgery class has no entry point; 82-security.md states it as a testable requirement.
- Every outbound wait is bounded, so a hung dependency ends a request with `503 dependency_unavailable` or a stream with its `error` event, and an activity with a retryable `activity_timeout`, instead of holding capacity.
- Azure SDK clients keep azure-core's redirect policy; they reach only configured Azure endpoints. The standard-library downloads of the k6 release files follow the release host's redirect, and the SHA-256 in the lock verifies what arrives (ADR 0135).
- The AST check can't see per-call arguments, so the Cosmos DB per-operation timeouts live in the conversation store's repository functions (ADR 0073).
- A new client or a changed timeout is a change to `docchat_core.clients`, which CODEOWNERS routes to the security team.
- Tests (84-testing.md): `check_outbound_clients.py` fails on `httpx.AsyncClient()` without `timeout=`, on `follow_redirects=True`, on `urlopen(url)` without `timeout=`, on an Azure SDK client built in `docchat_core.clients` without `connection_timeout=` or `read_timeout=`, and on a `BlobServiceClient(...)` call in `docchat_api`, and passes the same calls written as required.

## References

- azure-core configuration (`connection_timeout`, `read_timeout`, `permit_redirects`): https://learn.microsoft.com/en-us/python/api/overview/azure/core-readme?view=azure-python (checked 2026-10-21)
- HTTPX timeouts: https://www.python-httpx.org/advanced/timeouts/ (checked 2026-10-21)
- Python `urllib.request.urlopen` timeout: https://docs.python.org/3.13/library/urllib.request.html (checked 2026-10-21)
- Python `ast` module: https://docs.python.org/3.13/library/ast.html (checked 2026-10-21)
