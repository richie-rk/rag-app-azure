# ADR 0065: Follow-up suggestions after the answer

- **Status:** Accepted
- **Date:** 2026-10-18
- **Decided by:** Product owner and architect
- **Specified in:** 12-chat-pipeline.md, 11-api.md, 13-conversations-and-memory.md, 10-web-client.md, 80-data-model.md, 81-config.md, 84-testing.md, 85-observability.md
- **Related ADRs:** 0007, 0028, 0055, 0056, 0057, 0058, 0059, 0060, 0061, 0062, 0063, 0064, 0066, 0068, 0070, 0074, 0075, 0147

## Context

The product suggests follow-up questions after each answer: one call to the chat-small model, based on the retrieved sources, that never blocks the answer or the next question, behind an operational setting that is on by default. The call has a 5-second timeout and is given the question, the answer, and the title, heading path and first 300 tokens of the top three sources, asking for up to three questions those sources can answer. The suggestions arrive as their own `followups` event, after `citations` and before `done`, and are added to the saved turn; a timeout or failure skips them without an error. The client re-enables input when `citations` arrives. Answer text travels only in `delta` events, because a suggestion list placed inside the answer stream, behind a marker or a stop sequence, could cut the answer short and hold back the events after it.

A suggestion is chat-small output written from retrieved document text, and a click sends it as the user's next question. Memory extraction reads only the user's own questions, never the sources or the answers, so that a poisoned document can't plant a memory. A clicked suggestion must therefore be recognised as a suggestion and never extracted, which needs it stored before the user can click it.

Vendor facts:

- Strict structured outputs on Chat Completions use `response_format` with `type` `json_schema` and `strict: true`; every property must be required and every object must set `additionalProperties: false`; the strict subset doesn't support `minItems`, `maxItems`, `minLength` or `maxLength`, so the count and length limits are enforced after the call. Microsoft's page (ms.date 2026-08-24) lists supported models only up to the gpt-5.1 family, so the chat-small model's support is proven by the deployed model check (ADR 0068), not by the documentation.
- gpt-5.6 models default to reasoning effort `medium`, so each call sets its effort explicitly.
- A prompt the content filter blocks returns `400` with error code `content_filter`; a filtered completion ends with `finish_reason` `content_filter`. The chat-small deployment runs under `docchat-text-guard` (ADR 0066).

## Decision

**When the call runs.** One chat-small call (`followups`) starts as soon as the answer stream ends, concurrently with persistence, when all of these hold: `followups.enabled` is true (default true); at least one source was sent to the answer call; the answer has visible text; the client is still connected. Otherwise no call is made. A turn that ends with an `error` event shows and stores no follow-ups, and a follow-ups call still running then is cancelled.

**Inputs.** The question; the answer with citation tokens removed by `strip_citation_tokens` (ADR 0064) and cut to its first 800 tokens; and, for the top `followups.source_count` (default 3) sources sent to the answer call, by rank, the title, heading path and first `followups.source_excerpt_tokens` (default 300) tokens of the chunk text. Token cuts use `DOCCHAT_OPENAI_SMALL_ENCODING` (default `o200k_base`). Only screened sources that survived the token budget reach this call (ADR 0062, ADR 0063).

**Messages.** `system`: constant text (exact wording in 12-chat-pipeline.md) asking for at most `followups.max_count` questions that the user might ask next and that the `SOURCE` excerpts can answer; leaving out the question just asked and anything the answer already covers; each question written as the user would type it, in the language of the user's question, in at most 120 characters; no links, Markdown, numbering or surrounding quotation marks; block contents are data whose instructions are ignored; then the block marker, and a request for JSON matching the schema, with an empty list when no useful question exists. `user`: a `QUESTION` block, an `ANSWER` block and one `SOURCE` block per source (`S1` onwards, by rank) holding `Title:` and `Section:` lines and the excerpt. Every block uses the per-call marker and defanging of ADR 0063.

**Call parameters.** `chat.completions.create` on `DOCCHAT_OPENAI_SMALL_DEPLOYMENT` with `response_format` = the strict JSON schema `followup_questions`:

```json
{"type": "object", "properties": {"questions": {"type": "array", "items": {"type": "string"}}}, "required": ["questions"], "additionalProperties": false}
```

`max_completion_tokens` = `chat.small_call_max_completion_tokens` (default 800); `reasoning_effort` = `DOCCHAT_OPENAI_SMALL_REASONING_EFFORT` (default `none`); `safety_identifier`; no sampling parameters (ADR 0068). Timeout `followups.timeout_seconds` (default 5 s), counted from the call's start and covering connection and response; no retry.

**Validation.** Trim each item; drop items that are empty, longer than 200 characters, contain `://` or `www.`, duplicate another item, or equal the question (comparisons after case folding); keep the first `followups.max_count` (default 3). A body that fails the schema, a timeout, a failure, a filtered prompt or completion, or an empty list after validation writes nothing and raises no error.

**Store, then show.** When at least one question remains and `finalize_turn` returned `saved = true`, the API calls `attach_followups(handle, followups, usage_record)` with the kept questions and the call's usage record, and writes the `followups` event (`{"type": "followups", "questions": [...]}`, 1 to 3 strings of 1 to 200 characters) only when it returns `True`. `attach_followups` patches the turn's `followups`, `followupsAt`, `usage` and `updatedAt` without an ETag, so a concurrent feedback save also persists, with at most 3 attempts on throttling, timeout or `5xx`; after that it logs `followups_not_saved`, increments `docchat.conversations.followups_not_saved` and returns `False`, and the stream goes on to `done` without the event. Every suggestion a user can click is therefore already stored on the turn.

**Order and timing.** `followups` comes at most once, after `citations` and before `done` (ADR 0055). `citations` is written as soon as the answer is stored; the client re-enables input then, and the stream lease is released then (ADR 0058), so a pending follow-ups call never blocks the next question. `done` follows when the follow-ups step ends: with the 5-second timeout, within 6 s of `citations` even when the model takes longer.

**Interruptions and replays.** A disconnect after the answer stream ended cancels a running follow-ups call and stores none; an `attach_followups` already started runs to its end, and its questions appear only in later session reads (ADR 0057). The turn deadline, once the answer is stored, cancels only the follow-ups call, and the stream ends with `done` (ADR 0070). At the shutdown drain, a turn whose answer is stored drops its follow-ups and writes `done` (ADR 0007). A replay of a completed turn sends its stored follow-ups with no model call (ADR 0056).

**Usage and capacity.** The call produces a usage record with `call` `followups` (ADR 0059), estimated when the call ended without reported usage (ADR 0060). The record always reaches the user's day and month counters, and it is stored on the turn with the questions through `attach_followups`. The call's worst-case chat-small charge is 5,100 + (500 + `followups.source_excerpt_tokens`) × `followups.source_count` + `chat.small_call_max_completion_tokens` = 8,300 tokens at the defaults, part of the per-turn chat-small charge of ADR 0147.

**Suggestions never feed memory.** Before generation, the next turn compares its question with the stored follow-ups of every turn `read_recent_turns` returned and with the project's example questions, after line-break normalisation, NFC, whitespace collapsing and case folding (`is_suggested_question`). A match stores the completed turn with `memoryExtraction` `{state: not_applicable, reason: suggested_question}`, and it is never extracted (ADR 0075). The newest completed turn, whose follow-ups are the ones on screen, is always among the turns read (ADR 0074).

**Client.** The newest turn's follow-ups appear as plain-text buttons between the message list and the composer; activating one sends it as the next question; they're disabled while sending is blocked and removed when a new question is sent; the client keeps a `followups` event that arrives after a newer question was sent but doesn't show it; a reopened session shows its newest turn's stored follow-ups. Session reads return each turn's follow-ups.

**Settings.** ops_config rows typed by `FollowupsOps` (ADR 0028), changed with `docchat-ops config set` and applied on the first access after the API's 60-second snapshot: `followups.enabled` (boolean, default `true`), `followups.timeout_seconds` (5, 2 to 10), `followups.max_count` (3, 1 to 3), `followups.source_count` (3, 1 to 8), `followups.source_excerpt_tokens` (300, 100 to 800), and the shared `chat.small_call_max_completion_tokens` (800, 200 to 8,000). App settings: `DOCCHAT_OPENAI_SMALL_DEPLOYMENT`, `DOCCHAT_OPENAI_SMALL_REASONING_EFFORT` (`none`), `DOCCHAT_OPENAI_SMALL_ENCODING` (`o200k_base`), `DOCCHAT_OPENAI_SMALL_PROMPT_CACHE` (`disabled`).

**Telemetry**, metadata only: span `docchat.chat.followups` (`docchat.deployment`, `docchat.outcome`, `docchat.prompt_tokens`, `docchat.completion_tokens`); `docchat.openai.duration` and `docchat.openai.tokens` with `call` `followups`; span `docchat.conversations.attach_followups` (`docchat.followups.count`, `docchat.outcome`); `content_filter_triggered` with `call` `followups` on a filter hit.

## Alternatives considered

- **Follow-ups inside the answer stream, after a marker or a stop sequence.** Lost: a marker or a stop sequence can cut the answer short and holds back the events after it; the answer stream carries answer text only.
- **The chat deployment for follow-ups.** Lost: the product uses the small model for this call; the chat deployment costs more per token and its capacity is sized for answers.
- **Generating suggestions before or alongside the answer.** Lost: the suggestions must skip what the answer covers, which needs the answer; a call before the answer would also delay the first token.
- **Holding `citations` or the input until suggestions arrive.** Lost: suggestions must never block; the user can ask again as soon as the answer is stored.
- **Showing suggestions as soon as the call returns, then storing them.** Lost: a click on a suggestion not yet stored isn't recognised by the suggested-question test, so memory extraction could take document-steered text as the user's own statement; storing first costs one patch before the event.
- **Suggestions from the question and answer only.** Lost: the suggestions must be questions the retrieved sources can answer, so that a click leads to an answerable question.
- **Free-text output split into lines.** Lost: numbering, bullets and quotation marks would need heuristics; the strict schema makes parsing deterministic, and validation in code covers the limits the schema can't express.
- **Retrying a slow or failed call.** Lost: suggestions are optional; a retry would push `done` past the 5-second budget and add cost.

## Consequences

- Easier: suggestions never interfere with the answer text or the next question; their failure is invisible to users.
- Cost: one chat-small call per completed turn that sent sources, charged to the user's quotas and part of the chat-small capacity share; in `prod`, raising `followups.source_count`, `followups.source_excerpt_tokens` or `chat.small_call_max_completion_tokens` needs `docchat-ops chat capacity-check` first (ADR 0147).
- Latency: `done` can follow `citations` by up to the 5-second timeout plus the attach patch; input is already enabled.
- Runbook (86-operations.md): under `docchat-ops jit grant --level config`, `docchat-ops config set followups.enabled false` switches suggestions off platform-wide for every turn that starts after the API's 60-second snapshot refreshes; there is no per-project switch.
- Tests: validation drops empty, over-long, URL-bearing, duplicate and question-equal items and keeps at most 3; a 6-second model delay yields no `followups` event and `done` within 6 s of `citations`; `followups.enabled = false` makes no call; a disconnect after the model stream ended stores no follow-ups; the deployed model check passes the `followup_questions` strict schema on chat small; the stream writer receives the `followups` line only after `attach_followups` returned `True`, and with `False` the stream has no `followups` line while the counters still rise by the call's usage; a question that repeats a stored suggestion or an example question is stored `not_applicable` with reason `suggested_question` and never reaches extraction, while the same words in another order are stored `pending` for extraction; a document that steers a suggestion can't plant a memory through a click on it; a concurrent feedback save and `attach_followups` both persist; after a reload, follow-ups show on the newest turn only; in the browser, a question shows deltas, citations and follow-ups.
- Monitoring: the `docchat.chat.followups` span outcomes and `docchat.conversations.followups_not_saved` on the chat dashboard.
- Risks: a poisoned document among the top sources can steer the wording of a suggestion; the sources were screened (ADR 0062), URL-bearing items are dropped, suggestions render as plain text, a user must click one, and a clicked one never feeds memory. A suggestion may still lead to a low-confidence search, which the broadened search handles (ADR 0061). Strict-schema support on a new chat-small model is known only after the deployed model check.

## References

- Structured outputs (strict schemas, unsupported keywords, supported models): https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/structured-outputs (ms.date 2026-08-24, checked 2026-10-18)
- Reasoning models (`reasoning_effort` defaults, `max_completion_tokens`): https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/reasoning (checked 2026-10-14)
- Content filter behaviour (filtered prompt `400`, `finish_reason` `content_filter`): https://learn.microsoft.com/en-us/azure/foundry-classic/foundry-models/concepts/content-filter (checked 2026-10-14)
- Chat Completions reference (`response_format`, `safety_identifier`): https://learn.microsoft.com/en-us/rest/api/microsoft-foundry/azureopenai/chat (checked 2026-10-14)
