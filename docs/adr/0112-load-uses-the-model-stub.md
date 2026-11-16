# ADR 0112: Load uses the model stub

- **Status:** Accepted
- **Date:** 2026-10-21
- **Decided by:** Architect
- **Specified in:** 83-infrastructure.md, 84-testing.md, 81-config.md
- **Related ADRs:** 0062, 0095, 0098, 0099, 0100, 0101, 0105, 0126, 0131, 0134, 0135, 0151

## Context

The load test runs in a short-lived `load` environment with production SKUs and measures the API's event loop, statelessness, autoscale and ingestion parallelism (ADR 0134), not the model's latency. A real Azure OpenAI account in `load` would draw on the subscription's pooled quota: Global Standard quota is pooled per subscription, model and deployment type, and `dev` plus `prod` already hold 530 units of `gpt-5.6-sol`, 740 of `gpt-5.6-terra` and 440 of `text-embedding-3-large` against a Tier-1 pool of 1,000 each (ADR 0100), so load runs at the peak scenario's rate would compete with `prod` and pay for tokens on every run. The stubbed model must be a separate OpenAI-compatible service selected by the endpoint setting, never a switch in production code. Prompt Shields screening runs on every chat turn, so it stays real in `load` and is measured.

## Decision

1. **The switch.** `infra/env/load.json` sets `openAi.enabled: false` and `modelStub.enabled: true`; `dev.json` and `prod.json` set `openAi.enabled: true` and `modelStub.enabled: false`. With `openAi.enabled` false, `main.bicep` declares no Azure OpenAI account, content filter policy, deployment, private endpoint, DNS zone or Cognitive Services OpenAI User assignment.
2. **The stub host (`load` only).** Plan `asp-docchat-stub-load` (Linux, Premium v3 P1v3, 1 instance, no autoscale); site `app-docchat-stub-load-<token>` (`kind: 'app,linux,container'`) with the user-assigned identity `id-docchat-stub-load` only (AcrPull on the shared registry); VNet integration on `snet-stub-integration` (`10.63.3.0/26`) with `imagePullTraffic: true`; `publicNetworkAccess: 'Disabled'`; private endpoint `sites` in `snet-private-endpoints` with the zone `privatelink.azurewebsites.net`; `httpsOnly: true`, TLS minimum 1.3, FTP and basic publishing off, SCM denying all traffic; `alwaysOn: true`; `healthCheckPath: '/healthz'`; `WEBSITES_PORT: 8000`; `WEBSITES_ENABLE_APP_SERVICE_STORAGE: false`; image `docchat/model-stub@<digest>` from the release's `publish` job; the process `uvicorn docchat_model_stub.app:app --host 0.0.0.0 --port 8000 --workers ${DOCCHAT_STUB_WORKERS}` with `DOCCHAT_STUB_WORKERS=2`; its other settings from `infra/config/app-settings.json` key `modelStub` (the `DOCCHAT_STUB_*` values of 84-testing.md: first token after 800 ms, 25 ms per token, 400 answer tokens, every 20th call to the ingestion embedding and table summary deployments throttled with a 500 ms retry hint).
3. **The endpoint setting is the only selector.** In `load` both apps' `DOCCHAT_OPENAI_ENDPOINT` is `https://<stub default host name>`, which the linked zone resolves to the stub's private endpoint. The deployment-name settings keep their usual values (`chat`, `chat-small`, `ingest-summary`, `embed-query-te3-large-v1`, `embed-ingest-te3-large-v1`), by which the stub keys its statistics and throttling. The clients send their Entra bearer token as in every environment; the stub ignores it. No production module imports the stub package `docchat_model_stub` (an import-linter contract enforces it).
4. **Everything else is real.** Content Safety (`DOCCHAT_CONTENTSAFETY_ENDPOINT` is `load`'s own S0 account), AI Search, Cosmos DB, SQL, both storage accounts, Document Intelligence and Key Vault are the `load` resources with `prod` SKUs where the load test measures them (ADR 0095).
5. **Network.** `nsg-docchat-load-stub` allows outbound TCP 443 to `snet-private-endpoints` and denies the rest of the network; `nsg-docchat-load-pe` admits TCP 443 from the stub subnet beside the two integration subnets.
6. **What `load` skips.** Preflight checks 6 (model versions) and 7 (quota) don't run; the capacity outputs `DOCCHAT_OPENAI_CHAT_CAPACITY` and `DOCCHAT_OPENAI_SMALL_CAPACITY` are empty; the `postprovision` hook doesn't run `docchat-ops chat capacity-check`.

## Alternatives considered

- **A real Azure OpenAI account in `load`.** Rejected: it would compete with `prod` for the subscription's pooled quota, charge for tokens on every run, and add model latency and throttling that the scaling targets don't measure.
- **A stub mode inside the API.** Rejected: production code holds no test switch; the endpoint setting selects a separate service.
- **The stub on a public endpoint.** Rejected: an internet-reachable model endpoint would accept anyone's traffic; the private endpoint keeps it inside `load`'s network.
- **The stub on the API's plan.** Rejected: it would share CPU with the system under test and distort the single-instance scenario.
- **A stubbed Content Safety too.** Rejected: screening runs on every chat turn, so the load test measures the real service; its S0 limit of 1,000 requests per 10 seconds bounds the peak scenario's rate (ADR 0151).

## Consequences

- Load results exclude real model latency and throttling; the stub's settings set first-token time, token rate and answer length, so time-to-first-token in `load` reflects the API, not the model.
- The stub plan costs $113.15 a month if left running; the nightly guard tears `load` down after 6 hours unless its run is still active (ADR 0126).
- The `load` deploy identity holds no Azure OpenAI right, and no model quota is consumed by load runs.
- Tests (84-testing.md): the `load` environment provisions with `openAi.enabled: false`, both apps reach the stub through its private endpoint, and autoscale adds an instance after 5 minutes above 70% CPU; the stub's conformance suite runs against `openai` 3.31.0 `AsyncOpenAI` in stage 0.

## References

- Global Standard quota pooling (Tier 1, per subscription): https://learn.microsoft.com/en-us/azure/foundry/openai/quotas-limits (checked 2026-10-14)
- Private endpoint DNS zones (`privatelink.azurewebsites.net`): https://learn.microsoft.com/en-us/azure/private-link/private-endpoint-dns (checked 2026-10-14)
