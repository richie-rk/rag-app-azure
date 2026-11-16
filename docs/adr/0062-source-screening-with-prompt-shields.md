# ADR 0062: Source screening with Prompt Shields

- **Status:** Accepted
- **Date:** 2026-10-18
- **Decided by:** Architect
- **Specified in:** 12-chat-pipeline.md, 13-conversations-and-memory.md, 80-data-model.md, 81-config.md, 82-security.md, 83-infrastructure.md, 84-testing.md, 85-observability.md, 86-operations.md
- **Related ADRs:** 0009, 0059, 0061, 0063, 0064, 0065, 0066, 0068, 0075, 0101, 0112, 0113, 0150, 0151

## Context

Retrieved chunks are text written by other people, and a document can carry instructions aimed at the model (indirect prompt injection). The product screens the kept sources with the Azure AI Content Safety Prompt Shields API before generation, drops any flagged source, logs its document ID for admin review, and, when screening is unavailable after one retry, ends the turn with a retryable `error` event, because an answer must never rest on unscreened sources. Delimiting and the platform rules are ADR 0063; this ADR is the screening layer in front of them.

Vendor facts:

- `POST {endpoint}/contentsafety/text:shieldPrompt?api-version=2024-09-01` is GA, and since 2025-03-01 every other version except two previews is deprecated. It takes `{userPrompt, documents[]}` and returns `userPromptAnalysis.attackDetected` and one `documentsAnalysis[i].attackDetected` per document, in order.
- One call analyses a prompt and up to five documents with at most 10,000 characters in total, so long sources must be split and packed.
- The GA SDK `azure-ai-contentsafety` 1.0.0 has no Prompt Shields operation (only the 1.1.0b1 beta adds it).
- Entra authentication needs the account's custom subdomain and the scope `https://cognitiveservices.azure.com/.default`; S0 accepts 1,000 requests per 10 seconds.
- Prompt Shields is regional (not offered in swedencentral, uksouth, germanywestcentral, northeurope, italynorth, norwayeast, spaincentral, switzerlandwest, uaenorth or southafricanorth) and was tested with English only.
- The model deployments' content filters, by contrast, analyse only the latest `user` message, and a filter hit fails the whole model call.

## Decision

**What is screened.** After retrieval (ADR 0061) and before prompt assembly, every kept source is screened. The text screened for a source is exactly the defanged body that will sit inside its `SOURCE` block (title line, section line, page line, an empty line, the chunk text; ADR 0063), so the model never receives source text the service didn't analyse.

**Segments.** Lengths are measured in UTF-16 code units, which are never fewer than code points, so the service's 10,000-character limit holds however it counts. A body of at most 10,000 units is one segment. A longer body is cut into segments of at most 10,000 units at the last whitespace before each limit (at the limit itself when the window holds no whitespace, never inside a character), and each segment after the first starts 200 units before the previous one ended, so an instruction of up to 200 units that straddles a cut appears whole in one segment.

**Calls.** Segments are packed first-fit decreasing: longest first, each into the first call that has fewer than 5 documents and room within 10,000 units in total, a new call when none fits. At most 4 calls run at once per turn, each `POST {DOCCHAT_CONTENTSAFETY_ENDPOINT}/contentsafety/text:shieldPrompt?api-version=2024-09-01` with body `{"userPrompt": "", "documents": [<segments>]}`, a bearer token for `https://cognitiveservices.azure.com/.default` from the process credential, and a 5-second timeout (`DOCCHAT_CONTENTSAFETY_TIMEOUT_SECONDS`, default 5, range 1 to 15). `documentsAnalysis[i]` belongs to the call's `i`-th segment.

**Retry and fail closed.** A call that times out, fails to connect, or answers `429` or `5xx` is retried once, after 0.5 s, or after its `Retry-After` when that is at most 2 s. A second failure ends the turn with the `error` event `source_screening_unavailable` (retryable) before any answer call. Any other non-`2xx` status, and a `2xx` body without one boolean `attackDetected` per document sent, end the turn the same way at once, because an unscreened source is never sent. No setting bypasses screening.

**Flagged sources.** A source is flagged when any of its segments has `attackDetected = true`. A flagged source is dropped: it is never sent to the model, never cited, and its URLs never enter `allowedLinks` (ADR 0064). Each one is logged as the custom event `source_screening_flagged` with the document ID, chunk set ID, search index ID, project ID, session ID and turn ID, never text. The remaining sources keep their rank order and receive the source IDs `S1`, `S2` and onwards in that order. When every source is flagged, the answer call receives the line that no sources were retrieved, the platform rules have the model say that the sources don't hold the answer, and no follow-ups are suggested (ADR 0065).

**Empty `userPrompt`.** At stage 3, the empty-`userPrompt` probe of `docchat-ops --env dev chat model-check` (ADR 0068) sends `{"userPrompt": "", "documents": [<the command's indirect-attack probe document>]}` to the `dev` Content Safety account and records the status code and `attackDetected` in its `model_deployments.checked` row, confirming that the service accepts an empty `userPrompt` and flags the document; the deployed screening test confirms the same through a chat turn. If the service refuses the empty string with `400`, the API sends a single space, and this ADR is amended with the result.

**One screening module for sources and facts.** `docchat_api.chat.screening` is the only module that calls Prompt Shields, through one `httpx.AsyncClient` (httpx 0.28.1, ADR 0009) built by `docchat_core.clients` (ADR 0113, ADR 0150). Memory uses it too: `shield_user_prompt(text: str) -> bool` sends `{"userPrompt": text, "documents": []}` with the same client, endpoint, token, timeout and retry rule; `text` must hold 1 to 10,000 UTF-16 units, otherwise it raises `ValueError` without a call; it returns `userPromptAnalysis.attackDetected`; it raises `docchat_api.chat.screening.ScreeningUnavailable` after the second failure, and at once, with no retry, on any other non-`2xx` status (a `400` included) or on a `2xx` body without a boolean `userPromptAnalysis.attackDetected`. The conversation store screens every extracted or user-edited fact through it (ADR 0075): `ScreeningUnavailable` refuses a fact edit with `503 dependency_unavailable` and returns an extraction to `pending`. The request probe of the same `chat model-check` run sends `{"userPrompt": "Works on supplier contract renewals", "documents": []}` and records the status code and `attackDetected`, confirming that an empty `documents` array is accepted; if the service refuses it with `400`, the body omits `documents`, and this ADR is amended with the result. The helper records no usage and emits no metric or event of its own: the memory code counts each screening in `docchat.memory.fact_screenings`, and the `httpx` instrumentation span covers the call.

**Account, identity and environments.** The API's user-assigned identity holds Cognitive Services User (`a97b65f3-24c7-4388-baec-2e87135dc908`) on the Content Safety account `cs-docchat-<env>-<token>` (`kind: 'ContentSafety'`, S0, custom subdomain, local authentication off, private endpoint). The `dev` probes run as a member of `sg-docchat-dev-config-operators`, which the bootstrap grants Cognitive Services User at `rg-docchat-dev`; Microsoft Learn names that role for Microsoft Entra access to Content Safety, and no built-in role holds the Content Safety data actions alone (confirmed 2026-10-25). The region preflight fails with `prompt_shields_unavailable` in a region without Prompt Shields (ADR 0101). `load` screens against its own real Content Safety account (ADR 0112). Local and CI runs use the model stub's screening endpoint, which refuses more than 5 documents or more than 10,000 characters in one call with `400 InvalidRequestBody`, so a packing defect fails tests.

**Records.** The trace's `retrieval.screening` holds `calls`, `segments`, `characters`, `flagged` and `outcome` (`passed`, `flagged` or `unavailable`), and each `kept` entry's `outcome` is `sent`, `flagged` or `budget_dropped`. `usage.billable.shieldCalls` and `usage.billable.shieldCharacters` count the turn's screening (ADR 0059). Telemetry, metadata only: span `docchat.chat.screen` (`docchat.calls`, `docchat.segments`, `docchat.characters`, `docchat.flagged`, `docchat.outcome`); metrics `docchat.chat.screening.flagged_sources` and `docchat.chat.screening.failures` (`reason` `timeout`, `throttled` or `error`).

## Alternatives considered

- **The chat deployment's indirect-attack filter over embedded documents.** Lost: a filter hit blocks the whole answer call, so one poisoned document would fail the turn rather than drop one source; the filters analyse only the latest user message; and harm filters over third-party text would block legitimate questions about documents that discuss violence or quote attack-like text. The deployment filters remain the backstop for the question and the completion (ADR 0066), and the table-summary deployment, whose input has no per-document screening, does use the indirect-attack filter.
- **The beta SDK** (`azure-ai-contentsafety` 1.1.0b1). Lost: GA over preview; the REST operation is one POST with a two-field body.
- **Screening once, at ingestion.** Lost: screening at chat time applies the service's current detection to every document however long ago it was indexed, and screens the exact block text the model receives.
- **Failing open when the service is down.** Lost: a turn that fails visibly is safer than an answer steered by an injected instruction.
- **Cutting long sources at 10,000 characters.** Lost: the unscreened rest would reach the model.
- **One source per call.** Lost: up to 8 calls per turn where packing usually needs one or two, adding latency and request rate against the S0 limit.
- **A custom role holding only `Microsoft.CognitiveServices/accounts/ContentSafety/text:shieldprompt/action`.** Lost: Cognitive Services User is the role the product's identity table assigns to the API on Content Safety, and with local authentication off the role's key-listing action yields no key that can authenticate.

## Consequences

- A Content Safety outage stops chat (every turn ends with `source_screening_unavailable`) and memory writes (fact edits answer `503`, extractions wait as `pending`), by design. Runbook "Prompt Shields unavailable" (86-operations.md): check Content Safety service health and the region, post a service notice; there is no bypass.
- Latency: screening adds one round trip before the answer call; the worst case for one call before the turn fails is 5 s, a wait of 0.5 s to 2 s, then 5 s.
- Capacity: S0's 1,000 requests per 10 seconds bound screening; the load test derives its maximum turn rate from the measured calls per turn, at 80 requests per second (ADR 0151).
- Detection limits: Prompt Shields was tested with English only. An injection it misses still meets the delimiters and platform rules (ADR 0063), the link allowlist (ADR 0064), a model with no tools (ADR 0068), and memory that never reads sources (ADR 0075).
- Runbook "Review flagged sources" (86-operations.md): query `source_screening_flagged` events by document ID; an admin reviews each document and deletes it or uploads a corrected version.
- Tests: packing never exceeds 5 documents or 10,000 UTF-16 units per call, long bodies split with the 200-unit overlap, a source flagged in one segment is dropped, results map back by position; `shield_user_prompt`'s body, timeout, single retries (0.5 s, or 2 s with `Retry-After: 2`) and errors through `httpx.MockTransport`; the stub's screening endpoint refuses 6 documents or 10,001 characters; a source the stub flags is absent from the prompt and emits `source_screening_flagged`, and two screening failures end with `source_screening_unavailable` and zero answer calls; against the deployed `dev` Content Safety account, an empty `userPrompt` is accepted and an indirect-attack document is flagged, and an empty `documents` array is accepted (the two probes of `docchat-ops --env dev chat model-check`, operator checks recorded by the stage's performer), and a chat turn drops the fixture document `indirect-attack.pdf` as flagged (the deployed screening test).
- Alert: `docchat.chat.screening.failures` above 2% of turns for 10 minutes.

## References

- Prompt Shields REST, API 2024-09-01: https://learn.microsoft.com/en-us/rest/api/contentsafety/text-operations/shield-prompt?view=rest-contentsafety-2024-09-01 (checked 2026-10-14)
- Prompt Shields concepts (a prompt and up to five documents): https://learn.microsoft.com/en-us/azure/ai-services/content-safety/concepts/jailbreak-detection (checked 2026-10-14)
- Limits (10,000 characters per call) and regions: https://learn.microsoft.com/en-us/azure/ai-services/content-safety/region-availability (checked 2026-10-14)
- Query rates and authentication: https://learn.microsoft.com/en-us/azure/ai-services/content-safety/overview (checked 2026-10-14)
- API version deprecations: https://learn.microsoft.com/en-us/azure/ai-services/content-safety/whats-new (checked 2026-10-14)
- SDK coverage: https://pypi.org/project/azure-ai-contentsafety/ (checked 2026-10-14)
- Entra authentication and custom subdomains: https://learn.microsoft.com/en-us/azure/ai-services/authentication (checked 2026-10-14)
- Deployment filters analyse the latest user message: https://learn.microsoft.com/en-us/azure/foundry-classic/openai/concepts/content-filter-document-embedding (checked 2026-10-14)
- Roles and data actions: https://learn.microsoft.com/en-us/azure/role-based-access-control/built-in-roles/ai-machine-learning and https://learn.microsoft.com/en-us/azure/role-based-access-control/permissions/ai-machine-learning (checked 2026-10-14; the built-in roles page and the Content Safety overview checked once more on 2026-10-25 for the `dev` operator probes' role)
