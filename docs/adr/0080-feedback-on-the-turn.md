# ADR 0080: Feedback on the turn

- **Status:** Accepted
- **Date:** 2026-10-18
- **Decided by:** Product owner and architect
- **Specified in:** 13-conversations-and-memory.md, 80-data-model.md, 11-api.md, 81-config.md, 10-web-client.md, 85-observability.md, 84-testing.md, 86-operations.md
- **Related ADRs:** 0012, 0014, 0020, 0024, 0028, 0030, 0032, 0033, 0071, 0072, 0078, 0143

## Context

- Users rate answers so that the organisation can see where answers fall short. A rating is thumbs up or down on any of the user's own answered turns, with an optional category (incorrect, incomplete, not relevant, other) and an optional comment; it is stored on the turn, the user can change it, and session reads return it with the turn.
- A rating counts only once the API has stored it. The client reports success only after the API confirms the save, and a failed save stays visible and can be sent again, so the feedback data is complete and a user never believes a rating was kept when it wasn't. Thumbs up is stored as well as thumbs down, so the data isn't skewed towards complaints, and every answered turn the user can see is rateable, in a live session and in a reopened one.
- Each save emits a metadata-only event (rating, category, project ID and turn ID, never the comment) for an admin dashboard.
- The turn document sits in the caller's own partition `[oid, sessionId]` (ADR 0030). After finalisation other writers patch the same turn on other paths (follow-ups, memory-extraction state; ADR 0072), so its ETag moves on its own.
- Some organisations don't allow free-text feedback, because comments can hold personal data.

## Decision

**Operation.** `putTurnFeedback` — `PUT /v1/sessions/{sessionId}/turns/{turnId}/feedback`; burst-limited (`429 too_many_requests`) and write-limited (`429 write_rate_limited`, or `503 dependency_unavailable` when the limit store can't be reached, failing closed; ADR 0143); body at most 65,536 bytes of `application/json`.

- Body `FeedbackRequest` (`extra="forbid"`): `rating` `"up"` or `"down"` (required); `category` `"incorrect"`, `"incomplete"`, `"not_relevant"`, `"other"` or null (default null); `comment` string of at most 8,000 characters before normalisation, or null (default null).
- Comment normalisation: NFC; CR LF and lone CR become LF; characters of category Cc other than LF and TAB, and of categories Cf, Co and Cs, are removed; trimmed; an empty result becomes null; the result must hold at most 2,000 code points.
- Checks, in the API-wide order (ADR 0020): validation, `422 validation_failed` (reason `category_requires_down` at `body.category` when `category` is non-null with `up`; `too_long` at `body.comment` above 2,000 code points; `comments_disabled` at `body.comment` for a non-null comment while `feedback.comments_enabled` is false); then lookup: point-read the turn in `[oid, sessionId]` and call `authorize_project_for_chat` with its `projectId`, where a missing session, a missing turn and an unreachable project all answer `404 turn_not_found` with one body; then the state rule: feedback is allowed on a `completed` turn and on a `cancelled` turn whose answer holds text, otherwise (`streaming`, `failed`, or `cancelled` with no text) `409 feedback_not_allowed`.
- Write: `patch_item` with `set /feedback` = `{rating, category, comment, createdAt, updatedAt}` and `set /updatedAt`, conditioned on the ETag of the turn just read (`etag`, `match_condition`); `createdAt` is the stored value, or now on the first save; `updatedAt` is now. A `412` (another patch landed in between) re-reads the turn, repeats the lookup and state checks and reapplies, at most 3 attempts, then `503 dependency_unavailable`.
- `200` `FeedbackView` = `rating`, `category`, `comment`, `updatedAt`, sent only after the patch commits.
- A `PUT` carries the whole value and replaces it, keeping the first save's `createdAt`: a `null` comment removes a stored comment, and a change to `up` clears the category. There is no delete. Last write wins, with no `If-Match`, because only the turn's owner can write it. The same body twice stores the same value with a new `updatedAt`.
- Errors: `401`, `403`, `404 turn_not_found`, `409 feedback_not_allowed`, `413 request_too_large`, `415 unsupported_media_type`, `422 validation_failed`, `429 too_many_requests`, `429 write_rate_limited`, `503 dependency_unavailable`.

**Event and metric.** After the commit: the custom event `turn_feedback_saved` with exactly `rating`, `category` (or `none`), `projectId`, `turnId`, `sessionId`, `isChange` (feedback was already stored) and `hasComment`; and the counter `docchat.feedback.saved` with the dimensions `rating` and `category` only. Never the comment. Span `docchat.feedback.put` carries the session, turn and project IDs, rating, category, `is_change` and the outcome. No audit row: a rating is a user's opinion of their own answer, not an admin or security action.

**Comment switch.** Ops config `feedback.comments_enabled` (boolean, default `true`, `FeedbackOps`). When `false`, a non-null comment is refused with `comments_disabled`; stored comments stay; `getCurrentUser` returns `features.feedbackComments: false`, and the web client hides the comment box. A change is live within 60 seconds (ADR 0028).

**Client.** Thumbs up and thumbs down appear on every `completed` turn and every `cancelled` turn with an answer, in live and reopened sessions; the pressed state shows only the rating the API confirmed. Thumbs up saves at once with `category` and `comment` null; while the confirmed rating is up and comments are enabled, a comment action opens the dialog without categories. Thumbs down opens a dialog with an optional category and an optional comment, prefilled from stored feedback. Success shows only on `200`; a `422` marks the field its path names; any other failure leaves the rating unconfirmed and shows the error with its correlation ID, and the user can send it again.

**Dashboard and review.** The `conversations` workbook reads thumbs up and down per project per day, the down rate and negative feedback by category from `turn_feedback_saved` events (`AppEvents`); the metric gives totals with bounded cardinality. Reading the content behind a negative rating follows the just-in-time conversation-read runbook with a ticket (ADR 0024).

## Alternatives considered

- **Feedback in its own container or SQL table.** Rejected: a second store to write, and a join on every session read; the rating belongs to the turn, and session reads return it with no extra query.
- **`If-Match` on feedback.** Rejected: the turn's ETag moves whenever follow-ups or extraction state are patched, so clients would meet `412`s for changes they don't care about; with one writer and whole-value `PUT`s, last write wins loses nothing. The internal ETag condition still keeps the patch safe against concurrent patches.
- **A delete operation.** Rejected: users need to change a rating, not withdraw it; one write operation covers corrections.
- **A category with thumbs up.** Rejected: the four categories say what was wrong; with `up` they'd be contradictory data.
- **Required category or comment on thumbs down.** Rejected: friction that suppresses ratings; both stay optional.
- **Feedback on `streaming` or `failed` turns.** Rejected: a `streaming` turn can still change, and a `failed` turn ended in an error its error code already records; a rating rates an answer.
- **An audit row per save.** Rejected: `audit_events` records admin and security actions; the stored value is the record and the event feeds the dashboard.
- **A `projectId` dimension on the metric.** Rejected: its cardinality grows with every project; the event carries the project.
- **Comments always allowed.** Rejected: organisations that forbid free text holding personal data need a switch that works without a release.

## Consequences

- Every rating a user gives is stored and can be changed on any answered turn, including in reopened sessions; success is never shown before the commit.
- Comments are user content in the conversation store: covered by retention, export and deletion (ADR 0032, ADR 0033), never logged, readable by operators only through just-in-time access.
- Turning the switch off stops new comments and keeps stored ones; `FeedbackView` and `getSession` still return them.
- A rating can be changed but not withdrawn.
- The per-project tiles depend on `turn_feedback_saved` events reaching Application Insights; the counter keeps the totals.
- Tests: validation and normalisation (`category_requires_down`, 2,001 code points → `too_long`, `comments_disabled` with the switch off); up, down and a change that keeps `createdAt`, the category rule, the comment limit, `409` on `streaming` and `failed` turns, and an injected Cosmos DB failure answering `503` with nothing stored; concurrent feedback and follow-ups on one turn both persist; the captured event holds exactly its 7 keys and never the comment, and the metric carries only `rating` and `category`; authorisation matrix rows (no token `401`, no role `403`, another user's or a hidden turn `404`); in the browser, success shows only after `200`, and an injected `503` shows the error with its correlation ID; isolation: another user, an `Admin` included, gets `404 turn_not_found` and the turn is unchanged, and a revoked membership gives `404` until it is restored.

## References

- Cosmos DB partial document update (patch `set`, at most 10 operations): https://learn.microsoft.com/en-us/azure/cosmos-db/partial-document-update (checked 2026-10-14)
- `azure.cosmos.aio.ContainerProxy.patch_item` (`etag`, `match_condition`): https://learn.microsoft.com/en-us/python/api/azure-cosmos/azure.cosmos.aio.containerproxy (checked 2026-10-15)
- Cosmos DB optimistic concurrency (`412` on a stale `_etag`): https://learn.microsoft.com/en-us/azure/cosmos-db/database-transactions-optimistic-concurrency (checked 2026-10-14)
