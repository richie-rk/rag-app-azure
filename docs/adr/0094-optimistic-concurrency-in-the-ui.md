# ADR 0094: Optimistic concurrency in the UI

- **Status:** Accepted
- **Date:** 2026-10-21
- **Decided by:** Architect
- **Specified in:** 10-web-client.md, 11-api.md, 13-conversations-and-memory.md, 84-testing.md
- **Related ADRs:** 0012, 0020, 0022, 0077, 0090, 0092, 0143

## Context

Every admin edit is optimistic: a resource carries its `rowVersion` in the response body, the client returns it in `If-Match`, and a stale write gets `412`. The value travels in the body because platform CORS can't expose response headers to scripts. Memory edits use the same protocol with the memory document's ETag. Admins edit, archive, restore, re-ingest and delete projects, indexes and documents, so two admins, or one admin in two tabs, can work on the same resource at once, and neither may silently overwrite the other or lose what they typed.

The server's rules:

- `rowVersion` is the 8-byte SQL `ROWVERSION` as padded base64 (12 characters, `^[A-Za-z0-9+/]{11}=$`). `If-Match` must be exactly one strong entity tag in double quotes: a missing header, `*`, a weak tag or a list gets `428 precondition_required`, and a well-formed stale tag gets `412 precondition_failed`.
- Members and index mappings are parts of the project, so adding or removing a member, mapping or unmapping an index and changing the default index each move the project's `rowVersion` and are guarded by it. These four operations answer with `projectRowVersion` in `ProjectMemberChange`, `ProjectIndexMappingChange` or `ProjectPartRemoval` (both removals answer `200` with that body). A call that changes nothing (an existing membership, an unchanged mapping, an absent row to remove) leaves the version as it was and returns it.
- The memory `etag` is Cosmos DB's `_etag` verbatim, double quotes included, and the Cosmos DB write is conditioned on it.

Vendor facts:

- Only `Cache-Control`, `Content-Language`, `Content-Length`, `Content-Type`, `Expires`, `Last-Modified` and `Pragma` are readable by a cross-origin script without `Access-Control-Expose-Headers`, and App Service platform CORS has no setting for it.
- `If-Match` uses the strong comparison function, and a server that finds the condition false doesn't perform the method and answers `412`. SQL `rowversion` increments on every insert or update of the row; a Cosmos DB replace with `if-match` on `_etag` answers `412` on a mismatch.
- WCAG 2.2 success criterion 3.3.7 (Redundant Entry, level A) requires information the user already entered in a process to be kept or offered again rather than asked for twice.

## Decision

1. **Which writes send `If-Match`, with which value.**

   | Operations | Value |
   |---|---|
   | `adminUpdateProject`, `adminAddProjectMember`, `adminRemoveProjectMember`, `adminMapProjectIndex`, `adminUnmapProjectIndex` | the project's `rowVersion` |
   | `adminUpdateIndex`, `adminStartReingest`, `adminDeleteIndex` | the index's `rowVersion` |
   | `adminDeleteDocument`, `adminReingestDocument` | the document's `rowVersion` |
   | `updateMemorySettings`, `updateMemoryFact`, `deleteMemoryFact`, `clearMemory` | the memory's `etag` |

   Feedback, upload and create operations send no `If-Match`.
2. **Header format.** The body value is sent verbatim when it already starts and ends with `"` (memory ETags) and wrapped in double quotes otherwise: the `rowVersion` `AAAAAAAAB9E=` goes out as `If-Match: "AAAAAAAAB9E="`.
3. **Keeping the version current.** Every successful write returns the updated resource, whose `rowVersion` or `etag` replaces the cached one. The four membership and mapping operations return `projectRowVersion`, which replaces the `rowVersion` of the cached project (query key `['admin', 'project', projectId]`), so one admin's consecutive changes chain without a reload.
4. **Project first on the Members and Indexes tabs.** Every load or reload of either tab (each opening, whatever the cached data's age; window focus; after a change or a `412`) requests `adminGetProject`, then the first page of its list (`adminListProjectMembers` with 100 per page, or `adminListProjectIndexes` with 50), and writes the project response into the cache only after that page has arrived. While either tab is shown, the project query doesn't refetch on its own. So the version a tab sends is never newer than the rows it shows.
5. **Forms on `412`.** A form loads the resource as `base` and keeps its `rowVersion`; the user edits a `draft`; Save validates the draft against the generated request schema and limits and then sends the `PATCH` with `If-Match`. On `200` the response replaces the cache and becomes `base`. On `412` the client reads the resource again (`latest`) and takes `latest.rowVersion`; it keeps the draft; under every field where `latest` differs from `base` it shows that field's saved value with a link that copies it into the draft (text keys `admin.conflict.latestValue` and `admin.conflict.useLatest`); it shows the conflict error bar (`admin.conflict.title`, `admin.conflict.body`), sets `base` to `latest`, and the user saves again. A `404`, `409`, `422` or `429` also keeps the draft; a `429` disables Save until the time from `retryAfterSeconds`.
6. **The Details form and the same admin's member changes.** When a membership or mapping change succeeds and the Details form's kept `rowVersion` equals the version that change sent, the form takes the returned `projectRowVersion` and leaves `base` and the draft as they are, because members and mappings aren't form fields; a later Save changes then doesn't meet a `412` the same admin caused.
7. **Actions on `412`.** Re-ingest of an index, re-ingest of a document and delete of a document refetch the resource or list, show the action-conflict text (`admin.conflict.action`) and leave the user to repeat the action deliberately. Add people, Remove, Map, Make default and Unmap send no further request (Add people stops its loop and the people not yet added stay in the picker; the Map an index dialog stays open with its choices), reload the tab project first, show `admin.conflict.action` above the table, and leave the user to check the reloaded rows and repeat the action, which then sends the reloaded version. A notice still reports the people added before the `412`.
8. **Add people, one person at a time.** For each chosen person, in the order chosen: `adminAddProjectMember` with `If-Match` = the cached project's `rowVersion` for the first call and the previous response's `projectRowVersion` for each later call. `201` (added) and `200` (already a member, such as a person who joined at first sign-in) both count as added. `429 too_many_requests` or `429 write_rate_limited`: wait `retryAfterSeconds` and send the same person with the same `If-Match`, at most 3 waits per person. `412` stops the loop (item 7); `404 project_not_found`, `409 project_archived` and `428` also stop it, with their own text; `404 user_not_found` or any other failure fails that person and the loop goes on. Leaving the tab ends the loop before its next request.
9. **Memory on `412`.** Refetch `getMemory` and show the memory-conflict text (`memory.conflict`); an open fact editor keeps its typed text when its fact still exists and closes otherwise; the pause switch shows the refetched state.
10. **`428` is a defect.** It means the client sent no tag or a malformed one; the screen shows the defect text with the reference ID and never retries.

## Alternatives considered

- **Last write wins, with no `If-Match`.** Rejected: two admins' edits would overwrite each other silently, and every admin edit needs the protocol.
- **Reading the `ETag` response header.** Rejected: platform CORS can't expose response headers to scripts.
- **Discarding the draft and reloading on `412`.** Rejected: the user would lose typed input (criterion 3.3.7) and couldn't see what changed.
- **Resending a `412`'d write automatically with the new version.** Rejected: it would overwrite the other admin's change without anyone seeing it.
- **A version per membership or mapping row.** Rejected: two admins working from one stale screen could each make a different index the default; one project version over fields, members and mappings stops the second. The accepted cost is that a membership change makes another admin's pending Details edit stale, which costs that admin one merge.
- **Reading `adminGetProject` again after each member change, without chaining `projectRowVersion`.** Rejected: an extra request per person, and the re-read would pick up another admin's change that the tab's rows don't show.
- **Continuing Add people after a `412` with the fresh version.** Rejected: the other admin's change can bear on the batch, so the user checks the reloaded rows first.

## Consequences

- Typed values survive every conflict, and each changed field shows its saved value with a one-click way to take it; editing, archiving, restoring, re-ingesting and deleting stay safe with several admins at work.
- One admin's consecutive changes never collide with each other; another admin's change in between always surfaces as a `412` that the user resolves.
- A membership or mapping change by one admin makes another admin's open Details form stale, so that admin's next Save meets one `412` and the merge of item 5.
- The client's checks protect nothing on their own: the API answers `428` and `412` whatever the client does.
- Tests (84-testing.md): unit, `If-Match` is sent verbatim for a quoted ETag and quoted for a bare `rowVersion`, on exactly the operations of item 1 and never on feedback, upload or create operations; browser, a `412` on Save changes keeps the typed values, shows the latest saved value under each changed field, and a second save succeeds with the new `rowVersion`; component, Add people with three people sends three `adminAddProjectMember` calls one after another, the first with the project's `rowVersion` and each later one with the previous `projectRowVersion`, a `200` counts as added, and Save changes on the Details tab then sends the last returned version and succeeds; a `412` on the second of three adds sends no third add, reloads `adminGetProject` and then the members, shows `admin.conflict.action`, keeps the second and third people in the picker and reports 1 person added; with fake timers, `429 write_rate_limited` with `retryAfterSeconds` 2 waits 2 s and resends with the same `If-Match`, `429 too_many_requests` waits 1 s the same way, and a fourth refusal fails that person while the next is still sent; Remove, Map, Make default and Unmap keep each `projectRowVersion`, `removed: false` shows the same notice as `removed: true`, and a `412` reloads the tab project first while the Map an index dialog keeps its choices; the Members and Indexes tabs request `adminGetProject` before their lists and cache it only after the first page arrives; browser, each memory action sends the current ETag, and a `412` refetches, shows the memory conflict and keeps the open editor's text. The stub API scripts `412` and `428` on the membership and mapping operations (ADR 0092).

## References

- CORS-safelisted response headers: https://developer.mozilla.org/en-US/docs/Glossary/CORS-safelisted_response_header (checked 2026-10-14)
- App Service platform CORS (allowed origins only, no exposed headers): https://learn.microsoft.com/en-us/azure/app-service/app-service-web-tutorial-rest-api (checked 2026-10-14)
- RFC 9110, `If-Match` (section 13.1.1) and `412`: https://www.rfc-editor.org/rfc/rfc9110#name-if-match (checked 2026-10-21)
- `rowversion` and the optimistic-concurrency pattern: https://learn.microsoft.com/en-us/sql/t-sql/data-types/rowversion-transact-sql (checked 2026-10-14)
- Cosmos DB optimistic concurrency with `_etag`: https://learn.microsoft.com/en-us/azure/cosmos-db/database-transactions-optimistic-concurrency (checked 2026-10-14)
- WCAG 2.2 success criterion 3.3.7, Redundant Entry: https://www.w3.org/WAI/WCAG22/Understanding/redundant-entry.html (checked 2026-10-21)
