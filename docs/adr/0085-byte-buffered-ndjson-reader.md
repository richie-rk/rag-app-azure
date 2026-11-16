# ADR 0085: Byte-buffered NDJSON reader

- **Status:** Accepted
- **Date:** 2026-10-21
- **Decided by:** Architect
- **Specified in:** 10-web-client.md, 12-chat-pipeline.md, 84-testing.md
- **Related ADRs:** 0004, 0013, 0055, 0056, 0057, 0058, 0064, 0072, 0083, 0084, 0088, 0090

## Context

- The API streams each answer as `application/x-ndjson`, one UTF-8 JSON object per line ending in `\n`, with eight event types: `meta`, `status`, `delta`, `citations`, `followups`, `heartbeat`, `error` and `done`, in the order `meta`, any `status`, the `delta` events, `citations`, any `followups`, then `done`, with `heartbeat` anywhere (after 15 seconds without output) and exactly one terminal event (ADR 0055). A stream that ends without a terminal event is a failure the client must show. The API finalises a completed turn before it writes `citations`, and the client re-enables input when `citations` arrives.
- The web client reads the stream with `fetch` and validates every event at run time against the schemas generated from the OpenAPI document (ADR 0013). A line split across network chunks must never be lost, follow-up suggestions come only from the `followups` event, failures before the stream and `error` events show as failures, and a truncated answer says so.
- Fetch facts (checked 2026-10-21): a response body is read as `Uint8Array` chunks whose boundaries need not fall on line boundaries, so a line, and a 2-, 3- or 4-byte UTF-8 character, can be cut anywhere; a `TextDecoder` created with `fatal: true` throws a `TypeError` on malformed input instead of inserting U+FFFD.
- The largest event, `citations`, holds at most 16 entries and 50 links of 2,048 characters. The API deploys before the web client, so for a short time a newer API can talk to an older page.

## Decision

1. **The request.** `POST {apiBaseUrl}/v1/chat/turns` with the JSON body `{ "projectId", "sessionId", "clientTurnId", "question" }`: `sessionId` omitted for a new session, `clientTurnId` a new `crypto.randomUUID()` for every send, retries included. Headers as for every API call (bearer token, `traceparent`, ADR 0088) with `Accept: application/x-ndjson`; the `fetch` carries an `AbortController` signal. A `200` whose `Content-Type` starts with `application/x-ndjson` goes to the reader; a `200` of another type is `stream_unreadable`; any other status is a pre-stream failure read from its problem details.
2. **The reader** (`src/chat/stream/ndjson.ts`). It reads `response.body.getReader()` and splits each chunk at every byte `0x0A` before decoding, joining a line's pieces across chunks. For each complete line: drop one trailing `0x0D`; skip an empty line; a line longer than 1,048,576 bytes is `stream_unreadable`; restart the stall timer; decode with `new TextDecoder('utf-8', { fatal: true })` (a throw is `stream_unreadable`); `JSON.parse` (a throw is `stream_unreadable`); a value that isn't an object, or whose `type` isn't a string, is `stream_unreadable`; a `type` outside the eight is skipped; otherwise `safeParse` with the generated Zod schema of that type (a failure is `stream_unreadable`) and apply the event. More than 1,048,576 bytes pending without a newline is `stream_unreadable`. When the body ends: bytes left without a final `0x0A` are `stream_unreadable`, and no terminal event is `stream_incomplete`. A read that rejects is `stream_interrupted`; the user's stop ends the turn as stopped. Zod runs jitless (ADR 0083).
3. **The state machine** (`src/chat/stream/events.ts`). Each turn is `awaitingMeta`, `answering`, `cited`, `done` or `failed`:

   | State | Allowed (effect) | Anything else |
   |---|---|---|
   | `awaitingMeta` | `heartbeat`; `meta` (to `answering`); `error` (to `failed`) | `stream_unreadable` |
   | `answering` | `heartbeat`; `status` at most once; `delta` (append text); `citations` (to `cited`, sending unblocked); `error` (to `failed`) | `stream_unreadable` |
   | `cited` | `heartbeat`; `followups` at most once; `done` (to `done`); `error` (to `failed`, the answer and its sources stay visible) | `stream_unreadable` |

   `meta.sessionId` must equal the request's `sessionId` when one was sent. After `done` or `error` the client cancels the reader and ignores anything further. Every turn gets exactly one terminal outcome.
4. **Applying events.** `meta` stores the turn and session IDs and, for a new chat still on screen, replaces the location with `/chat/<sessionId>` without remounting; `status` `search_broadened` changes the progress line to "Searching more broadly for better matches" and announces it; `delta` appends text and schedules a render (at most every 100 ms, ADR 0084); `citations` stores the citations and the normalised `allowedLinks`, unblocks sending and renders at once; `followups` stores up to 3 questions and shows them while the turn is the session's newest; `done` stores `truncated` and validates `usage` as the generated `TurnUsageSummary` without showing it; `error` records `{ code, message, correlationId }`. The session queries are refreshed after a terminal outcome.
5. **Stall timeout.** A 45,000 ms timer starts when the request is sent and restarts on every complete line, heartbeats included; when it fires, the client aborts the request and the turn fails as `stream_stalled`. It also bounds the wait for response headers.
6. **Stop, retry and display.** The stop button, or Escape while it shows, aborts the request; the partial answer and any sources stay, the API saves the turn `cancelled`, and stop isn't offered after `citations`. Try again on the session's newest failed or stopped turn sends the same question with a new `clientTurnId` in the same session, or as a new session when the turn never received `meta`; it is offered when an `error` event's `retryable` is true, for every failure the client detects itself, and for pre-stream failures except `409 session_turn_limit`, and never for `question_filtered` or `answer_filtered`. A `409 turn_in_progress` that arrives within 10 s after this tab's previous turn in the session reached `cited` is resent identically (same `clientTurnId`) after 1,000 ms, at most 2 times. An `error` event shows its message and `correlationId`; `stream_incomplete`, `stream_stalled`, `stream_unreadable`, `stream_interrupted` and network failures show the client's own text (`chat.error.cutOff`, `chat.error.stalled`, `chat.error.unreadable`, `chat.error.network`) with `meta.correlationId` when `meta` arrived, otherwise the request's trace ID; a `done` with `truncated` shows the truncated caption under the answer.
7. **Streams outlive navigation.** Live turns sit in `src/chat/stream/turnStore.ts`, keyed by `clientTurnId` and, from `meta` on, by `sessionId`; opening another session or another screen doesn't stop them, and the session list marks a session whose answer is still streaming. While any turn is before its terminal outcome, or an upload runs, a `beforeunload` handler asks the browser to confirm leaving; closing or reloading the tab ends the stream and the API saves the turn `cancelled`.

## Alternatives considered

- **Server-sent events with `EventSource`.** Rejected: `EventSource` sends only `GET` and can't carry an `Authorization` header, and the protocol is NDJSON (ADR 0055).
- **A streaming text decoder, splitting on `\n` in text.** Rejected: it handles split characters too, but splitting bytes first decodes each line once, whole, with fatal errors, and makes the 1 MiB cap exact in bytes.
- **Failing on unknown event types.** Rejected: during a deploy a newer API would break every open page; skipping keeps the reader forward-compatible, while known types still validate strictly.
- **Skipping lines that fail validation.** Rejected: a damaged known event could lose citations or the terminal event without anyone noticing.
- **No stall timer.** Rejected: a silent connection can hang for minutes; with a heartbeat after 15 s of silence, 45 s is three missed heartbeats.
- **Ending streams when the user navigates away.** Rejected: people switch chats while they wait, and the API's stream leases bound how many run at once (ADR 0058).

## Consequences

- A line split anywhere, inside a multi-byte character included, parses; a stream that ends early or goes silent is shown as a failure with a reference ID; a partial answer is never shown as complete.
- Memory per turn is bounded by 1 MiB of pending bytes plus the answer text.
- A new event type can ship in the API first; a new field or enum value inside a known event must reach the generated schemas and the client first (ADR 0013).
- Obligations (84-testing.md): unit tests that every recorded stream in the shared `tests/fixtures/streams/*.ndjson` (2-, 3- and 4-byte UTF-8 characters and a 70 KB line) parses to the same events when its bytes are split at every offset into two chunks and when sent one byte per chunk; that `\r\n` endings parse, empty lines are skipped and an unknown `type` is skipped; that a body ending after `delta` gives `stream_incomplete` and a final line without `\n` gives `stream_unreadable`; that invalid UTF-8, invalid JSON, a known event failing its schema, `delta` before `meta`, a second `citations`, `done` before `citations` and a 1,048,577-byte line each give `stream_unreadable`; that after `done` a further line is ignored and the reader cancelled; that a `done` whose `usage` lacks `totalTokens` gives `stream_unreadable`; with fake timers, that 45 s without a line gives `stream_stalled` while heartbeats every 15 s keep a 3-minute stream alive; component and browser tests for blocking until `citations`, stop, Try again with a new `clientTurnId`, the bounded `409 turn_in_progress` resend, the broadened-search line and the new chat's URL after `meta`; at least 95% branch coverage in `src/chat/stream/`.

## References

- `ReadableStreamDefaultReader.read()` (fetch chunks as `Uint8Array`, lines across chunks): https://developer.mozilla.org/en-US/docs/Web/API/ReadableStreamDefaultReader/read (checked 2026-10-21)
- `TextDecoder.fatal`: https://developer.mozilla.org/en-US/docs/Web/API/TextDecoder/fatal (checked 2026-10-21)
- Zod jitless mode: https://zod.dev/compile (checked 2026-10-14)
- `@hey-api/openapi-ts` Zod plugin (generated event schemas): https://heyapi.dev/docs/openapi/typescript/plugins/zod/v4 (checked 2026-10-14)
