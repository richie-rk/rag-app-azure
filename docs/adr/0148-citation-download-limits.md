# ADR 0148: Citation download limits

- **Status:** Accepted
- **Date:** 2026-10-21
- **Decided by:** Architect
- **Specified in:** 11-api.md, 12-chat-pipeline.md, 80-data-model.md, 81-config.md, 82-security.md, 84-testing.md, 85-observability.md, 86-operations.md
- **Related ADRs:** 0007, 0012, 0030, 0058, 0069, 0070, 0087, 0143, 0145, 0152

## Context

- Three operations stream a rendition through an API worker: `getCitationContent` the cited page's rendition, of at most 20,971,520 bytes, `getCitationDocument` the whole-document rendition, of at most 104,857,600 bytes (ADR 0069), and `adminGetDocumentPdf` the same whole-document rendition for an administrator. No operation streams an uploaded file (ADR 0152). Each download holds a blob stream, a connection and a buffer in a worker for as long as it runs, and file egress is paid.
- The per-user chat limits (ADR 0058) bound questions and tokens, not downloads. A download loop, or hundreds of slow readers, would exhaust the API's workers and run up egress for everyone, so the file operation needs per-user limits of its own.
- The per-user burst limit (ADR 0143) is a flood guard: an in-process token bucket per Gunicorn worker (40 tokens, refilled at 10 per second) that answers `429 too_many_requests` with `retryAfterSeconds` 1 before any store is touched. It loosens with every worker and instance, so it can't be a per-user budget.
- Any instance serves any request, so per-user state lives in the Cosmos DB `usage` container, partitioned by the user's object ID, with the algorithms of ADR 0058.
- The browser reads `Content-Length`, a CORS-safelisted header, so a body cut short is detectable (ADR 0087).

## Decision

1. **Where.** On `getCitationContent`, `getCitationDocument` and `adminGetDocumentPdf`, counted together in one window and one set of leases per user: for the two citation operations after the pre-body checks (the burst limit among them, before path validation) and path validation (canonical UUIDs, `citationNumber` an integer from 1 to 16, else `422`), and before the turn read (ADR 0069 step 2); for `adminGetDocumentPdf` after the administrator check and path validation, and before its index and document lookups, its lease carrying a null `sessionId` and `turnId`. `getCitation` returns metadata only and has no limit of its own beyond the burst limit.
2. **Request window.** The two-bucket sliding window of ADR 0058 on items `dlrate:{epochMinute}` in the caller's `usage` partition (`type` `download_rate_window`, `count`, `ttl` 180), with `L` = `ratelimit.citation_downloads_per_minute`, default 30, range 1 to 500. A refusal is `429 rate_limited` with `retryAfterSeconds`, the smallest whole number of seconds from 1 to 120 after which one more request would be allowed with no further requests, and the same value in `Retry-After`. Every request that passes path validation counts, including those later refused with `404` or `410`; refused requests aren't counted.
3. **Concurrency lease.** The slot algorithm of ADR 0058 on items `dllease:{slot}` (`type` `download_lease`, `sessionId`, `turnId`, `acquiredAt`, `expiresAt` = acquisition + 300 s, `ttl` 300), slot 1 to `ratelimit.max_concurrent_downloads`, default 2, range 1 to 10. All slots live → `429 too_many_downloads` with `retryAfterSeconds` 10.
4. **Store failures.** Both checks use the `usage` timeouts (2 s for point reads; 3 s for create, patch, replace and delete); a timed-out operation is retried once, and a second timeout refuses the request with `503 dependency_unavailable` (`retryAfterSeconds` 5). The limits fail closed.
5. **Release.** The lease is deleted, conditioned on the ETag this request wrote and shielded from cancellation, whenever the response ends for any reason: the body completed, the client disconnected, a later check refused the request (`404 citation_not_found`, `409 document_pdf_unavailable`, `410 document_changed`, `500 citation_content_missing`, and for `adminGetDocumentPdf` `404 document_not_found` and `500 document_pdf_missing`) or the download was aborted. A `404`, `412` or timeout on the delete is ignored, because the TTL ends the lease.
6. **Streaming bounds.** Bytes are read in 1 MiB chunks (the blob client's `max_single_get_size` and `max_chunk_get_size` are 1 MiB) and written to the connection in 64 KiB pieces. A piece the connection hasn't accepted within 30 s aborts the response (`slow_client`); a download still running 300 s after its lease was acquired is aborted (`duration`), so no download outlives its lease; at the shutdown drain, 90 s after the stop signal (ADR 0007), running downloads are aborted (`drain`) and their leases released. Headers have already gone out, so an aborted response closes the connection with no problem body. The response class `docchat_api.chat.citations.CitationFileResponse`, a Starlette `StreamingResponse` subclass, wraps each piece's `send` in the 30-second timeout and owns the release.
7. **Telemetry.** Counter `docchat.ratelimit.rejections` with `reason` `download_rate` or `download_concurrency`; counter `docchat.citation.download_aborts` with `reason` `slow_client`, `duration` or `drain`; warning log record `citation_download_aborted` with `docchat.session_id`, `docchat.turn_id`, `docchat.citation.number`, `docchat.citation.bytes_sent` and `docchat.citation.abort_reason`, metadata only.
8. **The web client.** `getCitationContent`, `getCitationDocument` and `adminGetDocumentPdf` are retried automatically only on `429 too_many_requests` (at most 2 retries); `429 rate_limited` and `429 too_many_downloads` show the wait with a Try again that enables when it ends, and a body shorter than `Content-Length` shows the error with Try again (ADR 0087). The API keeps these responses uncompressed, with `Content-Length` equal to the blob size.

The values: 300 s carries the largest whole-document rendition, 104,857,600 bytes, at about 2.8 Mbit/s (104,857,600 bytes × 8 ÷ 300 s), and a single-page rendition, at most 20 MiB, at about 0.6 Mbit/s; a client that accepts less than 64 KiB in 30 s (about 17 kbit/s) has stopped reading; 30 requests a minute is one every 2 s on average, far beyond reading pace, so only a loop meets it; 2 leases let a page view and a download overlap, as 2 stream leases do for chat; `retryAfterSeconds` 10 matches `too_many_streams`.

## Alternatives considered

- **Only the burst limit.** Rejected: it is per worker and in memory, so a user's ceiling grows with workers and instances, and it holds no concurrency or duration bound.
- **A daily byte quota per user.** Rejected: it needs a counter write per response and a size before streaming starts, while the window, the lease and the 300-second cap already bound what one user can occupy: at most 2 streams at a time, each for at most 300 s.
- **SAS URLs so the browser downloads from storage.** Rejected: the browser makes no direct Azure call, and no storage URL or key ever reaches it.
- **A longer cap per download.** Rejected: workers stay occupied longer by readers that barely read, and the lease, whose TTL equals the cap, must outlive its download.
- **Counting only successful downloads.** Rejected: probing with requests answered `404` or `410` would then cost nothing.
- **Range requests so an interrupted download resumes.** Rejected (ADR 0069): the client reads whole bodies, and ranges would multiply the requests the window counts.

## Consequences

- One user can't hold more than 2 worker streams for downloads, or keep one longer than 300 s; a crashed instance strands a lease for at most 300 s, so "too many sources loading" clears without an operator.
- Each download costs the `usage` container two point reads and a write for the window, plus a create (or a read and a replace) and a delete for the lease; items stay under 1 KB.
- A reader on a link slower than about 2.8 Mbit/s can't finish a whole-document rendition at its 104,857,600-byte cap, and sees the cut-short state with Try again; page renditions are far smaller, and the cited page always opens on its own.
- A `usage` outage refuses downloads with `503 dependency_unavailable` rather than serving them unmetered.
- Obligations (84-testing.md): an integration test that, with `ratelimit.max_concurrent_downloads` = 2, three concurrent downloads by one user held open by slow readers, two `getCitationContent` and one `getCitationDocument`, give two `200` and one `429 too_many_downloads` with `retryAfterSeconds` 10, that a new request gets `200` once one completes, and that a `404` after the lease leaves no lease; an integration test that the 31st request in a minute, the three operations counted together, gets `429 rate_limited` with `retryAfterSeconds` and `Retry-After`, that a client that stops reading is aborted after 30 s ± 2 s with `citation_download_aborted` (`slow_client`) and its lease deleted, and that with a fake clock a download still running at 300 s is aborted (`duration`); a route test that the burst limit answers the three citation routes; the two `ratelimit.*` rows in ops config; the runbook for "too many sources loading" reports (leases expire within 300 s with no manual step; a rise in `slow_client` aborts from one user is checked against `download_rate` and `download_concurrency` rejections).

## References

- Azure Cosmos DB time to live: https://learn.microsoft.com/en-us/azure/cosmos-db/time-to-live (checked 2026-10-14)
- Azure Cosmos DB optimistic concurrency: https://learn.microsoft.com/en-us/azure/cosmos-db/database-transactions-optimistic-concurrency (checked 2026-10-14)
- Blob downloads with the Python client library (`max_single_get_size`, `max_chunk_get_size`): https://learn.microsoft.com/en-us/azure/storage/blobs/storage-blobs-tune-upload-download-python (checked 2026-10-21)
- CORS-safelisted response headers: https://developer.mozilla.org/en-US/docs/Glossary/CORS-safelisted_response_header (checked 2026-10-14)
