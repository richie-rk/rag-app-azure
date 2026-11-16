// Citation downloads (10-web-client.md section 8.14): only the API is fetched, the bytes become a
// blob typed `application/pdf`, and a temporary `<a download>` saves it under a safe name; the
// object URL is revoked 30,000 ms later. `429 too_many_requests` is retried at most twice after
// its `retryAfterSeconds`; every other refusal is shown at once.

import { download, type Downloaded } from "../../api/http";
import type { FileOperationId, RequestArgs } from "../../api/operations";
import { retryDelay, shouldRetry } from "../../api/queryClient";
import { DOWNLOAD_STEM_MAX, OBJECT_URL_REVOKE_MS } from "../../platform/constants";

const UNSAFE = /[\p{Cc}\\/:*?"<>|]/gu;

/** The name a page or the whole document is saved under: unsafe characters replaced by `_`, the
 * stem cut to 150 characters, the extension forced to `.pdf`; an empty name becomes
 * `source-<n>.pdf`. */
export function safeFileName(name: string, citationNumber: number): string {
  const replaced = name.replace(UNSAFE, "_");
  const dot = replaced.lastIndexOf(".");
  const stem = dot > 0 ? replaced.slice(0, dot) : dot === 0 ? "" : replaced;
  const cut = Array.from(stem).slice(0, DOWNLOAD_STEM_MAX).join("");
  return cut.trim() === "" ? `source-${String(citationNumber)}.pdf` : `${cut}.pdf`;
}

/** Waits `ms`, or rejects at once with an `AbortError` when `signal` aborts. */
function pause(ms: number, signal: AbortSignal | undefined): Promise<void> {
  return new Promise((resolve, reject) => {
    const onAbort = () => {
      clearTimeout(timer);
      reject(new DOMException("Aborted", "AbortError"));
    };
    const timer = setTimeout(() => {
      signal?.removeEventListener("abort", onAbort);
      resolve();
    }, ms);
    signal?.addEventListener("abort", onAbort, { once: true });
  });
}

/** One of the three file operations, with section 5.7's automatic repeats. */
export async function downloadFile<K extends FileOperationId>(
  operationId: K,
  args: RequestArgs<K>,
): Promise<Downloaded> {
  for (let failures = 0; ; failures += 1) {
    try {
      return await download(operationId, args);
    } catch (reason) {
      if (!shouldRetry(failures, reason)) throw reason;
      await pause(retryDelay(failures, reason), args.signal);
    }
  }
}

/** Saves `blob` under `name` through a temporary link, and revokes its object URL later. */
export function saveBlob(blob: Blob, name: string): void {
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = name;
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => {
    URL.revokeObjectURL(url);
  }, OBJECT_URL_REVOKE_MS);
}
