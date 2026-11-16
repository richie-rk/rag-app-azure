# ADR 0060: Usage of cancelled calls is counted

- **Status:** Accepted
- **Date:** 2026-10-18
- **Decided by:** Architect
- **Specified in:** 12-chat-pipeline.md, 13-conversations-and-memory.md, 80-data-model.md, 81-config.md, 84-testing.md, 85-observability.md
- **Related ADRs:** 0006, 0007, 0055, 0057, 0058, 0059, 0063, 0068, 0070, 0074, 0075, 0081

## Context

- Each user's day and month token quotas are counted from actual model usage on every call, and the product records token usage and query cost for every call from its first version (ADR 0059). When the client disconnects, the API cancels the model call within 1 s and saves the turn as cancelled with the partial answer (ADR 0057).
- Vendor fact (checked 2026-10-14): a streamed Chat Completions call receives its usage only in a final chunk, sent when the request sets `stream_options.include_usage`, and "If the stream is interrupted, you may not receive the final usage chunk". A non-streamed call that is cancelled or times out returns no body, so it reports nothing. Reasoning tokens are billed as output and are reported only in the usage, so a call cut short hides them too.
- So the calls a user can cut short at will, by closing the tab or pressing Stop, are exactly the calls with no reported usage. Counting them as zero would let a scripted client generate unmetered tokens and would under-state cost, so a call cut short must still count, at a figure the API can compute itself.
- The API already measures every prompt with the deployment's own tokenizer: prompt assembly counts the answer prompt against its budget (ADR 0063), and the encodings are app settings loaded from the image's cache, the chat and chat-small ones checked against the service's reported prompt tokens to within 1% (ADR 0068). Microsoft's counting helper counts 3 tokens per message plus 3 for reply priming.

## Decision

1. **Usage is requested on every call that can report it.** The one streamed call, `answer`, sends `stream_options={"include_usage": true}` and reads the final chunk; the non-streamed calls (`rewrite`, `search_passage`, `followups`, `summary`, `memory` and the embeddings) read `usage` from the response body (ADR 0059).
2. **A call that ends without reported usage records an estimate.** This covers every such ending: the client disconnected and the call was cancelled, a per-call timeout or the turn deadline ended it, the shutdown drain cancelled it, the service refused a filtered prompt, or the call failed before or during its output. Its `ModelCallUsage` record holds:
   - `promptTokens`: the prompt as sent, counted with the deployment's configured encoding by the message-counting rule of ADR 0063 (3 tokens per message plus the tokens of its content, plus 3 for reply priming). The `answer` call reuses the count that prompt assembly already made; the chat-small calls count their messages with `DOCCHAT_OPENAI_SMALL_ENCODING`; an embeddings call counts its input text with its model's `encoding` from the embedding catalogue `DOCCHAT_OPENAI_EMBEDDING_MODELS` (`cl100k_base` for `text-embedding-3-large`), with no per-message tokens.
   - `completionTokens`: the tokens of the visible text received from the model before the call ended, counted with the same encoding: the content deltas as they arrived, held-back marker text included, before citation markers become citation tokens. A non-streamed or embeddings call that ended without a response has 0.
   - `reasoningTokens`, `cachedTokens` and `cacheWriteTokens`: 0. `totalTokens` = `promptTokens` + `completionTokens`.
   - `estimated`: `true`. `outcome`: `cancelled` (a disconnect or the drain), `timed_out` (a per-call timeout or the turn deadline), `filtered` (a refused prompt) or `failed` (any other error).
   - `estimatedCostMicros`: ADR 0059's formula on these counts, so `promptTokens` × `inputPerMillion` + `completionTokens` × `outputPerMillion`, rounded half-up; `null` when the model has no rate.
3. **It counts like any record.** The estimate goes through `docchat_api.usage.record_model_call(oid, record)`: the day and month counters receive their `incr` patches, `estimatedCalls` included (ADR 0058), so the user's quota and the cost totals rise by exactly the estimated amounts. The record joins the turn's `usage.calls` when the turn stores that call (ADR 0059); the turn's `usage.totals.estimated` becomes `true`, and `done.usage.estimated` carries it when the stream still ends with `done`. The conversation store's `summary` and `memory` calls record their estimates the same way, whether or not their output is used.
4. **Cancellation can't skip it.** A record, estimated or not, is built as soon as its call ends, a cancelled call included, by a shielded task that counts the estimate in one `asyncio.to_thread` call (so a long partial answer never blocks the event loop, ADR 0006) and starts the two counter writes, each a shielded task (ADR 0058). On the chat path the turn's shielded finaliser, with its 15-second budget, awaits them before the turn task ends (ADR 0057).
5. **No new setting.** The rule uses the encodings that ADR 0068 defines: `DOCCHAT_OPENAI_CHAT_ENCODING` and `DOCCHAT_OPENAI_SMALL_ENCODING` (default `o200k_base`) and the `encoding` of each `DOCCHAT_OPENAI_EMBEDDING_MODELS` entry.

## Alternatives considered

- **Count nothing when usage is missing.** A client could cancel every stream before its usage chunk and generate tokens that no quota or cost total sees.
- **Keep reading after a disconnect until the usage chunk arrives.** The model would keep generating, and billing, for nobody; a disconnect must cancel the model call, and ADR 0057 does so within 1 s.
- **Charge the whole allowance, the prompt plus `max_completion_tokens`.** It over-counts every stopped answer by up to 12,000 tokens, so a user who stops a long answer loses quota for text that was never generated.
- **Estimate from characters (about 4 per token).** The ratio varies widely by language and for code, while the exact tokenizer is already loaded for the token budget.
- **Ask the service for the figure afterwards.** The Chat Completions calls used here report usage only in their own response, and an interrupted stream may never deliver it.

## Consequences

- Disconnecting, stopping or timing out never hides usage: those calls count against the user's day and month quotas and appear in the cost totals.
- The estimate under-counts reasoning tokens spent before the cut, which only the missing usage reports, by at most the call's `max_completion_tokens` less the visible tokens counted; by default that cap is 12,000 for the answer, 800 for the `rewrite`, `search_passage` and `followups` calls, 2,000 for the summary and 1,200 for memory extraction. It also counts no cached or cache-write tokens, which matches the default configuration, where prompt caching is off (ADR 0068).
- It can over-count a call that produced no billable work, such as a throttled or unreachable request, which is charged at its measured prompt: an error on the quota's side, accepted so that no ending of a call goes uncounted.
- Every estimate is marked: `estimated` on the record, `usage.totals.estimated` on the turn, `estimatedCalls` on the day and month counters and `estimated` in `done.usage`, so dashboards and investigations can tell measured from estimated usage.
- Estimates follow the encoding settings; the tokenizer check of ADR 0068 keeps the chat and chat-small counts within 1% of the service's prompt counts.
- Tests: a missing usage chunk yields an estimated record; a disconnect before the usage chunk gives an estimated `answer` record with the measured prompt tokens and the tokenised partial completion, `estimated = true`, and the day and month counters rise by exactly those amounts.

## References

- Azure OpenAI Chat Completions reference (`stream_options.include_usage`; an interrupted stream may lack the final usage chunk): https://learn.microsoft.com/en-us/rest/api/microsoft-foundry/azureopenai/chat (checked 2026-10-14)
- Azure OpenAI v1 API reference (the same warning on the usage chunk): https://learn.microsoft.com/en-us/azure/ai-foundry/openai/latest (checked 2026-10-14)
- Reasoning models (reasoning tokens billed as output and reported in the usage): https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/reasoning (checked 2026-10-14)
- Token counting for chat messages (3 tokens per message, 3 for reply priming): https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/chatgpt (checked 2026-10-14)
