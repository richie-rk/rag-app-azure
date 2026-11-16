// The upload panel's checks before hashing and the hashing itself (10-web-client.md sections 10.1
// and 10.2, ADR 0093). Each chosen file is checked against `adminGetIndexOptions.uploadLimits`
// before any of its bytes are read: the extension, compared case-insensitively, the size, and the
// number of files the panel would hold. A file that passes is hashed with SHA-256, one file at a
// time, so at most one file's bytes are in memory.

import type { UploadLimits } from "../../api/generated/types.gen";

/** The `accept` list of the file input (section 10.1). It only narrows the browser's picker; the
 * checks below decide. */
export const UPLOAD_ACCEPT = ".pdf,.docx,.pptx";

/** A chosen file's verdict before hashing: `file_rejected_type`, `file_rejected_size`, or ready to
 * hash. */
export type FileCheck = "rejectedType" | "rejectedSize" | "accepted";

export type SelectionCheck =
  /** `too_many_files`: the selection would take the panel past `maxFilesPerBatch`, so none of it
   * is added. */
  { readonly tooMany: true } | { readonly tooMany: false; readonly files: readonly FileCheck[] };

/** The file's extension in lower case, with its dot, or `""` when the name has none. */
export function extensionOf(name: string): string {
  const dot = name.lastIndexOf(".");
  return dot < 0 ? "" : name.slice(dot).toLowerCase();
}

/** Checks one file: the extension first, then the size. */
export function checkFile(
  file: Pick<File, "name" | "size">,
  limits: Pick<UploadLimits, "allowedExtensions" | "maxFileBytes">,
): FileCheck {
  const allowed = limits.allowedExtensions.map((extension) => extension.toLowerCase());
  if (!allowed.includes(extensionOf(file.name))) return "rejectedType";
  if (file.size > limits.maxFileBytes) return "rejectedSize";
  return "accepted";
}

/** Checks a new selection against a panel already holding `held` files. */
export function checkSelection(
  held: number,
  files: readonly Pick<File, "name" | "size">[],
  limits: UploadLimits,
): SelectionCheck {
  if (held + files.length > limits.maxFilesPerBatch) return { tooMany: true };
  return { tooMany: false, files: files.map((file) => checkFile(file, limits)) };
}

/** The file's SHA-256 as 64 lower-case hexadecimal characters. Rejects when the file can't be
 * read (`file_unreadable`). */
export async function sha256Hex(file: Blob): Promise<string> {
  const bytes = await file.arrayBuffer();
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  return Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, "0")).join("");
}
