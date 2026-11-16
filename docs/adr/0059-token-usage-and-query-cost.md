# ADR 0059: Token usage and query cost

- **Status:** Accepted
- **Date:** 2026-10-18
- **Decided by:** Product owner and architect
- **Specified in:** 11-api.md, 12-chat-pipeline.md, 13-conversations-and-memory.md, 14-ingestion.md, 80-data-model.md, 81-config.md, 84-testing.md, 85-observability.md, 86-operations.md
- **Related ADRs:** 0014, 0028, 0055, 0056, 0057, 0058, 0060, 0061, 0062, 0068, 0071, 0074, 0075, 0100, 0138, 0147

## Context

- The product records query cost and token usage from its first version. Every turn stores the token usage of each model call and the deployments used, and its usage is added to the user's day and month counters, from which the token quotas are counted (ADR 0058): answers, standalone search queries, broadened-search passages, follow-ups, summaries and memory extraction all count. The `done` event tells the client what the turn used. Operators need cost and token dashboards beside the monthly budget.
- Answering one question takes up to six Azure OpenAI calls on three deployments: the standalone search query, the query embedding, the search passage and its embedding when the search is broadened, the answer, and follow-ups. The conversation store adds the summary and memory calls after the response. The deployments run reasoning models (`gpt-5.6-sol`, `gpt-5.6-terra`) and `text-embedding-3-large` (ADR 0100), whose prices differ by model and by token kind, so a usable cost needs counts per call and per token kind, not one total.
- An investigation of any answer needs to find which deployments it used and what each call consumed, next to the stored model input (ADR 0071).
- Vendor facts (Azure OpenAI, checked 2026-10-14 unless stated). Chat Completions reports `usage.prompt_tokens`, `usage.completion_tokens`, `usage.prompt_tokens_details.cached_tokens` and `usage.completion_tokens_details.reasoning_tokens`; reasoning tokens are billed as output, count inside `completion_tokens` and are capped by `max_completion_tokens`. A streamed call receives usage only in a final chunk, with empty `choices`, when it sends `stream_options.include_usage`. GPT-5.6 deployments can charge cache writes, reported as `usage.prompt_tokens_details.cache_write_tokens` (prompt-caching how-to, checked 2026-10-18). An embeddings response reports `usage.prompt_tokens`. The Azure OpenAI pricing page lists each model's price per 1,000,000 tokens for input, cached input, cache writes (on GPT-5.6) and output, per deployment type (checked 2026-10-18).

## Decision

**Scope.** Every Azure OpenAI call the API makes produces one `ModelCallUsage` record, its retries included: the chat pipeline's `rewrite` (the standalone search query), `query_embedding`, `search_passage`, `passage_embedding`, `answer` and `followups`, and the conversation store's `summary` and `memory`. Every record goes through one recorder, `docchat_api.usage.record_model_call(oid, record)`. A replay of a completed turn makes no call and records nothing (ADR 0056). The ingestion app's calls run on its own deployments, outside user quotas: each ingestion run item records `embedding_tokens`, `summary_input_tokens`, `summary_output_tokens` and `docintel_pages`, unpriced in the first version.

**The record.** A Pydantic model in `docchat_core`, camelCase in Cosmos DB:

| Field | Type | Value |
|---|---|---|
| `call` | string | `rewrite`, `search_passage`, `answer`, `followups`, `query_embedding`, `passage_embedding`, `summary` or `memory` |
| `deployment`, `model` | string | the deployment name and its model name, from the app settings or, for embeddings, the physical index record |
| `promptTokens`, `completionTokens`, `reasoningTokens`, `cachedTokens`, `cacheWriteTokens` | integer ≥ 0 | from the response `usage` (below); `completionTokens` includes reasoning tokens |
| `totalTokens` | integer ≥ 0 | `promptTokens` + `completionTokens`; the figure the quotas compare (ADR 0058) |
| `estimated` | boolean | `true` when the call ended without reported usage and the counts were estimated (ADR 0060) |
| `estimatedCostMicros` | integer ≥ 0 or null | the cost below; `null` when the model has no rate |
| `durationMs` | integer ≥ 0 | from the call's start to its end |
| `outcome` | string | `ok`, `filtered`, `failed`, `cancelled` or `timed_out` |

**Reading usage.** `promptTokens` = `prompt_tokens`, `completionTokens` = `completion_tokens`, `cachedTokens` = `prompt_tokens_details.cached_tokens`, `cacheWriteTokens` = `prompt_tokens_details.cache_write_tokens`, `reasoningTokens` = `completion_tokens_details.reasoning_tokens`. A field the response doesn't report counts as 0, which covers missing detail objects and every completion field of an embeddings response. The streamed `answer` call sends `stream_options={"include_usage": true}` and reads the final chunk; the non-streamed calls read `usage` from the response body.

**Estimated cost.** With the four rates of `pricing.model_rates[model]` read as decimal numbers (`decimal.Decimal`, never binary floating point), in currency units per 1,000,000 tokens:

`estimatedCostMicros = (promptTokens − cachedTokens − cacheWriteTokens) × inputPerMillion + cachedTokens × cachedInputPerMillion + cacheWriteTokens × cacheWritePerMillion + completionTokens × outputPerMillion`

Tokens times a rate per million tokens is directly in micro-units, 10⁻⁶ of `pricing.currency`; the result is rounded half-up to an integer. Units check: 1,000 tokens at a rate of 1.75 per million are 1,750 micro-units, 0.00175 of the currency. A model with no entry yields `null` and increments `docchat.pricing.rate_missing` (attribute `model`).

**Price table** (typed ops_config rows in `PricingOps`, ADR 0028, changed only with `docchat-ops config set`, live on the first access after the API's 60-second snapshot):

- `pricing.currency`: string, default `USD`, three upper-case letters (ISO 4217).
- `pricing.model_rates`: object, default `{}`, at most 20 keys; each key a model name matching `^[a-z0-9][a-z0-9.\-]{0,63}$` (the form of `DOCCHAT_OPENAI_CHAT_MODEL`, `DOCCHAT_OPENAI_SMALL_MODEL` and the embedding model names); each value `{inputPerMillion, cachedInputPerMillion, cacheWritePerMillion, outputPerMillion}`, each a decimal string from 0 to 1000 with at most 6 decimal places.
- Operators take the rates for the deployment type, region and currency in use from the Azure OpenAI pricing page, write them with `docchat-ops config set`, and confirm that `docchat.pricing.rate_missing` stays 0 for an hour. The model-change runbook sets a new model's rate before traffic reaches it, and `prod` opens to users only when every deployed model has a rate, which a deployed acceptance test checks.

**Billable operations that aren't priced.** Each turn records `usage.billable.semanticQueries` (semantic-ranker queries, ADR 0061), `usage.billable.shieldCalls` and `usage.billable.shieldCharacters` (Prompt Shields calls and characters, ADR 0062), integers ≥ 0; a memory extraction records its own Prompt Shields calls in `memoryExtraction.shieldCalls` (ADR 0075).

**Where records go.**

- The turn's `usage` (`TurnUsage`, never returned by session reads): `calls`, at most 10 records in call order; `totals` = `{promptTokens, completionTokens, reasoningTokens, cachedTokens, cacheWriteTokens, totalTokens, estimatedCostMicros, unpricedCalls, estimated}` over `calls`, where `estimatedCostMicros` is the sum, or `null` when any call had no rate, and `estimated` is true when any record is; and `billable`. Finalisation stores the calls made so far (ADR 0057); `attach_followups(handle, followups, usage_record)` appends the follow-ups record and recomputes `totals`.
- The summary call's record goes to `session.summary.lastUsage` (ADR 0074) and the memory call's to `turn.memoryExtraction.usage` (ADR 0075).
- Every record reaches the user's day and month counters through `incr` patches on its five token fields, `totalTokens` and `calls`, on `estimatedCostMicros` when the cost is known or `unpricedCalls` when it isn't, and on `estimatedCalls` when the record is estimated, each write a shielded task (ADR 0058). A follow-ups record that `attach_followups` couldn't store still reaches the counters.
- The client receives only `done.usage` (`TurnUsageSummary`: `promptTokens`, `completionTokens`, `reasoningTokens`, `cachedTokens`, `totalTokens`, `estimated`) over the turn's calls so far (ADR 0055); never a cost, a deployment or a per-call record.

**Telemetry** (metadata only, ADR 0014): counter `docchat.openai.tokens` (attributes `deployment`, `call`, `kind`: `prompt`, `completion`, `reasoning`, `cached`, `cache_write`); counter `docchat.openai.estimated_cost` in micro-units (`deployment`, `model`); counter `docchat.pricing.rate_missing` (`model`); token attributes on the `docchat.chat.generate`, `docchat.chat.rewrite`, `docchat.chat.search_passage` and `docchat.chat.followups` spans; the log record `chat_turn_finished` with the turn's token totals and estimated cost; the `budget` workbook (estimated cost and tokens per deployment per day, models without a price, prompt tokens per turn); the alert `prices-missing` in `prod` (any `docchat.pricing.rate_missing` within an hour, severity 3).

## Alternatives considered

- **One token total per turn.** It can't price cached, cache-write and reasoning tokens at their own rates, and it hides which call and deployment used the capacity, which the capacity rule of ADR 0147 and the throttling runbook need.
- **Cost from Azure Cost Management.** It reports actual cost per resource and meter, never per user, project or turn, so it can't price a query. It stays the monthly budget's job.
- **Prices as code constants.** Prices differ by deployment type, region and currency and change over time; every change would need a release. A typed ops_config row is validated, audited and live within 60 seconds.
- **Cost as floating-point currency.** Rounding errors accumulate over millions of additions to the same counter; integer micro-units add exactly with Cosmos DB `incr`.
- **Tokens only, priced later in the workbook.** The rate in force when a call ran would be lost once rates change. Storing both keeps each call's estimate and the counts needed to re-price.
- **Pricing search and screening per turn now.** Model calls dominate a query's cost and can be priced per call; the recorded counts let a later cost model price semantic queries and Prompt Shields without a data migration.

## Consequences

- Every turn carries its usage and estimated cost per call; each user's day and month counters carry the same totals; metrics break both down by deployment, model and call. Quotas and cost come from the same records.
- The cost is an estimate at list prices from ops config. Discounts, commitments, taxes and currency conversion aren't reflected; the Azure budget measures actual spend (ADR 0058).
- A rate change affects only records written after it; stored costs keep the rate of their time, and the token counts stay for re-pricing. The day and month counters carry no currency field, so a month in which `pricing.currency` changes mixes two currencies in its cost total.
- A model without a rate leaves its calls' cost `null`, counts them in `unpricedCalls` and fires `prices-missing` in `prod`.
- Cache-write accounting depends on the service reporting `cache_write_tokens`; prompt caching is off by default (ADR 0068), so these counts are normally 0.
- Obligations: the `pricing.currency` and `pricing.model_rates` rows; the price-maintenance runbook step and the model-change runbook's price step; tests that a record built from a usage chunk carries cached, cache-write and reasoning tokens, that cost is in micro-units with half-up rounding, and that a model without a rate gives `null` and increments `docchat.pricing.rate_missing`; that 50 concurrent counter updates for one user give exact day and month totals; that a follow-ups record still reaches the counters when `attach_followups` returns `False`; that session reads never contain `usage`; and a deployed acceptance test in `prod` that every deployed model has a rate and `docchat.pricing.rate_missing` stays 0 for one hour of traffic.

## References

- Azure OpenAI Chat Completions reference (`usage`, `stream_options.include_usage`, the final usage chunk): https://learn.microsoft.com/en-us/rest/api/microsoft-foundry/azureopenai/chat (checked 2026-10-14)
- Azure OpenAI v1 API reference: https://learn.microsoft.com/en-us/azure/ai-foundry/openai/latest (checked 2026-10-14)
- Reasoning models (reasoning tokens billed as output, `completion_tokens_details.reasoning_tokens`): https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/reasoning (checked 2026-10-14)
- Prompt caching (cache writes on GPT-5.6): https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/prompt-caching (checked 2026-10-18)
- Azure OpenAI pricing (per-model rates per 1,000,000 tokens for input, cached input, cache writes and output): https://azure.microsoft.com/en-us/pricing/details/cognitive-services/openai-service/ (checked 2026-10-18)
- Azure Cosmos DB partial document update (`incr`): https://learn.microsoft.com/en-us/azure/cosmos-db/partial-document-update (checked 2026-10-14)
