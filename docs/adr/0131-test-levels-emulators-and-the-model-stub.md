# ADR 0131: Test levels, emulators and the model stub

- **Status:** Accepted
- **Date:** 2026-10-21
- **Decided by:** Architect
- **Specified in:** 84-testing.md, 01-repository.md, 81-config.md, 83-infrastructure.md
- **Related ADRs:** 0006, 0019, 0092, 0112, 0122, 0124, 0126, 0129, 0132, 0133, 0134

## Context

Tests are part of the definition of done, and their shape is set: unit tests for every module; integration tests on local emulators started from `compose.yaml` (Azurite, SQL Server and a Cosmos DB emulator that supports hierarchical partition keys); tests against live Azure only behind an explicit flag; deployed and load tests with test-user tokens; and a stubbed model that is a separate OpenAI-compatible service chosen by the endpoint setting, never a switch in production code. Every client is created through a composition root, so each test level can replace what it doesn't run. Three of the services the apps call have no local emulator (AI Search, Content Safety's Prompt Shields, Document Intelligence), and the real model is slow, costly and nondeterministic, so each level must say what stands in for what.

Vendor facts (checked 2026-10-14):

- The Linux vNext Cosmos DB emulator is GA from `vnext-EN20260602`; hierarchical partition keys work from `vnext-EN20260907`; it enforces no authentication, charges no request units and lacks range and composite indexes.
- SQL Server 2025 container images run only on x86-64 hosts (Rosetta, Prism and QEMU are unsupported) with SQL authentication only; filtered unique indexes and `rowversion` behave as in Azure SQL.
- Azurite 3.37.0 marks blob index tags as preview and supports neither blob soft delete nor blob versions.
- Prompt Shields (API 2024-09-01) accepts at most 5 documents and 10,000 characters per call. The reasoning models in use reject `temperature`, `top_p` and `max_tokens`, and gpt-5.1 and later reject `reasoning_effort: minimal`.

## Decision

1. **Levels and locations.**

   | Level | Location | Runs against | When |
   |---|---|---|---|
   | Unit (Python) | `<member>/tests/unit/`, one test module per source module | the process only: `httpx` mocked with respx, Azure SDK clients given `ReplayTransport`, in-memory fakes through the composition root, time through `FakeClock` | every CI run |
   | Unit (web) | `apps/web/src/**/*.test.ts(x)` | jsdom and MSW | every CI run |
   | Integration | `<member>/tests/integration/` and `tests/integration/` (the API in process, ingestion orchestrators through the orchestration harness) | the `compose.yaml` services | every CI run |
   | Contract | `tests/integration/contracts/` | each in-memory fake (every CI run) and the matching `dev` service (`--live`) | as stated |
   | Live | `<member>/tests/live/` | the `dev` environment's Azure services | by an operator, with `-m live --live`, at each stage acceptance and before a release that changes an adapter |
   | Deployed | `tests/deployed/` | a deployed environment's public endpoints with test-user tokens (ADR 0019); the infrastructure suite in `dev` as the `dev` deploy identity | after every `dev` deploy (`smoke`), nightly (`full`), weekly and after every change under `infra/` reaches `dev` (`infra`), after load seeding, after every `prod` deploy (`prod_smoke`) |
   | Browser | `tests/e2e/` | the production web build under the Static Web Apps CLI emulator with a stub API and identity provider; the deployed site | every pull request; after every web deploy (ADR 0092) |
   | Load | `tests/load/` | the short-lived `load` environment (ADR 0126) | per runbook R11 (ADR 0134) |

   pytest runs with `--strict-markers`, `--strict-config`, `filterwarnings = ["error"]`, `asyncio_mode = "auto"` and a 60-second default timeout; the markers `integration` (set automatically under `tests/integration/`), `live` and `deployed` keep the outer levels out of ordinary runs, and a selected integration test whose compose service is unreachable fails the session instead of being skipped.
2. **One test-support package.** The workspace member `docchat_testing` holds everything tests share: the pytest plugin (options `--live`, `--deployed` and `--stage-acceptance`, readiness checks, the loop guard of ADR 0132, telemetry capture), per-session SQL, Cosmos DB and storage fixtures, the in-process API fixture, test principals and an RS256 test key set for in-process tokens, ROPC helpers for deployed tests, builders, in-memory fakes, `FakeClock` and the orchestration harness. No production image contains it, and import contract 3 forbids any production package to import it or the stub (ADR 0129).
3. **Emulators** (`compose.yaml`, project `docchat`, every host port bound to `127.0.0.1`, images pinned by digest):

   | Service | Image | Port (host → container) | Readiness |
   |---|---|---|---|
   | `sqlserver` | `mcr.microsoft.com/mssql/server:2025-CU9-GDR1-ubuntu-24.04` (`platform: linux/amd64`) | 14330 → 1433 | `sqlcmd` health check every 6 s, 20 retries after a 30-second start period |
   | `azurite` | `mcr.microsoft.com/azure-storage/azurite:3.37.0` (`--skipApiVersionCheck --loose`) | 10000, 10001, 10002 | TCP check every 5 s, 12 retries |
   | `cosmos` | `mcr.microsoft.com/cosmosdb/linux/azure-cosmos-emulator:vnext-EN20260907` (`PROTOCOL=http`) | 8081 → 8081; 18080 → 8080 (health probe) | the plugin polls `/ready` for up to 180 s |
   | `model-stub` | built from `tests/model-stub/Dockerfile` | 8091 → 8000 | `GET /healthz` every 5 s, 12 retries |

   `docker compose up -d --wait` starts them locally and in CI; CI tears them down with `docker compose down -v` in every case. A newer Cosmos DB emulator tag is adopted only through a Dependabot pull request on which the full integration suite passes.
4. **Local-emulator mode is guarded.** `DOCCHAT_LOCAL_EMULATORS` (default `false`) lets a developer's process use SQL authentication, Azurite, the emulator's published Cosmos DB key and `http://` loopback model endpoints. Start-up stops with `local_emulators_in_azure` when it is `true` and `WEBSITE_SITE_NAME` or `WEBSITE_INSTANCE_ID` is set, and with `insecure_endpoint` when it is `false` and any configured endpoint uses `http://` or a loopback host. Tests build their clients directly and never set the flag.
5. **The model stub.** Package `docchat_model_stub`, image `docchat/model-stub` (built and scanned with the apps, ADR 0124), served by `uvicorn docchat_model_stub.app:app --host 0.0.0.0 --port 8000 --workers ${DOCCHAT_STUB_WORKERS}`. An app reaches it only because its endpoint setting points there: `DOCCHAT_OPENAI_ENDPOINT` locally, in CI and in `load` (where it runs on its own P1v3 plan behind a private endpoint, ADR 0112), and `DOCCHAT_CONTENTSAFETY_ENDPOINT` locally and in CI only, so `load` screens with the real Content Safety account. It serves `POST /openai/v1/chat/completions`, `POST /openai/v1/embeddings` and, locally and in CI, `POST /contentsafety/text:shieldPrompt`. It requires a bearer header (never logged), answers `400 unsupported_parameter` to `temperature`, `top_p`, `max_tokens`, `presence_penalty` or `frequency_penalty` and `400 unsupported_value` to any `reasoning_effort` other than `none`, `low`, `medium` or `high`, as the deployed models do; streams server-sent events with configurable delays (`DOCCHAT_STUB_*` settings) and a usage chunk; returns unit-length embeddings seeded by the SHA-256 of the input, so equal texts give equal vectors, for `dimensions` 1 to 3,072; enforces Prompt Shields' 5-document and 10,000-character limits; and obeys directives of the form `[[stub:<directive>]]` in the last user message (status codes, content-filter outcomes, cut streams, delays, hangs, flagged documents), each optionally limited to one call kind with `@stream`, `@json`, `@text`, `@embed` or `@shield`. Tests inspect it through `GET /_stub/stats`, `GET /_stub/requests` and scripted responses under `PUT /_stub/scripts/{name}`.
6. **Services without an emulator.** AI Search, Prompt Shields and Document Intelligence are tested three ways: adapter unit tests replay hand-written exchanges for the exact API versions in use (`2026-04-01`, `2024-09-01`, `2024-11-30`) through `ReplayTransport` or respx; domain flows use in-memory fakes injected through the composition root, never through a setting; and each fake has a contract suite that runs against the fake on every CI run and against the `dev` service with `--live`. A behaviour the domain relies on (a deleted chunk isn't returned, filtering by `documentId`, reranker scores on the 0 to 4 scale, Prompt Shields' limits, layout Markdown with HTML tables) enters the contract before the fake implements it.
7. **Live tests stay out of CI.** They run only with `--live`, from a machine whose address is in `dev`'s operator allowlist, signed in as an operator holding the just-in-time data roles, because GitHub-hosted runners can't reach the private endpoints.

## Alternatives considered

- **Mocks in place of SQL, Cosmos DB and storage emulators.** Rejected: unique and filtered indexes, `rowversion`, transactions, hierarchical partition keys and blob semantics behave correctly only in the real engines.
- **Live Azure in every CI run.** Rejected: GitHub-hosted runners can't reach the private endpoints, and live runs cost money and fail for reasons unrelated to the change.
- **A switch in production code that fakes the model.** Rejected: a switch could be turned on in a deployed environment; the stub is a separate service that only an endpoint setting selects, and import contract 3 proves production code can't reach it.
- **The real model in CI.** Rejected: nondeterministic text breaks assertions on streams and citations, every run spends quota, and the account sits behind a private endpoint.
- **A generic HTTP mock server as the stub.** Rejected: the tests need server-sent-event streaming with timing, deterministic embeddings, parameter rules and directives; a small FastAPI service with the API's pinned libraries does this and runs unchanged on App Service in `load`.
- **Fakes without contract suites.** Rejected: a fake drifts from its service unless the same assertions run against both.

## Consequences

- Every required test has one home, and only the live, deployed and load levels need Azure.
- Integration tests need Docker on an x86-64 host, because the SQL Server image runs nowhere else; developers on other hosts run them in CI.
- Emulator limits shape the tests: no request-unit measurement (it happens in `load`), no range or composite indexes, no blob soft delete or versions, and no Entra logins in SQL (the stage-0 SQL spike and the live tests prove those).
- The stub must follow the real API's rules; conformance tests against `openai` 3.31.0 (stream with usage, structured output, embeddings with `dimensions`, `retry-after-ms`) and the parameter tests fail when it drifts.
- Tests (84-testing.md): meta-tests prove that an integration session fails when a compose service is down and that the local-emulator guard stops start-up in both cases; integration tests prove the stub's directives, screening limits, scripted responses, throttling and parameter checks, and that the Cosmos DB emulator accepts the hierarchical key `/userId,/sessionId`; stage-0 acceptance A0-2 requires the whole local suite to pass.

## References

- Cosmos DB Linux emulator: https://learn.microsoft.com/en-us/azure/cosmos-db/emulator-linux (checked 2026-10-14)
- Cosmos DB emulator release notes: https://learn.microsoft.com/en-us/azure/cosmos-db/emulator-release-notes (checked 2026-10-14)
- SQL Server Linux containers: https://learn.microsoft.com/en-us/sql/linux/quickstart-install-connect-docker?view=sql-server-linux-ver17 (checked 2026-10-14)
- Azurite: https://learn.microsoft.com/en-us/azure/storage/common/storage-use-azurite (checked 2026-10-14)
- Prompt Shields (API 2024-09-01): https://learn.microsoft.com/en-us/rest/api/contentsafety/text-operations/shield-prompt?view=rest-contentsafety-2024-09-01 (checked 2026-10-14)
- Reasoning models: https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/reasoning (checked 2026-10-14)
