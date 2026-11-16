# ADR 0130: One first-party HTTP client

- **Status:** Accepted
- **Date:** 2026-10-21
- **Decided by:** Architect
- **Specified in:** 01-repository.md, 82-security.md, 84-testing.md, 85-observability.md
- **Related ADRs:** 0009, 0017, 0062, 0068, 0070, 0113, 0129, 0131, 0137, 0150

## Context

Every I/O call is an awaited async call, the fetch of Microsoft Entra signing keys included, and every client is created once per process, shared and closed at shutdown. Plain HTTPS calls (the signing keys, the Content Safety REST call, test and operator tooling) use `httpx` with its async client, and Azure OpenAI is called through the `openai` package's `AsyncOpenAI`. That package (3.31.0) brings its own HTTP transport, `httpx2`, a separate distribution with its own module name, so the lock holds two HTTP stacks. Left alone, code would drift between them: each would need its own timeout, redirect and mocking rules, and only one of them is traced.

Vendor facts (checked 2026-10-14 unless stated):

- `httpx` 0.28.1 (released 2024-12-06) is the latest stable release and carries the classifier 4 - Beta; `httpx2` 2.13.1 (2026-09-23) is Production/Stable and is imported as `httpx2`; `openai` 3.31.0 depends on `httpx2 >=2.12.0,<3`, and the lock resolves 2.13.1.
- Without explicit options, `AsyncOpenAI` retries twice (`DEFAULT_MAX_RETRIES = 2`) and allows each request a 10-minute total timeout with a 5-second connect timeout.
- The Azure Monitor OpenTelemetry distro 1.8.11 bundles instrumentations for `azure_sdk`, `django`, `fastapi`, `flask`, `httpx`, `psycopg2`, `requests`, `urllib` and `urllib3`, and none for `httpx2`.
- respx 0.23.1 mocks `httpx` transports in unit tests (checked 2026-10-15).

## Decision

1. **`httpx` is the one first-party HTTP client.** Every plain HTTPS call that first-party code makes uses `httpx` 0.28.1 `AsyncClient`: the Entra signing-key fetch (`EntraSigningKeyProvider` in `docchat_core.auth`: `httpx.Timeout(8.0, connect=4.0)`, `httpx.Limits(max_connections=4)`, ADR 0017), the Prompt Shields call `POST {endpoint}/contentsafety/text:shieldPrompt?api-version=2024-09-01` (timeout `DOCCHAT_CONTENTSAFETY_TIMEOUT_SECONDS`, default 5 s, ADR 0062), and, in the tools, the ROPC token request (15 s) and the GitHub OIDC token request (10 s) in `docchat_testing`, Microsoft Graph and the Log Analytics query API (30 s) in `docchat-ops` and `docchat_testing`, and `DeployedApiClient`. In the apps each client is created once per process at start-up and closed at shutdown. Every `httpx.AsyncClient` passes an explicit `httpx.Timeout` and `follow_redirects=False`, and its target comes only from a validated setting (ADR 0150). The synchronous `httpx.Client`, `requests` and `urllib.request` are banned in first-party code; only the standard-library CI tools and the load generator's bootstrap script call `urllib.request.urlopen`, always with `timeout=` (ADR 0129).
2. **`httpx2` stays inside the `openai` client.** `packages/core/src/docchat_core/clients/openai.py` is the only module that imports `httpx2`, and only to configure the two `AsyncOpenAI` profiles it builds:
   - the API profile: `base_url = "<DOCCHAT_OPENAI_ENDPOINT>/openai/v1/"`, `api_key` = a bearer-token provider over the process's one credential for `https://ai.azure.com/.default`, `max_retries = 0` (the chat pipeline's own retry rules apply, ADR 0070), `timeout = httpx2.Timeout(connect=10.0, read=60.0, write=10.0, pool=10.0)` (connect and read from `DOCCHAT_OPENAI_CONNECT_TIMEOUT_SECONDS` and `DOCCHAT_OPENAI_READ_TIMEOUT_SECONDS`), `http_client = httpx2.AsyncClient(limits=httpx2.Limits(max_connections=200, max_keepalive_connections=50))`;
   - the ingestion profile: the same base URL and token provider, `max_retries = 3`, `timeout = httpx2.Timeout(60.0, connect=5.0)`.
   Every model call in the code base goes through one of these two clients; no other module, test code included, imports `httpx2`.
3. **Held there by two checks.** Ruff's `banned-api` rule (`TID251`) bans `httpx2` in every Python file; the per-file ignore for `docchat_core/clients/openai.py` is the only one that exists for `httpx2`'s sake, and the other `TID251` ignores exist for other names (ADR 0129). Because those other ignores also lift the `httpx2` ban in their files, import contract 7, "`httpx2` is imported only by `docchat_core.clients.openai`" (an import-linter `forbidden` contract over the six first-party packages, with `docchat_core.clients.openai -> httpx2` as its one ignored import), covers `docchat_core.sql.engine`, `docchat_core.sql.statements` and the model stub package too.
4. **Testing without `httpx2`.** Unit tests mock `httpx` with respx or `httpx.MockTransport`. Code that calls a model is tested at the integration level against the model stub, which the endpoint setting selects (ADR 0131); code above the model calls takes an in-memory fake of its port in unit tests. A unit test of a client profile reads the built client's `max_retries` and its `timeout.connect`, `timeout.read`, `timeout.write` and `timeout.pool` values and imports no `httpx2`.
5. **Tracing follows the split.** The distro's `httpx` instrumentation gives every first-party HTTPS call a client span; model calls, which no instrumentation sees through `httpx2`, are traced and measured by the calling code's own `docchat.*` spans and metrics (ADR 0137).
6. **When the choice reopens.** When the distro ships an `httpx2` instrumentation, or `httpx` stops receiving fixes upstream, a new ADR reconsiders the first-party client. Swapping then changes every module that constructs an `httpx.AsyncClient` (the signing-key provider, the Prompt Shields adapter, `docchat_testing`, `docchat-ops`) and their respx tests, not `docchat_core.clients` alone; ADR 0009's register carries the trigger.

## Alternatives considered

- **`httpx2` as the first-party client now.** Rejected: the distro instruments `httpx` and not `httpx2`, and respx mocks `httpx`; moving would lose the client spans of every first-party call and the unit tests' mocking, for a classifier change.
- **`aiohttp` for first-party calls.** Rejected: a third HTTP stack, with no instrumentation in the distro and no respx support.
- **The `openai` package's transport for the other calls.** Rejected: it would spread `httpx2` imports through the code base and leave those calls untraced.
- **Tests that build `httpx2.AsyncClient(transport=httpx2.MockTransport(...))`.** Rejected: they would import `httpx2` outside its one module, against the ban, and test a transport that production never configures that way; the model stub exercises the real client from end to end.
- **The Content Safety SDK for Prompt Shields.** Rejected: the GA SDK has no Prompt Shields operation and the SDK that has one is a preview (ADR 0009).

## Consequences

- Two HTTP libraries are installed, but each call site uses exactly one, and every first-party call follows one set of timeout, redirect, target and mocking rules.
- Model calls carry no automatic HTTP spans; their own spans and metrics must stay complete, which the telemetry tests check (ADR 0137).
- `httpx`'s Beta classifier is a recorded exception in ADR 0009's register, reviewed with each `httpx` Dependabot pull request.
- `httpx2` updates arrive through the `openai` lock; each one runs the stub-conformance test against `AsyncOpenAI` (stream, structured output, embeddings with `dimensions`, `retry-after-ms`).
- Tests (84-testing.md): Ruff and `lint-imports` in the `lint` job fail on any `httpx2` import outside `docchat_core.clients.openai`; the outbound-client check (ADR 0150) fails on an `httpx.AsyncClient` without `timeout=` or with `follow_redirects=True`; the profile unit tests prove each profile's retries and timeouts.

## References

- httpx on PyPI (0.28.1): https://pypi.org/project/httpx/ (checked 2026-10-14)
- httpx2 on PyPI (2.13.1): https://pypi.org/project/httpx2/ (checked 2026-10-14)
- openai on PyPI (3.31.0): https://pypi.org/project/openai/ (checked 2026-10-14)
- Azure Monitor OpenTelemetry distro and its bundled instrumentations: https://learn.microsoft.com/en-us/python/api/overview/azure/monitor-opentelemetry-readme?view=azure-python (checked 2026-10-14)
- respx on PyPI (0.23.1): https://pypi.org/project/respx/ (checked 2026-10-15)
