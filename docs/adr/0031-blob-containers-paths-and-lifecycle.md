# ADR 0031: Blob containers, paths and lifecycle

- **Status:** Accepted
- **Date:** 2026-10-18
- **Decided by:** Architect
- **Specified in:** 14-ingestion.md, 80-data-model.md, 82-security.md, 83-infrastructure.md, 84-testing.md, 86-operations.md
- **Related ADRs:** 0015, 0032, 0034, 0038, 0039, 0040, 0042, 0052, 0069, 0106, 0116, 0145, 0149, 0152

## Context

The data storage account `stdocchatdata<env><token>` holds three kinds of file: the bytes an admin uploads, the single-page PDFs that chat citations open, and the intermediate artifacts that one ingestion activity hands to the next. The Durable Functions host keeps its task hub in its own account `stdocchathost<env><token>`, so nothing here touches orchestration state.

- **Names can't decide where bytes live.** A document's name is display metadata supplied by an admin. The same name exists in several indexes, a new version can carry a different name, and a name can hold characters that mean something in a path. If names formed paths, two files with the same name would overwrite each other and a name could point outside its folder.
- **A run must never read another run's files.** Only one item per document runs at a time (ADR 0034), but a retry must resume from its own run's files while a later run can build the same version with other method parameters, and Enrich reuses the table summaries it finds. So every work file belongs to one run, one document and one version.
- **Every store needs a stated lifetime.** Work files matter only while their item runs, but a failed item or a crash mid-run must not leave them in storage for ever.
- **A mistaken delete must stay recoverable for a while,** because a source file can't be recreated from anything else in the product.
- **Activities exchange paths, never content,** so orchestration history stays small and every retry reads the same bytes.
- **Malware scanning shares the account.** Defender for Storage writes its verdict into blob index tags, and a metadata update soon after an upload can make the scan fail.
- **The local emulator is limited.** Azurite 3.37.0 supports neither blob nor container soft delete, blob versions or blob expiry, so those behaviours can be checked only in Azure.

Vendor facts that bound the choice: the blob service takes `deleteRetentionPolicy {enabled, days 1–365, allowPermanentDelete}`, `containerDeleteRetentionPolicy {enabled, days 1–365}`, `isVersioningEnabled`, `changeFeed` and `restorePolicy` (restore days fewer than the delete retention days); a lifecycle rule's prefix starts with the container name, both `daysAfterModificationGreaterThan` and `daysAfterCreationGreaterThan` are allowed, a policy change takes up to 24 hours to take effect, and with soft delete on a lifecycle delete leaves the blob soft-deleted for the retention period; with versioning on, index tags apply to one version; Undelete Blob needs `Microsoft.Storage/storageAccounts/blobServices/containers/write`; uncommitted blocks are garbage-collected when no Put Block or Put Block List succeeds on the blob within a week of the last Put Block.

## Decision

**Containers.** Three private containers on the data account, declared in Bicep through `avm/res/storage/storage-account` 0.33.1 (GA API 2025-06-01), each with `publicAccess: 'None'`: `documents`, `renditions` and `ingestion-work`. The account sets `allowBlobPublicAccess: false`, `allowSharedKeyAccess: false` and `defaultToOAuthAuthentication: true`, so every access is an Entra-authenticated call.

**Paths.** Every path is built from generated identifiers only:

| Container | Path | Written by | Holds |
|---|---|---|---|
| `documents` | `{indexId}/{documentId}/{v}/source` | API, `adminUploadContent` | the uploaded bytes, in the Cool access tier (ADR 0152); blob content type = the version's media type; never served to a reader |
| `documents` | `{indexId}/{documentId}/{v}/converted.pdf` | ingestion, Render | the PDF converted from a DOCX or PPTX source (ADR 0152), in the Cool access tier; read only by ingestion |
| `documents` | `_scanner-canary/{canaryId}.pdf` | ingestion, `ingestion_maintenance` (the sweep writes the first) | a generated one-page PDF that proves scanning works (ADR 0039) |
| `renditions` | `{indexId}/{documentId}/{v}/pages/{n}.pdf` | ingestion, Render | single-page PDF of page `n` (1-based) of the version's PDF, for every format |
| `renditions` | `{indexId}/{documentId}/{v}/document.pdf` | ingestion, Render | the whole-document rendition, joined from the verified pages, at most 104,857,600 bytes (ADR 0145); absent when the version's `document_pdf_status` is `too_large` |
| `renditions` | `{indexId}/{documentId}/{v}/complete.json` | ingestion, Render, deleted before the first page is written and written last | `{"pageCount": <n>, "pdfSha256": "<hex>", "renditionRulesVersion": <integer>, "documentPdf": "built" \| "too_large"}`; its presence means the rendition set of that PDF is complete under that rules version |
| `ingestion-work` | `{runId}/{documentId}/{v}/convert/converted.pdf` | Extract | the converted PDF that later Extract calls and Render read (DOCX and PPTX) |
| `ingestion-work` | `{runId}/{documentId}/{v}/extract/part-{first:05d}-{last:05d}.json` | Extract | extracted structure of pages `first` to `last` (page-wise) |
| `ingestion-work` | `{runId}/{documentId}/{v}/extract/document.json` | Extract | whole-document extraction (recursive) |
| `ingestion-work` | `{runId}/{documentId}/{v}/enrich/table-summaries.jsonl` | Enrich | one line per finished table, keyed by the SHA-256 of its sanitised HTML |
| `ingestion-work` | `{runId}/{documentId}/{v}/chunks/{chunkSetId}.jsonl` | Chunk | a header line, then one line per chunk |
| `ingestion-work` | `{runId}/{documentId}/{v}/embeddings/{chunkSetId}/batch-{n:05d}.json` | Embed | the vectors of one embedding batch |

1. IDs in paths are lowercase hyphenated UUIDs. `{v}` is `document_versions.version_number` in decimal without padding: a reservation takes `documents.next_version_number` and increments it under the document row's update lock, so a number is never reused, even after a stale upload is removed. Page numbers are decimal without padding; part and batch numbers are zero-padded to 5 digits.
2. No admin-supplied text (file name, title, index name) ever enters a path. Only `docchat_core.ingestion.names` forms paths, for both apps (the API's uploads and citation files, every ingestion activity): `source_path(index_id, document_id, version)`, `rendition_page_path(index_id, document_id, version, page)`, `rendition_marker_path(index_id, document_id, version)` and `work_prefix(run_id, document_id, version)`. `document_versions.blob_path` (`NVARCHAR(200)`) stores the source path.
3. A version's source path is written once: the API commits the block list with `match_condition=MatchConditions.IfMissing` (`If-None-Match: *`) only after size and SHA-256 match the reservation (ADR 0042).
4. Work files are UTF-8 JSON or JSON Lines carrying `formatVersion: 1`, written with `upload_blob(overwrite=True)` and read whole, so a retried activity replaces its own output. Work paths start with the run ID, so two runs never share a work file. Index operations write no work files. Render skips a version whose `complete.json` exists with the version's SHA-256.
5. Activities, sub-orchestration inputs and queue messages carry paths and IDs, never file content.
6. No blob metadata is written: SQL holds every attribute. Index tags are written only by Defender for Storage and read only by the ingestion identity through the custom role DocChat Blob Tag Reader (`.../blobs/tags/read` only) on `documents`; no workload identity holds `blobs/tags/write`.
7. On-upload malware scanning covers the sources and canaries of `documents` only: `excludeBlobsWithPrefix: ["renditions/", "ingestion-work/"]` and `excludeBlobsWithSuffix: ["/converted.pdf"]`, because the two containers and each converted PDF hold only output the ingestion app derived from scanned sources, and no code reads a scan tag on a converted PDF. No source or canary name ends with `/converted.pdf`. The canaries are scanned.

**Access per container.** API: Storage Blob Data Contributor on `documents` (stage, commit, read for citation downloads, delete a rejected duplicate), Storage Blob Data Reader on `renditions`, nothing on `ingestion-work`. Ingestion: Storage Blob Data Contributor on all three, DocChat Blob Tag Reader on `documents`.

**Soft delete and versioning.** Blob service of the data account: `deleteRetentionPolicy {enabled: true, days: 14, allowPermanentDelete: false}`; `containerDeleteRetentionPolicy {enabled: true, days: 30}`; `isVersioningEnabled: false`; change feed off; restore policy off. The host account keeps 7 days for both.

**Lifecycle.** `managementPolicies/default` holds exactly one rule; an expired work file then stays soft-deleted for 14 days:

```json
{"name": "ingestion-work-expiry", "enabled": true, "type": "Lifecycle",
 "definition": {"filters": {"blobTypes": ["blockBlob"], "prefixMatch": ["ingestion-work/"]},
                "actions": {"baseBlob": {"delete": {"daysAfterModificationGreaterThan": 7}}}}}
```

**Who deletes what.** Every delete below leaves the blob soft-deleted for 14 days.

| Files | Deleted by | When |
|---|---|---|
| an item's work files | `clean_up_item` (every blob under the item's `work_prefix`), in both its success and its discard mode | at the end of the item; the lifecycle rule removes what a crashed or interrupted item, or a failed clean-up, left behind, 7 days after the last write |
| a superseded version's source, converted PDF and renditions | `clean_up_item` after the Flip | at once; the sweep retries leftovers as `cleanup_document` items after `ingestion.cleanup_retry_minutes` (15) |
| a failed version's source, converted PDF and renditions | the sweep, through a `cleanup_document` item | 15 minutes after `failed_at` |
| a duplicate rejected at commit (`409 content_unchanged` or `duplicate_content`) | the API | at once |
| a deleted document's sources, converted PDFs and renditions | its `cleanup_document` item | in the `document_delete` run the delete creates |
| a deleted logical index's blobs | `purge_index_blobs` (ADR 0052) | in the `index_delete` run |
| a stale `uploading` version's blob | the sweep | `ingestion.stale_upload_hours` (24) after `reserved_at`, up to 200 a wake |
| a malicious source | the sweep (ADR 0038) | when its tag says `Malicious` |
| a canary blob | the sweep once its result is recorded; `ingestion_maintenance` step 4 otherwise (ADR 0032) | at the result, or 90 days after `written_at`, before its row |
| blocks staged by an upload that never committed | the storage service | one week after the last staged block |

## Alternatives considered

- **Name-based paths** (`{indexId}/{name}`): names repeat across indexes, change between versions and can carry path characters, so same-named files would overwrite each other.
- **One container with prefixes:** container-scoped role assignments are what let the API hold no right on work files and only read on renditions; separate containers also make the scan exclusion and the lifecycle rule whole-container prefixes.
- **Work paths without the run ID** (`{documentId}/{v}/...`): a later run of the same version, with other method parameters or another method version, would find an earlier run's extraction and table summaries and reuse them.
- **Blob versioning, with or without point-in-time restore for blobs:** work files are replaced on every retry and every batch of table summaries, so versioning would keep copies no reader needs; index tags would apply per version; Azurite doesn't support versions. Version-numbered paths already give each content version its own blob.
- **Blob metadata for document attributes:** a metadata write soon after an upload can make the malware scan fail, and SQL already holds every attribute with constraints.
- **A longer blob soft delete (30 days or more):** a document deleted by an admin, or removed for legal reasons, would stay recoverable longer with no need for it. 14 days covers a mistake noticed within two working weeks; **a shorter one (7 days)** risks a mistake found after a holiday.
- **App clean-up only, with no lifecycle rule:** failed or interrupted items would leave work files in storage for ever.
- **Expiry by creation time:** a work file written again by a retry should get a full 7 days from that write, so the rule counts from the last modification.
- **Content inside orchestration inputs and outputs:** history would grow with document size and every replay would carry it.

## Consequences

- A path can't name a file of another index, document or version, and a retry writes the same path, so retries are idempotent and clean-up is exact by prefix.
- Nothing outside the app finds a file by its name: an operator maps a document to its paths through SQL (`documents`, `document_versions.blob_path`).
- Container deletion is never an app operation; a container deleted by mistake is recoverable for 30 days, at no cost while nothing is deleted.
- `allowPermanentDelete: false` means no caller can purge a soft-deleted blob early: a document's bytes, legal removals included (ADR 0149), leave storage 14 days after clean-up deletes them, and soft-deleted bytes are billed until then.
- Recovering a deleted source within the 14 days is a storage-level Undelete Blob call that needs `containers/write`; it changes no SQL row, so recovered bytes re-enter ingestion only as a new upload of the same bytes. This version defines no runbook for it.
- A new environment's lifecycle rule takes up to 24 hours to start deleting.
- An abandoned upload's staged blocks stay billed until the service discards them, one week after the last staged block.
- Tests: Azurite can't run soft delete or versioning, so the `live` test marker covers blob soft delete, versioning (off) and index tags against `dev`, and the PSRule hardening checks cover soft delete on both storage accounts (ADR 0106). Integration tests prove that Render skips an existing page set, that the sweep removes stale uploads with their blobs, and that a document deleted mid-ingestion loses its chunks, renditions and source.
- Risk: the path builders serve both apps, so a builder bug misplaces every file at once. One unit test per builder pins its exact output for constant UUIDs, version and page numbers.

## References

- Blob service properties (Bicep): https://learn.microsoft.com/en-us/azure/templates/microsoft.storage/storageaccounts/blobservices (checked 2026-10-14)
- Lifecycle policy (Bicep): https://learn.microsoft.com/en-us/azure/templates/microsoft.storage/storageaccounts/managementpolicies (checked 2026-10-14)
- Lifecycle management overview: https://learn.microsoft.com/en-us/azure/storage/blobs/lifecycle-management-overview (checked 2026-10-14)
- Blob index tags: https://learn.microsoft.com/en-us/azure/storage/blobs/storage-manage-find-blobs (checked 2026-10-14)
- Malware scanning on upload: https://learn.microsoft.com/en-us/azure/defender-for-cloud/introduction-malware-scanning (checked 2026-10-14)
- Authorising blob operations with Entra ID (Undelete Blob): https://learn.microsoft.com/en-us/rest/api/storageservices/authorize-with-azure-active-directory (checked 2026-10-14)
- Put Block (uncommitted blocks): https://learn.microsoft.com/en-us/rest/api/storageservices/put-block (checked 2026-10-18)
- Azurite feature support: https://github.com/Azure/Azurite/blob/main/README.md (checked 2026-10-14)
