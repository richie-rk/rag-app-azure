# ADR 0069: Citation files through the authorised API

- **Status:** Accepted
- **Date:** 2026-10-18
- **Decided by:** Product owner and architect
- **Specified in:** 10-web-client.md, 11-api.md, 12-chat-pipeline.md, 14-ingestion.md, 80-data-model.md, 82-security.md, 84-testing.md, 85-observability.md
- **Related ADRs:** 0004, 0012, 0020, 0031, 0064, 0083, 0087, 0145, 0148, 0152

## Context

A numbered citation lets a user check an answer against the passage it came from. The product owner decided that citations open the source through the authenticated API and never through a public viewer. Opening one must therefore meet these needs:

- The caller names only what is theirs: a session, a turn and a citation number. No document ID, path, container or account comes from the client, because any of those would let a caller ask for a file they can't reach.
- Access is checked on every open, because memberships, index mappings and document versions change after the answer was written.
- No storage URL, key or SAS reaches the browser, and no file goes to a third-party viewer.
- A file response never runs as active content in the API's origin, and a file name never injects into a header.
- A citation to a superseded or deleted version answers `410` with a plain message that the document has changed.

The product owner decided that readers receive only sanitised PDF renditions, for every format: the cited page, and the whole document as one PDF for viewing or saving the entire file. DOCX and PPTX sources are converted to PDF during ingestion (ADR 0152), so every citation names a page of a PDF, and no uploaded file of any format is served to a reader.

Facts (checked 2026-10-14):

- Browsers render inline only HTML, XML, text, media, multipart and, when a PDF viewer is supported, PDF; anything else is handed off as a download. In Edge 154 an iframe pointed at a DOCX blob started a download and showed nothing.
- A fetched PDF wrapped in a `Blob` of type `application/pdf` renders in the built-in viewer from an object URL in an iframe under the web client's CSP (Edge 154, Chrome 155); a sandboxed iframe shows no viewer.
- Cross-origin scripts read only the CORS-safelisted response headers (`Cache-Control`, `Content-Language`, `Content-Length`, `Content-Type`, `Expires`, `Last-Modified`, `Pragma`) unless `Access-Control-Expose-Headers` lists more, and App Service's platform CORS has no expose-headers setting. The web client can read `Content-Length`, not `Content-Disposition`.
- An uploaded PDF can carry document-level actions, scripts, forms and embedded files, and an uploaded DOCX or PPTX macros, links to other files and embedded objects; the single-page renditions written during ingestion are built anew from the rendition object schema, with every annotation flattened into the page content, and neither they nor the whole-document rendition joined from them hold an annotation or a link of any kind (ADR 0145).

## Decision

**1. Three operations, keyed only by session, turn and number.** All three are mounted on the `/v1` router with route class `DocChatRoute` and its default route options (token, role, burst limit and first-sign-in provisioning run before path validation); the caller comes from `current_principal`.

- `getCitation`, `GET /v1/sessions/{sessionId}/turns/{turnId}/citations/{citationNumber}`: `200` `CitationDetail` (`application/json`) with `number`, `title` (1 to 300 characters), `pageNumber` (integer ≥ 1), `headingPath` (at most 8 strings of at most 200 characters), `contentType`, `openAs` (`pdf_page`, for every format), `fileName` (1 to 255 code points), `documentPdfAvailable` (boolean) and `documentFileName` (1 to 255 code points). It reads no blob.
- `getCitationContent`, `GET /v1/sessions/{sessionId}/turns/{turnId}/citations/{citationNumber}/content`: `200` with the cited page's rendition.
- `getCitationDocument`, `GET /v1/sessions/{sessionId}/turns/{turnId}/citations/{citationNumber}/document`: `200` with the whole-document rendition of the cited version.
- Path: canonical UUIDs and `citationNumber` an integer from 1 to 16, otherwise `422 validation_failed`.

**2. The checks, in order, on every call.**

1. Path validation; for `getCitationContent` and `getCitationDocument`, then the per-user download window and lease of ADR 0148 (every later refusal releases the lease).
2. `get_turn(principal, session_id, turn_id)`, a point read in partition `[oid, sessionId]`: missing, or `status = "streaming"`, → `404 citation_not_found`.
3. `authorize_project_for_chat(principal, turn.projectId)`: refused → `404 citation_not_found`.
4. The stored citation with that number, else `404 citation_not_found`.
5. One SQL query: `app.documents` by the citation's document ID, joined to `app.document_versions` on the cited version of that document and to `app.chunk_sets` on the cited chunk set of that version, left-joined to `app.project_indexes` on the turn's project and the document's `search_index_id` (index-backed by the four primary keys). No row → `410 document_changed`; `index_mapped = 0` (the document's index doesn't serve the turn's project) → `404 citation_not_found`; `is_deleted = 1`, or `current_version_id` other than the cited version, → `410 document_changed`.
6. The cited chunk set's `pdf_sha256` must equal the version's `sha256` for a PDF and its `converted_sha256` for a DOCX or PPTX, so the cited page number names a page of the renditions served; any other value, `NULL` on either side included, → `410 document_changed`. A DOCX or PPTX converted anew under another converter version can paginate differently, and this check keeps a citation of the earlier chunk set from opening a page that holds other text.
7. `getCitationDocument` only: the version's `document_pdf_status` other than `built` → `409 document_pdf_unavailable`, with no blob read. `getCitation` never applies this check, so a citation of a version without a whole-document rendition still opens its page.

A citation from a `cancelled` or `failed` turn opens like any other.

**3. What is served.** `{version}` is `document_versions.version_number`; paths come only from `docchat_core.ingestion.names` (`rendition_page_path`, `rendition_document_path`).

| Operation | For | Blob | `Content-Type` | File name |
|---|---|---|---|---|
| `getCitationContent` (`openAs` `pdf_page`) | every citation, of every format | container `renditions`, `{indexId}/{documentId}/{version}/pages/{n}.pdf`, with `n` = the citation's `pageNumber` | `application/pdf` | `<stem>-page-<n>.pdf`, where `<stem>` is `source_name` without its extension, cut to 255 code points minus the length of `-page-<n>.pdf` |
| `getCitationDocument` | every citation whose version's `document_pdf_status` is `built` | container `renditions`, `{indexId}/{documentId}/{version}/document.pdf` | `application/pdf` | `<stem>.pdf`, with `<stem>` cut to 251 code points |

No citation operation reads the container `documents`: neither the uploaded source of any format nor the converted PDF of a DOCX or PPTX is ever served. `getCitation` returns both file names in its body. The API reads `renditions` under Storage Blob Data Reader, scoped to that container; its role on `documents` serves the upload commit only.

**4. Headers on every file response.** `Content-Disposition: attachment` built only by `docchat_api.http.content_disposition(name)`:

- `filename*=UTF-8''<encoded>` (RFC 6266 with RFC 8187): the name in Unicode NFC cut to 200 code points keeping its extension (the part before the last `.` is shortened until it and the extension total 200; a name without `.` is cut to 200), every UTF-8 byte outside RFC 8187's `attr-char` set percent-encoded;
- `filename="<fallback>"`: derived from that cut name by replacing, one for one, every code point outside printable ASCII (0x20 to 0x7E) and each of `"`, `\`, `/`, `;` and `%` with `_`, so it is at most 200 characters and keeps the extension.

Also `Content-Length` (the blob size), `Content-Security-Policy: sandbox`, `Cache-Control: no-store` and `X-Content-Type-Options: nosniff`. The full body is always returned with `200`; range requests aren't supported.

**5. Streaming.** `BlobClient.download_blob()` then `chunks()` of 1 MiB, from the API's data-account `BlobServiceClient` built with `max_single_get_size` and `max_chunk_get_size` of 1,048,576 bytes; nothing is buffered whole. The download limits of ADR 0148 apply to `getCitationContent` and `getCitationDocument` counted together (per user, 30 requests per minute and 2 concurrent downloads; each 64 KiB write accepted within 30 s; 300 s per download, which carries the largest whole-document rendition, 104,857,600 bytes, at about 2.8 Mbit/s). A storage `404` for a current version → `500 citation_content_missing` and error log `citation_blob_missing`; a storage error after the headers were sent ends the response early and logs `citation_stream_failed`.

**6. The web client** (ADR 0087) calls `getCitation`, then, for every format: with `navigator.pdfViewerEnabled` true it fetches `getCitationContent` with its bearer token, requires `200`, `application/pdf` and at most 20,971,520 bytes, wraps the bytes in a `Blob` of type `application/pdf` and shows it from an object URL in an `<iframe>` without a `sandbox` attribute; without a PDF viewer it shows the details panel with the page-download action (copy key `citation.downloadPage`). Below either, Download full document (copy key `citation.downloadDocument`) fetches `getCitationDocument` when `documentPdfAvailable` is `true`, requires `200`, `application/pdf` and at most 104,857,600 bytes, and saves the bytes through a temporary `<a download>` named from `documentFileName`; when it is `false`, the caption `citation.documentTooLarge` stands in its place. A frame is never pointed at anything but a PDF blob, and no client action fetches an uploaded file.

**7. Errors.** `401`, `403` and `503 authentication_unavailable` (pre-body checks); `429 too_many_requests` (burst limit, `retryAfterSeconds` 1); `422 validation_failed`; `404 citation_not_found`; `410 document_changed`; `429 rate_limited` and `429 too_many_downloads` (`getCitationContent` and `getCitationDocument`); `409 document_pdf_unavailable` (`getCitationDocument` only); `500 citation_content_missing` (`getCitationContent` and `getCitationDocument`); `503 dependency_unavailable` (`retryAfterSeconds` 5).

## Alternatives considered

- **Serve the uploaded PDF, opened at the cited page.** Rejected: it can carry document-level actions, scripts, forms and embedded files, and the viewer frame can't be sandboxed without losing the viewer; it would also send the whole document to show one page. The rendition is sanitised and holds one page.
- **Serve the uploaded DOCX or PPTX as a download.** Rejected (ADR 0152): the reader's Office application would open a file written by someone else, with every feature that loads content from elsewhere or runs code when it opens; the renditions of its converted PDF show the same pages with no active content.
- **Serve the converted PDF, or the uploaded PDF, as the whole document.** Rejected: neither has been through `verify-render`; the whole-document rendition is joined from the verified pages, so it carries nothing that a page rendition doesn't.
- **Short-lived SAS URLs to the blobs.** Rejected: a storage URL carrying a credential would reach the browser, the web client's CSP would need the storage origin, and access wouldn't be re-checked when the citation opens.
- **An operation keyed by document ID and page.** Rejected: the client would name data, and each document ID would need its own authorisation path. Session, turn and number resolve only inside the caller's own partition.
- **`410` when the index has been unmapped from the project.** Rejected: the mapping is an access condition, and every access refusal answers `404`, which reveals nothing about the document. `410` stays for content changes, which the user can act on by asking again.
- **`Content-Disposition: inline` for PDF pages.** Rejected: the client reads the bytes and shows them from a blob URL, so `inline` adds nothing, and `attachment` stops a response opened by direct navigation from rendering in the API's origin.
- **The file name from `Content-Disposition`.** Rejected: cross-origin scripts can't read that header.
- **Range requests.** Rejected: the client reads whole bodies, and ranges would complicate the download leases and limits.

## Consequences

- Every open re-runs the checks, so a removed membership, an unmapped index or a new version takes effect on the next open, with no cache to invalidate. No storage URL, key or SAS reaches the browser, and no file goes to a third-party viewer.
- Ingestion must write a rendition for every page of every version, of every format, and the whole-document rendition or its `too_large` status, before its Flip, and cleanup removes the renditions of superseded versions (ADR 0145); `citation_blob_missing` is the signal of a gap.
- Users see one page per citation in the viewer, and the whole document as one PDF up to 104,857,600 bytes; a larger document opens page by page only. A DOCX or PPTX shows the converter's pagination, which can differ from Office's, while the citation and the page opened always agree.
- Telemetry (85-observability.md): span `docchat.citation.open` (`docchat.operation`, `docchat.outcome`, `docchat.open_as` `pdf_page` or `document_pdf`), counter `docchat.citation.requests` (`operation`; `outcome` `ok`, `not_found`, `gone`, `unavailable`, `missing`, `limited` or `aborted`), and the log records `citation_blob_missing`, `citation_stream_failed` and `citation_download_aborted`, all metadata only, never a title or file name.
- Tests: unit, a page name built from a 255-code-point `source_name` at page 2,000 is at most 255 code points and ends in `-page-2000.pdf`, a whole-document name is at most 255 code points and ends in `.pdf`, and the file routes pass the name to `content_disposition(name)` and send `attachment`; integration, a PDF, a DOCX and a PPTX citation each return `openAs: pdf_page`, the single page with `application/pdf`, `attachment`, `sandbox`, `no-store`, `nosniff` and the right `Content-Length`, and the whole-document rendition from `getCitationDocument`; a version with `document_pdf_status` `too_large` answers `409 document_pdf_unavailable` with no blob read, while its page still opens; a chunk set whose `pdf_sha256` differs from the version's PDF answers `410`; a superseded version and a deleted document answer `410`, and an unmapped index, another user's session, a removed membership, a streaming turn and an unknown number answer `404`; the blob recorder shows no read under `documents/` for any citation operation of any format; isolation entries `citations.foreign_turn`, `citations.revoked`, `citations.superseded` and `citations.unmapped_index`; end-to-end, a PDF and a DOCX citation show their page and Download full document saves `<stem>.pdf`.

## References

- HTML Standard, loading a document (what renders inline): https://html.spec.whatwg.org/multipage/browsing-the-web.html#loading-a-document (checked 2026-10-14)
- HTML Standard, iframe `sandbox`: https://html.spec.whatwg.org/multipage/iframe-embed-object.html#attr-iframe-sandbox (checked 2026-10-14)
- CORS-safelisted response headers: https://developer.mozilla.org/en-US/docs/Glossary/CORS-safelisted_response_header (checked 2026-10-14)
- App Service platform CORS (no expose-headers setting): https://learn.microsoft.com/en-us/azure/app-service/app-service-web-tutorial-rest-api (checked 2026-10-14)
- Blob data roles: https://learn.microsoft.com/en-us/azure/storage/blobs/assign-azure-role-data-access (checked 2026-10-14)
