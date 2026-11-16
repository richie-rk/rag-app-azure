# ADR 0079: Session titles from the first question

- **Status:** Accepted
- **Date:** 2026-10-18
- **Decided by:** Architect
- **Specified in:** 13-conversations-and-memory.md, 80-data-model.md, 10-web-client.md, 84-testing.md
- **Related ADRs:** 0014, 0033, 0056, 0071, 0078, 0084

## Context

- Every session needs a title in the session list, so its owner can recognise a conversation by the topic it started with. A title that changed with later questions would make a conversation hard to find again, so the title must stay as it was set.
- The session document holds the title beside the project, the rolling summary and the timestamps (ADR 0071), one title per session.
- The session is created by the first turn's start batch (ADR 0056), before any answer exists, and the client lists it as soon as the `meta` event arrives, so the title must exist at creation.
- The chat request model `ChatTurnRequest` normalises the question (CR LF and lone CR to LF, NFC, trimmed), limits it to 1 to 4,000 code points, refuses control characters other than LF and TAB and surrogates, and requires at least one code point of category L, N, P or S, so a title always keeps at least one character.
- Titles appear in the session list, in `getSession` and in user-data exports; they never appear in logs, traces or audit metadata (ADR 0014).

## Decision

- `begin_turn` derives the title in code from the request's normalised question when it creates the session, through the internal factory `new_session_document(oid, session_id, project_id, first_question, now)` (`docchat_api.conversations.store`), and stores it in `session.title`. The title never changes: no operation renames a session, later turns don't touch it, and no model is called.
- Algorithm:
  1. Unicode NFC (`unicodedata.normalize("NFC", text)`).
  2. Replace each run of whitespace, line breaks included, with one space U+0020; whitespace is every character for which `str.isspace()` is true.
  3. Remove every character of Unicode general category Cc, Cf, Co or Cs.
  4. Trim leading and trailing spaces.
  5. If the result has at most 80 code points, it is the title.
  6. Otherwise let `p` be the 1-based position of the last space among code points 1 to 79. If such a space exists and `p` ≥ 40, keep code points 1 to `p` − 1; otherwise keep code points 1 to 79. Remove trailing spaces and the characters `.`, `,`, `;`, `:` and `-` (`rstrip(" .,;:-")`), then append `…` (U+2026). The result has at most 80 code points.
  7. If no character survives step 4 (unreachable under the request model's rules), the title is `…`.
- The session document model validates `title` as 1 to 80 code points before every write; a breach is a programming defect, never silent truncation.
- The web client renders titles only as text nodes, never as Markdown or HTML, on one line cut visually with a CSS ellipsis.

## Alternatives considered

- **A model-written title** (the chat-small model summarising the first question or exchange). Rejected: a model call, latency and cost on every new session, wording that varies between runs, and model output in a list the user scans; it would also be ready only after the session is already listed.
- **A title from the first answer.** Rejected: the answer is shaped by third-party source text, which a poisoned document could steer, and it exists only after generation.
- **The latest question as the title.** Rejected: the title would shift with every follow-up, and the conversation could not be found by its topic.
- **A rename operation.** Rejected for the first version: one more write with its own validation and concurrency that nothing requires yet; a later `PATCH` on the session can add it without touching stored titles.
- **A title sent by the client.** Rejected: the chat request carries only project, session, client turn ID and question; a client-written title is one more untrusted input for no gain.
- **Cutting at a set count with no word boundary.** Rejected: it splits words; the 40-code-point floor stops one long word near the end from shrinking the title to a stub.
- **Counting UTF-16 units or bytes.** Rejected: every text limit in the API counts code points, and a code-point cut never splits a surrogate pair.

## Consequences

- No latency or cost at session creation, and identical first questions give identical titles.
- A session whose first turn failed or was cancelled keeps that first question's title.
- Removing category Cf strips bidirectional controls and zero-width characters, so a title can't display differently from its text. The cost: a zero-width-joiner emoji sequence shows as its separate emoji, and zero-width non-joiners are removed from scripts that use them; accepted for a display label.
- Step 2 runs before step 3, so a format character between two spaces leaves two spaces in the title; accepted, because the title is display text only.
- A cut can separate a combining mark from its base where no precomposed form exists; accepted, because the full question stays in the session's first turn.
- Categories and whitespace follow Python 3.13's Unicode database (UCD 15.1.0). A runtime upgrade to a newer database can derive a different title from the same question for new sessions only, because a title is derived once and stored.
- A mistyped first question stays the title; the user starts a new chat for a new one.
- Titles are personal data in the user's partition: exported and deleted with the user's data (ADR 0033), never logged.
- Tests: whitespace runs and line breaks collapse; Cc, Cf, Co and Cs characters vanish; a question of 80 code points is kept whole; a 200-character question is cut at a space at position 40 or later and ends with `…`; with no such space it is cut at 79; trailing `.,;:-` are removed; emoji and right-to-left text are preserved.

## References

- Python `unicodedata` (NFC, general categories, UCD 15.1.0): https://docs.python.org/3.13/library/unicodedata.html (checked 2026-10-18)
- Python `str.isspace()` and `str.rstrip()`: https://docs.python.org/3.13/library/stdtypes.html (checked 2026-10-18)
