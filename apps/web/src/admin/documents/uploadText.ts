// The upload panel's text (10-web-client.md sections 10.3, 10.4, 10.6 and 10.10): each file's
// result, the shared-index warning above Upload, and the summary after the submit.

import type { ProjectRef, UploadPreflightResult } from "../../api/generated/types.gen";
import { en } from "../../copy/en";
import { fillText, formatSize, pluralText } from "../../ui/format";
import type { RowCheck, UploadRow, UploadSummary } from "./uploadBatch";

/** The shared-index warning names at most this many projects, then `upload.sharedIndex.more`. */
export const SHARED_INDEX_NAMES = 10;

/** `{maxSize}`: `uploadLimits.maxFileBytes` in MB with one decimal place (section 12.7). */
export function maxSizeText(maxFileBytes: number): string {
  return formatSize(maxFileBytes);
}

/** A preflight result's text (section 10.3); an `update` row adds its checkbox. */
export function preflightText(result: UploadPreflightResult, maxFileBytes: number): string {
  switch (result.status) {
    case "new":
      return en["upload.result.new"];
    case "unchanged":
      return en["upload.result.unchanged"];
    case "duplicate":
      return fillText(en["upload.result.duplicate"], {
        existingName: result.existingDocument?.name ?? "",
      });
    case "update":
      return en["upload.result.update"];
    case "rejected":
      switch (result.reason) {
        case "unsupported_type":
          return en["upload.result.rejectedType"];
        case "too_large":
          return fillText(en["upload.result.rejectedSize"], {
            maxSize: maxSizeText(maxFileBytes),
          });
        case "empty_file":
          return en["upload.result.empty"];
        case "invalid_name":
          return en["upload.result.invalidName"];
        case "duplicate_in_batch":
          return en["upload.result.duplicateInBatch"];
        case null:
          return en["upload.result.rejected"];
      }
  }
}

/** A file's text before the upload: the checks, the hash and the preflight. */
export function checkText(check: RowCheck, maxFileBytes: number): string {
  switch (check.kind) {
    case "pending":
    case "hashed":
      return "";
    case "rejectedType":
      return en["upload.result.rejectedType"];
    case "rejectedSize":
      return fillText(en["upload.result.rejectedSize"], { maxSize: maxSizeText(maxFileBytes) });
    case "unreadable":
      return en["upload.result.unreadable"];
    case "preflight":
      return preflightText(check.result, maxFileBytes);
  }
}

/** A file's result: its upload's outcome once reached, otherwise its check. The progress of a file
 * being sent is `progressText`. */
export function resultText(row: UploadRow, maxFileBytes: number): string {
  switch (row.upload.kind) {
    case "none":
    case "uploading":
      return checkText(row.check, maxFileBytes);
    case "done":
      return en["upload.fileDone"];
    case "failed":
      return fillText(en["upload.fileFailed"], { reason: row.upload.reason });
    case "skipped":
      return row.upload.reason;
    case "stopped":
      return en["upload.fileStopped"];
    case "paused":
      return en["upload.filePaused"];
  }
}

/** `upload.progress` for a file being sent. */
export function progressText(name: string, percent: number): string {
  return fillText(en["upload.progress"], { name, percent });
}

/** The warning above Upload (section 10.4): the projects that map the index, in the response's
 * order, the first 10 by name and then the count of the rest; `upload.sharedIndex.unknown` while
 * the list is loading, after it failed, or when it names none. */
export function sharedIndexText(projects: readonly ProjectRef[] | null): string {
  if (projects === null || projects.length === 0) return en["upload.sharedIndex.unknown"];
  const names = projects
    .slice(0, SHARED_INDEX_NAMES)
    .map((project) => project.name)
    .join(", ");
  const rest = projects.length - SHARED_INDEX_NAMES;
  const listed =
    rest > 0 ? `${names} ${fillText(en["upload.sharedIndex.more"], { count: rest })}` : names;
  return pluralText(en["upload.sharedIndex"], projects.length, { projects: listed });
}

/** The summary after the submit (section 10.6): complete when every file chosen for upload was
 * uploaded and submitted, otherwise partial. */
export function summaryText(summary: UploadSummary): {
  readonly complete: boolean;
  readonly title: string | null;
  readonly body: string;
} {
  if (summary.submitted === summary.total) {
    return {
      complete: true,
      title: en["upload.complete.title"],
      body: pluralText(en["upload.complete.body"], summary.submitted),
    };
  }
  return {
    complete: false,
    title: null,
    body: fillText(en["upload.partial"], { uploaded: summary.submitted, total: summary.total }),
  };
}
