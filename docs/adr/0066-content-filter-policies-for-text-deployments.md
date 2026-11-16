# ADR 0066: Content-filter policies for text deployments

- **Status:** Accepted
- **Date:** 2026-10-18
- **Decided by:** Architect
- **Specified in:** 12-chat-pipeline.md, 13-conversations-and-memory.md, 14-ingestion.md, 82-security.md, 83-infrastructure.md, 84-testing.md, 85-observability.md, 86-operations.md
- **Related ADRs:** 0051, 0059, 0060, 0062, 0063, 0067, 0074, 0075, 0106, 0112, 0138

## Context

Three Azure OpenAI deployments generate text: `chat` (answers), `chat-small` (question rewrite, search passage, follow-ups, rolling summary, memory extraction) and `ingest-summary` (ingestion's table summaries, on the chat-small model with its own quota). Every Azure OpenAI deployment that generates text must use a content-filter policy declared in Bicep, with Prompt Shields for jailbreak attacks in the user's question. The embedding deployments generate no text; ADR 0067 records why they carry no policy. The `load` environment has no Azure OpenAI account and calls the model stub (ADR 0112).

Document text is written by third parties and is untrusted. The answer call carries retrieved passages in delimited source blocks (ADR 0063) after screening each one with Prompt Shields (ADR 0062). Ingestion sends the HTML of each table in an uploaded document to a model and indexes the summary it writes (ADR 0051). The platform filters must judge the user's question and every model output without letting one document's wording fail a whole turn.

Vendor facts that bound the choice (Microsoft Learn, checked 2026-10-14 unless dated otherwise):

- In Chat Completions, the harm categories and Prompt Shields run on the latest `user` message only. Indirect-attack detection inspects only documents wrapped as `"""` + `<documents>` … `</documents>` + `"""`, with JSON escaping.
- Prompt Shields for direct attacks (jailbreak) is GA and on by default for prompts; Prompt Shields for indirect attacks is GA and off by default; protected material text and code are GA and on by default for completions; turning harm filters off on completions needs Microsoft's approval.
- The default safety policy for text models filters hate and fairness, violence, sexual and self-harm content at severity threshold Medium on prompts and completions, jailbreak on prompts, and protected material text and code on completions (checked 2026-10-18).
- A filtered prompt returns HTTP `400` with error code `content_filter`. A filtered completion ends with `finish_reason: content_filter` (in a stream, on the last chunk for that choice). When the filtering system is down or can't finish in time, the request completes unfiltered and `content_filter_results` carries an error object with code `content_filter_error` (checked 2026-10-18).
- `Microsoft.CognitiveServices/accounts/raiPolicies` (latest GA API 2026-07-01) takes `basePolicyName`, `mode` (`Default`, `Deferred`, `Blocking` or `Asynchronous_filter`), `contentFilters[]` (`name`, `enabled`, `blocking`, `severityThreshold`, `source`) and `customBlocklists[]`; filter names are `Hate`, `Sexual`, `Selfharm`, `Violence`, `Jailbreak`, `Indirect Attack`, `Protected Material Text`, `Protected Material Code` and `Profanity`. A deployment names its policy in `properties.raiPolicyName` and must depend on it.

## Decision

**1. Two policies, declared in Bicep.** `infra/modules/ai/openai-deployments.bicep` declares both policies on `Microsoft.CognitiveServices/accounts/raiPolicies@2026-07-01` before any deployment; every deployment (`Microsoft.CognitiveServices/accounts/deployments@2026-07-01`, declared with `@batchSize(1)`) depends on its policy.

| Setting | `docchat-text-guard` (on `chat` and `chat-small`) | `docchat-table-summary-guard` (on `ingest-summary`) |
|---|---|---|
| `basePolicyName` | `Microsoft.DefaultV2` | `Microsoft.DefaultV2` |
| `mode` | `Default` (synchronous filtering, streamed output included; never `Asynchronous_filter` or `Deferred`) | `Default` |
| `Hate`, `Sexual`, `Violence`, `Selfharm` | each twice, `source: Prompt` and `source: Completion`, with `enabled: true`, `blocking: true`, `severityThreshold: Medium` | the same |
| `Jailbreak` (`source: Prompt`) | `enabled: true`, `blocking: true` | the same |
| `Indirect Attack` (`source: Prompt`) | `enabled: false` | `enabled: true`, `blocking: true` |
| `Protected Material Text`, `Protected Material Code` (`source: Completion`) | `enabled: true`, `blocking: true` | the same |
| `Profanity` | `enabled: false` | `enabled: false` |
| `customBlocklists` | none | none |

**2. Attachment and enforcement.** In `infra/env/<env>.json`, the deployment objects `openAi.deployments.chat` and `openAi.deployments.chatSmall` carry `raiPolicyName: docchat-text-guard` and `openAi.deployments.tableSummary` carries `raiPolicyName: docchat-table-summary-guard`. A change to either policy is a reviewed pull request to `openai-deployments.bicep`. PSRule rule `DocChat.OpenAI.DeploymentRules` fails the pull request when a deployment whose model name doesn't start with `text-embedding` names no `raiPolicyName` (ADR 0106). `load` declares no account, policy or deployment.

**3. Sources before the question.** In the answer call the latest `user` message holds only the `QUESTION` block, and the sources travel in the `user` message before it (the message order of ADR 0063). The platform filters therefore judge the question, and third-party source text is judged by the per-source Prompt Shields screening of ADR 0062, which drops a flagged source on its own and fails closed when screening is unavailable.

**4. Table summaries under the indirect-attack shield.** The table-summary call puts each table's sanitised HTML, JSON-escaped, between `"""` + `<documents>` and `</documents>` + `"""` in its `user` message, so the indirect-attack shield inspects it (ADR 0051).

**5. Filter outcomes.**

| Call | Outcome | Behaviour |
|---|---|---|
| Answer | HTTP `400` with code `content_filter` | stream `error` `question_filtered` (not retryable) |
| Answer | `finish_reason: content_filter` | stream `error` `answer_filtered` (not retryable); the turn is stored `failed` with the text already released and is never sent as history |
| Answer | any other HTTP `400` | stream `error` `model_request_rejected` (not retryable) |
| Answer | a chunk whose `content_filter_results` carries an error object | the turn continues; custom event `content_filter_degraded` (deployment, project, session and turn IDs) |
| Question rewrite, search passage, follow-ups | filtered prompt or completion | the call's own fallback: the question as written, the first search results, no follow-ups |
| Rolling summary | filtered prompt or completion | `summaryJob.lastErrorCode` `content_filtered`; retried after the summary backoff (ADR 0074) |
| Memory extraction | filtered prompt or completion | extraction `failed` with `errorCode` `content_filtered` at once, with no further attempt (ADR 0075) |
| Table summary | filtered prompt, filtered completion or refusal | the table is indexed as sanitised HTML without a summary; run-item warning `table_summary_filtered` with the page |

Every filtered call of the API still produces a usage record with `outcome` `filtered`; a filtered prompt reports no usage, so its record is estimated from the measured prompt (ADR 0059, ADR 0060).

**6. Telemetry and alert.** Every filter hit on the answer, question-rewrite, search-passage and follow-ups calls emits custom event `content_filter_triggered` with `where` (`prompt` or `completion`), `call`, deployment, project, session and turn IDs, never text. The summary and memory calls record `content_filtered` as `docchat.error_code` on their `docchat.summary.rebuild` and `docchat.memory.extract` spans. Alert A14 `content-filter-volume` (log alert, 1-hour window evaluated every hour, muted for 1 hour after it fires, severity 3, runbook R07) fires when more than 20 `content_filter_triggered` events arrive in the window.

## Alternatives considered

- **Microsoft's default policy, attached implicitly.** Lost: the policy must be declared in Bicep, and an implicit default could change with no reviewed change in this repository. The explicit policy keeps the default's thresholds and makes them visible and testable.
- **Sources in the final user message, with the question.** Lost: the harm and jailbreak filters read the whole latest user message, so a document that discusses violence or quotes attack-like text would block a legitimate question and fail the turn.
- **Indirect-attack shield on `chat` and `chat-small`.** Lost: it inspects documents only in the latest user message, so using it would put the sources back under the harm and jailbreak filters. Prompt Shields per source drops one source and keeps the answer; a platform block fails the turn.
- **Stricter thresholds (`Low`).** Lost: they block legitimate questions about documents on sensitive subjects. Medium is the service default; a threshold changes only through a reviewed policy change backed by `content_filter_triggered` data.
- **`Asynchronous_filter` mode, for lower latency.** Lost: streamed text could reach a user before the filter flags it. Synchronous mode means no unfiltered text reaches a user while the filtering system is up.
- **Profanity filter and custom blocklists.** Lost: no product need, and a term blocklist would put domain-specific data inside infrastructure, where configuration-over-code forbids it.
- **One policy, with the indirect-attack shield on everywhere.** Lost for the chat deployments (above). On `ingest-summary` the table is the only untrusted input, no standalone screening runs in ingestion, and a block costs one table's summary, never a user's turn, so the shield is on there.

## Consequences

- Easier: one Bicep module holds every filter value, reviewers see the thresholds, and PSRule stops a text deployment from shipping without a policy.
- A legitimate question can be refused (`question_filtered`, not retryable) and the user rephrases it. An answer that quotes long published passages from a source can trip protected-material detection (`answer_filtered`). Runbook R07 and the content-filter procedure in 86-operations.md handle false positives: review `content_filter_triggered` by project, read affected turns through just-in-time access, and change thresholds only in Bicep. The conversations runbook treats `content_filtered` on summary and memory calls as expected for some sessions and acts only above 5% of calls.
- While the filtering system is down, the chat deployments serve unfiltered text, as the service documents; the platform filter is a backstop that fails open, while source screening still fails closed (ADR 0062). `content_filter_degraded` makes the window visible.
- `load` has no account, so the policies are exercised only in `dev` and `prod`; the stub's `filter=prompt` and `filter=completion` directives exercise the apps' handling everywhere else.
- Tests (84-testing.md): unit, the finish-reason mapping covers `stop`, `length` with and without visible text, `content_filter`, a prompt `400 content_filter`, another `400` and a filter-error annotation; integration, a stub `400 content_filter` ends with `question_filtered`, a stub `finish_reason: content_filter` ends with `answer_filtered`, and that turn never appears in later history; deployed, a fixture jailbreak question ends with `question_filtered`; ingestion, the table-summary request carries the JSON-escaped table inside the delimiters, and content-filter results become `filtered` warnings; CI, PSRule `DocChat.OpenAI.DeploymentRules`.

## References

- Content filter document embedding (latest user message; document delimiters): https://learn.microsoft.com/en-us/azure/foundry-classic/openai/concepts/content-filter-document-embedding (checked 2026-10-14)
- Configure content filters (defaults of each filter; approval to turn harm filters off): https://learn.microsoft.com/en-us/azure/foundry-classic/openai/how-to/content-filters (checked 2026-10-14)
- Content filter names in a policy: https://learn.microsoft.com/en-us/azure/foundry-classic/foundry-models/how-to/configure-content-filters (checked 2026-10-14)
- Filter behaviour (`400 content_filter`, `finish_reason`, filtering system unavailable): https://learn.microsoft.com/en-us/azure/foundry-classic/foundry-models/concepts/content-filter (checked 2026-10-14 and 2026-10-18)
- Default safety policies (Medium on prompts and completions for text models): https://learn.microsoft.com/en-us/azure/foundry-classic/openai/concepts/default-safety-policies (checked 2026-10-18)
- `raiPolicies` Bicep reference, API 2026-07-01: https://learn.microsoft.com/en-us/azure/templates/microsoft.cognitiveservices/accounts/raipolicies (checked 2026-10-14)
- Deployments Bicep reference (`raiPolicyName`): https://learn.microsoft.com/en-us/azure/templates/microsoft.cognitiveservices/accounts/deployments (checked 2026-10-14)
