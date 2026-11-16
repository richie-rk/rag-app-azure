# ADR 0002: Product scope and deferred features

- **Status:** Accepted
- **Date:** 2026-10-18
- **Decided by:** Product owner
- **Specified in:** 00-overview.md, 01-repository.md, 10-web-client.md, 80-data-model.md, 82-security.md, 83-infrastructure.md, 84-testing.md
- **Related ADRs:** 0003, 0017, 0032, 0044, 0046, 0056, 0063, 0069, 0071, 0083, 0098, 0109, 0129, 0133, 0152

## Context

DocChat is a "chat with your documents" platform for one organisation at a time. Its scope has to be small enough that every shipped path is built, tested and reviewed, and every sign-in has to pass the organisation's own identity controls (multi-factor authentication, Conditional Access, app-role assignment). User questions and documents are the organisation's data, so they may reach only the Azure services the deployment declares. Several features are wanted later (an evaluation harness, retrieval over conversation history, personas, audio and video, source-code repositories, vision, PII anonymisation and others); the first version must not contain half-built versions of them, yet must not make them harder to add. Code that no shipped feature uses is attack surface with no benefit: it has to be reviewed, patched and scanned, and it misleads whoever changes the code next.

## Decision

1. **Microsoft Entra ID is the only way in, for one tenant.** Every registration is single tenant (`signInAudience: 'AzureADMyOrg'`). The web client's MSAL authority is `https://login.microsoftonline.com/<tenantId>`, never `common` or `organizations`. The API's platform authentication has only the Microsoft Entra provider, with issuer `https://login.microsoftonline.com/<tenantId>/v2.0`, and in-app token validation requires `tid` to equal the configured tenant ID. No password, local account, social provider or Static Web Apps provider exists: `app.users` holds no credential column of any kind, and the static site answers `404` on `/.auth/login/aad`, `/.auth/login/github` and every other path under `/.auth/`.
2. **One organisation per deployment.** The API app setting `WEBSITE_AUTH_AAD_ALLOWED_TENANTS` holds exactly one tenant ID. No primary key, partition key, index or blob path includes an organisation or tenant identifier, and no query filters by one: Cosmos DB partition keys are user object IDs and storage paths are generated IDs; `app.users.tenant_id` only records the `tid` of the user's latest token. A second organisation is a second deployment, with its own resources and its own Entra registrations.
3. **No Azure API Management.** The API's default host name, `https://app-docchat-api-<env>-<token>.azurewebsites.net`, is the single public entry point. Authentication, authorisation, rate limits, quotas and problem details live in the API itself.
4. **User content reaches only the environment's Azure services.** Model calls go only to the Azure OpenAI deployments declared in Bicep; callers never name a model, and no client for any other model provider exists. Uploads accept exactly PDF, DOCX and PPTX, and readers receive every format only as sanitised PDF renditions: DOCX and PPTX are converted to PDF in the ingestion sandbox (ADR 0152). No microphone, speech or audio path exists, and the static site's Permissions-Policy denies `microphone=()` and `camera=()` with every other listed feature except `clipboard-write=(self)`.
5. **No dormant code.** The repository holds no module, route, screen, setting, dependency or Bicep resource that no shipped feature uses, no feature flag for an unbuilt feature and no stub for later work; code whose last caller goes is deleted in the same change. Every on-off setting selects between behaviours that are built and tested in the first version: the feature switches are the ops-config keys `followups.enabled`, `memory.enabled` and `feedback.comments_enabled`, each default `true`, and the boolean fields of `infra/env/<env>.json` choose infrastructure (malware scanning, the budget, alerts, the model stub, zone options), never unbuilt code. CI enforces the rule: Ruff `ERA` (code left in comments), deptry (unused or undeclared dependencies), line-and-branch coverage of at least 90% for `docchat_core` and 85% for `docchat_api` and `docchat_ingestion`, `scripts/ci/check_test_layout.py` (every module has its unit-test file), and the authorisation matrix, which fails for any route in the OpenAPI document without its cases and requires the public set to be exactly `GET /healthz`.
6. **Deferred, and kept possible.** None of these items is designed, stubbed or flagged in the first version. Each keeps the hook below, which the first version builds for its own reasons:

| Deferred item | Hook in the first version |
|---|---|
| Retrieval-quality evaluation harness | each finalised turn's write-once `turn_trace` holds the standalone query, the retrieved chunk IDs with their scores and the answer call as sent to the model (ADR 0071) |
| Retrieval over conversation history | turns are stored per session with stable UUIDv5 IDs, so embedding them later needs no data migration (ADRs 0056, 0071) |
| Personas or a prompt library | project instructions are data, and the answer call is assembled in one step with one defined message order (system rules and project guidance, context, earlier turns, sources, question), where another layer fits (ADR 0063) |
| Audio and video transcription; source-code repositories; vision and multimodal content | an ingestion method is code plus one catalogue row, and index schemas are versioned, so a new method and a new allowed file type are enough (ADRs 0044, 0046) |
| PII detection and anonymisation | the answer prompt is assembled in exactly one place, where a redaction step fits (ADR 0063) |
| Chat restricted to one document | every chunk carries a filterable `documentId` (ADR 0044) |
| User-initiated deletion of chat sessions | sessions are kept indefinitely; retention is the per-container default-TTL parameter `conversationRetentionDays`, 0 (off) by default (ADR 0032) |
| Other identity providers and multiple organisations | identity is resolved in one module, `docchat_core.auth`, from validated token claims (ADR 0017) |
| A web application firewall in front of the API | the API is the single public entry point (item 3) |
| Voice input and read-aloud answers | the chat input and the answer view are self-contained components that call no speech API |
| Egress filtering and private monitoring ingestion | both apps use VNet integration from the first version, and address ranges are reserved (ADR 0109) |
| Customer-managed encryption keys | Key Vault exists in RBAC mode, empty in the first version |

## Alternatives considered

- **More sign-in methods (local accounts with passwords, Microsoft accounts, the Static Web Apps providers).** Lost: passwords need storage, hashing, reset, lockout and their own multi-factor rules, and every extra method is a way in that bypasses the organisation's Conditional Access policies; Entra ID already provides all of it.
- **A multi-organisation data model.** Lost: a tenant discriminator adds an isolation dimension to every query, partition key, cache and test, for a need one organisation doesn't have; a deployment per organisation separates organisations completely with no code at all.
- **Azure API Management in front of the API.** Lost: it adds cost and a second policy layer, and per-user limits and quotas still have to live in the API, because they count each user's actual model tokens.
- **Stubs or hidden flags for the deferred features.** Lost: dormant code is untested attack surface that has to be reviewed, patched and scanned while delivering nothing.

## Consequences

- The attack surface is exactly what the product uses: one identity path, one public entry point, and routes that all carry authorisation tests.
- A deferred feature needs its own design, ADR and tests before it ships; the hooks keep it from needing a data migration or a redesign.
- A second organisation means a second deployment, with its own resources, registrations and idle cost.
- Obligations: the authorisation matrix, deptry, Ruff `ERA`, the coverage gate and the test-layout gate run on every pull request; the deployed web smoke tests check that `/.auth/login/aad`, `/.auth/login/github` and `/.auth/me` answer `404` and that every response header, the Permissions-Policy included, equals the template; every browser test fails on any Permissions-Policy warning, console error or CSP violation.
- Risk: users ask for voice input or session deletion before either is designed. The answer is a new ADR, never a partial feature.

## References

- Static Web Apps authentication and authorisation (pre-configured providers and blocking them with `404` route rules): https://learn.microsoft.com/en-us/azure/static-web-apps/authentication-authorization (checked 2026-10-14)
- Microsoft Entra provider for App Service authentication (v2 issuer, `WEBSITE_AUTH_AAD_ALLOWED_TENANTS`): https://learn.microsoft.com/en-us/azure/app-service/configure-authentication-provider-aad (checked 2026-10-14)
