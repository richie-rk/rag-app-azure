# ADR 0093: Upload client

- **Status:** Accepted
- **Date:** 2026-10-21
- **Decided by:** Architect
- **Specified in:** 10-web-client.md, 11-api.md, 14-ingestion.md, 84-testing.md
- **Related ADRs:** 0012, 0039, 0042, 0043, 0088, 0092, 0143

## Context

Admins add PDF, DOCX and PPTX documents to an index through the API in five steps: preflight, confirm, reserve, upload and submit (ADR 0042). The browser computes each file's SHA-256 so the API can classify every file before any bytes move; the API recomputes the hash while it streams and refuses bytes that differ. The browser must never hold a storage credential or name a storage location, the file types it accepts must match the API's rule exactly (any letter case), and admins must see every file's outcome and every run's progress and failures.

What the four operations do:

- `adminPreflightUploads` takes 1 to `maxFilesPerBatch` (default 100) files as `{name, sizeBytes, sha256}` and answers, read-only and in request order, `new`, `unchanged`, `duplicate`, `update` or `rejected` with a reason (`unsupported_type`, `too_large`, `empty_file`, `invalid_name`, `duplicate_in_batch`).
- `adminReserveUpload` takes `{name, sizeBytes, sha256, projectId, confirmUpdate}` and answers `201 {documentId, versionId, versionNumber, reused}`, or `409 content_unchanged`, `duplicate_content` or `update_not_confirmed`.
- `adminUploadContent` takes the raw bytes in a `PUT` with `Content-Type: application/octet-stream` (else `415`) and a `Content-Length` (else `411`) equal to the reserved size; it streams 4 MiB blocks, checks signature, byte count and SHA-256 before committing, commits nothing on a disconnect, and answers `200` to a repeat once the version is `pending` with the reserved hash. Each API worker serves at most `DOCCHAT_UPLOAD_CONCURRENT_STREAMS_PER_WORKER` (default 4) upload streams at once and answers the next with `429 upload_busy` and `retryAfterSeconds` 5.
- `adminSubmitUploads` takes `{versionIds}` (1 to 100) and creates one `upload` run with one item per queued version: `202 {runId, results}`, or `200` with `runId: null` when nothing was queued.
- All four answer `503 malware_scanning_not_ready` with `retryAfterSeconds` 3,600 while scanning is on and no scanner canary has had a clean result in the last 48 hours (ADR 0039). All four pass the per-user burst limit (`429 too_many_requests`, `retryAfterSeconds` 1) and none is write-limited.

Vendor facts:

- `SubtleCrypto.digest()` takes the whole input as one `ArrayBuffer`, typed array or `DataView` and doesn't stream, so a file's bytes are in memory while it is hashed.
- `fetch` reports no upload progress; `XMLHttpRequest.upload` fires `progress` events with `loaded` and `total` while the body is sent.
- App Service closes a request that hasn't finished within 230 seconds, so a 50 MB upload needs about 2 Mbit/s of sustained upstream bandwidth.

## Decision

1. **Choosing files.** The upload panel opens from an index's Documents screen for the project chosen in its project picker, which lists the projects mapped to the index (preselected when exactly one is mapped; with none, uploads are disabled and the screen links to Projects). Files come from a Choose files button (`<input type="file" multiple accept=".pdf,.docx,.pptx">`) or a drop zone; nothing requires dragging.
2. **Checks before hashing**, against `adminGetIndexOptions.uploadLimits`: the extension must be in `allowedExtensions`, compared case-insensitively, so `.PDF` passes (else the file is listed as rejected for its type, client outcome `file_rejected_type`); the size must be at most `maxFileBytes` (default 52,428,800 bytes; else `file_rejected_size`); a selection that would take the panel above `maxFilesPerBatch` (100) files shows the too-many message and adds none of the new files (`too_many_files`).
3. **Hashing one file at a time.** `await file.arrayBuffer()`, then `crypto.subtle.digest('SHA-256', bytes)`, written as 64 lowercase hexadecimal characters, while the file shows a checking indicator; the next file starts only after the previous digest. A failed read lists that file as unreadable (`file_unreadable`).
4. **Preflight.** One `adminPreflightUploads` call with `{ files: [{ name, sizeBytes, sha256 }] }` for every remaining file. Each result's `status` and `reason` pick the file's text: `new`; `unchanged` and `duplicate` (skipped; a duplicate names `existingDocument.name`); `update`; `rejected` with one text per reason and a generic text for any other reason. A failed call shows an error with Try again, which repeats the call with the same hashes.
5. **Confirmation.** Each `update` row shows the question "This document is already ingested. Update it?" with an unchecked checkbox. The Upload button counts the `new` files plus the checked updates and is disabled at zero.
6. **Upload, one file at a time.**
   1. `adminReserveUpload` with `{ name, sizeBytes, sha256, projectId, confirmUpdate }`, where `confirmUpdate` is true only for a checked update. `409 content_unchanged` or `409 duplicate_content` marks the file skipped; any other failure marks it failed with the problem's text; either way the loop goes on with the next file.
   2. `adminUploadContent` by `XMLHttpRequest`: `PUT` with `Authorization`, `Content-Type: application/octet-stream` and a new `traceparent` on every attempt (the browser sets `Content-Length`), and the `File` itself as the body. `upload.onprogress` drives the file's progress bar; 120,000 ms without a progress event aborts the attempt as stalled (`upload_stalled`). `429 upload_busy`: wait `retryAfterSeconds` and send again, at most 6 times, not counted as a failure retry. A network failure, `408`, a `5xx` other than `503 malware_scanning_not_ready`, or a stall: retry after 2,000 ms, then after 6,000 ms, then mark the file failed. `422 hash_mismatch`, `size_mismatch` or `file_signature_mismatch`: the file changed after hashing, and the admin chooses it again. Any other `4xx`: failed with the problem's text.
   3. Stop uploading aborts the current request (that file shows as stopped), skips the files not yet sent and goes to the submit; closing the panel during this step does the same, and signing out asks for confirmation first and then aborts the upload.
7. **Submit once.** When at least one file was uploaded, one `adminSubmitUploads` call carries every uploaded `versionId`. Success shows that all or some files were submitted, with a link to the run's page (no link when `runId` is null, because every version was already queued), and refreshes the documents list; a failure shows an error whose Try again sends the same IDs.
8. **Waits on the burst limit.** In every step, a request answered `429 too_many_requests` waits `retryAfterSeconds` and is sent again, at most 3 times per request and not counted as a failure retry, before the step's failure rules apply. The upload operations aren't write-limited, so `429 write_rate_limited` never reaches this flow.
9. **Paused state.** When any of the four operations answers `503 malware_scanning_not_ready`, the batch sends no further request and never retries on its own: the current file and every file not yet uploaded show as paused; the error bar shows the problem's `detail` (never the generic server text), the time now + `retryAfterSeconds` (a short time when it falls today, otherwise a medium date with a short time) and the reference ID; Upload and Try again stay disabled until that time, after which Try again repeats the refused step (the preflight, the paused file onwards, or the submit). Files already uploaded stay uploaded.
10. **The API is the only destination.** `src/api/http.ts` is the only module that calls `fetch` or `XMLHttpRequest`. File bytes go only to `adminUploadContent`; the browser receives no SAS, names no storage account, container or blob path, and identifies the index only by its `indexId` path parameter.

## Alternatives considered

- **Browser uploads straight to Blob Storage with a SAS.** Rejected: the browser would hold a storage credential and need a path to storage, whose public access is off, and the stored bytes would bypass the API's streaming checks and scan gate (ADR 0042).
- **`fetch` for upload bodies.** Rejected: it reports no upload progress.
- **Hashing several files at once.** Rejected: `digest` holds each file's bytes in memory, so 100 files of 50 MB could hold 5 GB; one at a time bounds memory to one file.
- **A JavaScript SHA-256 library over file slices.** Rejected: a dependency doing what Web Crypto does natively, for files that fit in memory at the 50 MB default.
- **Parallel uploads.** Rejected: each API worker serves 4 streams, so one admin's batch would take a worker's slots; parallel transfers split the admin's bandwidth and push each one toward the 230-second limit; one file at a time keeps progress and failures per file.
- **`multipart/form-data` bodies.** Rejected: the API reads a raw `application/octet-stream` body whose declared `Content-Length` it checks against the reserved size before reading a byte.
- **A resumable, chunked upload protocol.** Rejected: a 50 MB file fits one request inside 230 seconds at about 2 Mbit/s, and a repeated `PUT` is harmless.
- **Retrying `503 malware_scanning_not_ready` automatically.** Rejected: the wait is an hour, and the admin needs to see the reason and the time.
- **One submit per file.** Rejected: one run per batch puts the batch's progress on one run page.

## Consequences

- The admin sees every file's outcome before any bytes move, and nothing replaces an existing document without a checked confirmation. Preflight is advisory, so reserve and commit enforce the same rules, and their `409`s are handled per file.
- Browser memory holds at most one file's bytes for hashing; the `File` body streams from disk during the upload. A file that changes on disk between hashing and upload fails with the file-changed text and is never committed.
- Upstream bandwidth bounds the file size: below about 2 Mbit/s a 50 MB file can't finish inside 230 seconds and fails after its retries, and raising `DOCCHAT_UPLOAD_MAX_BYTES` raises that need in proportion.
- After the submit, the run's page polls every 5,000 ms while the run is `queued` or `running` and the Documents screen every 15,000 ms while a document is in progress, so admins see each document's stage and error.
- Tests (84-testing.md): unit, a `.PDF` file is accepted, `.xlsx`, `.png` and `.zip` files are rejected before hashing, a file over the limit is rejected and 101 files show the too-many message; browser, 3 files covering `new`, `update` and `duplicate` show each result and only `new` plus a confirmed `update` upload, one at a time with progress, followed by one submit with exactly those version IDs; upload bodies are `application/octet-stream`; a `503` other than `malware_scanning_not_ready` is retried after 2 s and 6 s; `429 upload_busy` waits `retryAfterSeconds`; Stop uploading aborts the current file and still submits the uploaded ones; `503 malware_scanning_not_ready` from `adminPreflightUploads` shows the paused state with the problem's `detail` and time, and the same answer from `adminUploadContent` mid-batch is not retried, marks the current and remaining files paused, sends no further request and keeps Upload disabled until that time; component (fake timers), `adminReserveUpload` answered `429 too_many_requests` with `retryAfterSeconds` 1 waits 1 s and sends the same request again, and a fourth consecutive `429` falls to the step's failure rule.

## References

- `SubtleCrypto.digest()` (whole input, no streaming): https://developer.mozilla.org/en-US/docs/Web/API/SubtleCrypto/digest (checked 2026-10-21)
- `XMLHttpRequestUpload` `progress` event: https://developer.mozilla.org/en-US/docs/Web/API/XMLHttpRequestUpload/progress_event (checked 2026-10-21)
- App Service request time-outs (230 seconds): https://learn.microsoft.com/en-us/troubleshoot/azure/app-service/web-request-times-out-app-service (checked 2026-10-14)
- App Service app settings reference (idle request time-out of 230 seconds): https://learn.microsoft.com/en-us/azure/app-service/reference-app-settings (checked 2026-10-14)
