// One upload batch of the upload panel (10-web-client.md sections 10.1 to 10.9, ADR 0042): the
// checks before hashing, hashing one file at a time, one preflight, the confirmation of updates,
// uploading one file at a time (reserve, then the bytes), one submit, the paused state and the
// waits on the burst limit. The API is the only destination: every request goes through
// `src/api/http.ts`, and the browser never receives a storage URL.
//
// The batch is an external store, read with `useSyncExternalStore`, so it survives the panel
// closing and opening and keeps running after the panel's content unmounts: closing the panel
// during the upload stops it and still submits the files already uploaded (section 10.5 step 3).
// While files upload or the batch submits, it is registered as active work, so a sign-out asks
// first and then aborts it, and an unload asks the browser to confirm (sections 4.6 and 8.19).

import type {
  UploadLimits,
  UploadPreflightFile,
  UploadPreflightResult,
  UploadReservation,
  UploadReservationRequest,
  UploadSubmitResult,
} from "../../api/generated/types.gen";
import { request, uploadContent } from "../../api/http";
import {
  ApiFailure,
  apiErrorOf,
  clientError,
  isProblem,
  retryAtOf,
  type ApiError,
} from "../../api/problem";
import { en } from "../../copy/en";
import { registerActiveWork } from "../../platform/activeWork";
import {
  BATCH_RATE_LIMIT_WAITS,
  UPLOAD_BUSY_WAITS,
  UPLOAD_RETRIES,
  UPLOAD_RETRY_DELAYS_MS,
} from "../../platform/constants";
import { errorText } from "../../ui/ErrorBar";
import { checkSelection, sha256Hex, type FileCheck } from "./uploadChecks";

/** The four upload operations, bound to one index. */
export interface UploadApi {
  readonly preflight: (
    files: readonly UploadPreflightFile[],
    signal: AbortSignal,
  ) => Promise<readonly UploadPreflightResult[]>;
  readonly reserve: (
    body: UploadReservationRequest,
    signal: AbortSignal,
  ) => Promise<UploadReservation>;
  readonly content: (
    versionId: string,
    file: File,
    signal: AbortSignal,
    onProgress: (loaded: number, total: number) => void,
  ) => Promise<unknown>;
  readonly submit: (
    versionIds: readonly string[],
    signal: AbortSignal,
  ) => Promise<UploadSubmitResult>;
}

/** The upload operations through `http.ts`; the index travels only as the `indexId` path
 * parameter (section 10.9). */
export function httpUploadApi(indexId: string): UploadApi {
  return {
    preflight: async (files, signal) =>
      (
        await request("adminPreflightUploads", {
          path: { indexId },
          body: { files: [...files] },
          signal,
        })
      ).results,
    reserve: (body, signal) => request("adminReserveUpload", { path: { indexId }, body, signal }),
    content: (versionId, file, signal, onProgress) =>
      uploadContent({ path: { indexId, versionId }, body: file, signal, onProgress }),
    submit: (versionIds, signal) =>
      request("adminSubmitUploads", {
        path: { indexId },
        body: { versionIds: [...versionIds] },
        signal,
      }),
  };
}

/** A file's state before the upload: waiting for its hash, refused by a check, unreadable, hashed
 * and waiting for the preflight, or the preflight's result. */
export type RowCheck =
  | { readonly kind: "pending" }
  | { readonly kind: "rejectedType" }
  | { readonly kind: "rejectedSize" }
  | { readonly kind: "unreadable" }
  | { readonly kind: "hashed" }
  | { readonly kind: "preflight"; readonly result: UploadPreflightResult };

/** A file's upload: not reached, sending (with its progress), uploaded, failed or skipped with a
 * reason, stopped by the admin, or paused with the batch. */
export type RowUpload =
  | { readonly kind: "none" }
  | { readonly kind: "uploading"; readonly percent: number }
  | { readonly kind: "done"; readonly versionId: string }
  | { readonly kind: "failed"; readonly reason: string }
  | { readonly kind: "skipped"; readonly reason: string }
  | { readonly kind: "stopped" }
  | { readonly kind: "paused" };

export interface UploadRow {
  readonly key: string;
  readonly file: File;
  readonly sha256: string | null;
  readonly check: RowCheck;
  /** The confirmation checkbox of an `update` row, unchecked until the admin checks it. */
  readonly confirmUpdate: boolean;
  readonly upload: RowUpload;
}

/**
 * - `idle`: no file chosen;
 * - `checking`: hashing, then the preflight;
 * - `ready`: the results and confirmations, waiting for Upload;
 * - `uploading`, `submitting`: the steps of sections 10.5 and 10.6;
 * - `done`: the batch ended; a new selection starts a new batch.
 */
export type UploadPhase = "idle" | "checking" | "ready" | "uploading" | "submitting" | "done";

/** A step that failed for the whole batch. `paused`: `503 malware_scanning_not_ready`, which no
 * request repeats until Retry (section 10.7). */
export interface BatchFailure {
  readonly step: "preflight" | "upload" | "submit";
  readonly error: ApiError;
  readonly paused: boolean;
}

export interface UploadSummary {
  /** The run that ingests the files; null when every version was already queued. */
  readonly runId: string | null;
  /** The results whose outcome is `queued` or `already_queued`. */
  readonly submitted: number;
  /** The files chosen for upload. */
  readonly total: number;
}

export interface UploadState {
  readonly rows: readonly UploadRow[];
  readonly phase: UploadPhase;
  /** Hashing progress while checking: `upload.checking`. */
  readonly progress: { readonly done: number; readonly total: number } | null;
  /** The last selection would have passed `maxFilesPerBatch`, so none of it was added. */
  readonly tooMany: boolean;
  readonly failure: BatchFailure | null;
  readonly summary: UploadSummary | null;
}

const INITIAL: UploadState = {
  rows: [],
  phase: "idle",
  progress: null,
  tooMany: false,
  failure: null,
  summary: null,
};

/** Whether the file goes up when Upload is pressed: `new`, or a checked `update`. */
export function isChosen(row: UploadRow): boolean {
  if (row.check.kind !== "preflight") return false;
  const { status } = row.check.result;
  return status === "new" || (status === "update" && row.confirmUpdate);
}

/** Upload's count: the `new` files plus the checked updates (section 10.4). */
export function chosenCount(state: UploadState): number {
  return state.rows.filter(isChosen).length;
}

/** No file is `new` or `update`: every file was skipped (`upload.nothingToUpload`). */
export function nothingToUpload(state: UploadState): boolean {
  return (
    state.phase === "ready" &&
    state.failure === null &&
    state.rows.length > 0 &&
    !state.rows.some(
      (row) =>
        row.check.kind === "preflight" &&
        (row.check.result.status === "new" || row.check.result.status === "update"),
    )
  );
}

/** `503 malware_scanning_not_ready`: malware scanning isn't confirmed working (section 10.7). */
export function isScanningPause(error: ApiError): boolean {
  return isProblem(error, 503, "malware_scanning_not_ready");
}

/** A failure the content step retries after 2,000 ms and 6,000 ms: no response, a stall, `408`,
 * or a `5xx` other than the scanning pause. */
export function isTransientUploadFailure(error: ApiError): boolean {
  if (error.kind === "network") return true;
  if (error.status === null || (error.kind !== "problem" && error.kind !== "http")) return false;
  return error.status === 408 || (error.status >= 500 && !isScanningPause(error));
}

/** The reason in `upload.fileFailed`: `upload.stalled` for a stall, the problem's `detail`, or the
 * client's text for a failure without a problem body (`error.network.body` for no response). */
export function failureReason(error: ApiError): string {
  if (error.code === "upload_stalled") return en["upload.stalled"];
  if (error.kind === "problem") return error.detail ?? error.title ?? "";
  return errorText(error)?.body ?? "";
}

function isSkip(error: ApiError): boolean {
  return isProblem(error, 409, "content_unchanged") || isProblem(error, 409, "duplicate_content");
}

/** Whether `signal` has aborted by now: a request or a wait may end because of it. */
function wasAborted(signal: AbortSignal): boolean {
  return signal.aborted;
}

/** Waits `ms`, or less when the signal aborts. */
export function abortableSleep(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve) => {
    if (signal.aborted) {
      resolve();
      return;
    }
    const timer = setTimeout(done, Math.max(0, ms));
    function done() {
      clearTimeout(timer);
      signal.removeEventListener("abort", done);
      resolve();
    }
    signal.addEventListener("abort", done);
  });
}

function checkOf(verdict: FileCheck): RowCheck {
  switch (verdict) {
    case "rejectedType":
      return { kind: "rejectedType" };
    case "rejectedSize":
      return { kind: "rejectedSize" };
    case "accepted":
      return { kind: "pending" };
  }
}

type FileOutcome =
  | Extract<RowUpload, { kind: "done" | "failed" | "skipped" | "stopped" }>
  | { readonly kind: "paused"; readonly error: ApiError };

const STOPPED: FileOutcome = { kind: "stopped" };

export interface UploadBatchOptions {
  readonly api: UploadApi;
  /** For tests: how a file is hashed. */
  readonly hash?: (file: File) => Promise<string>;
  /** For tests: how the batch waits. */
  readonly sleep?: (ms: number, signal: AbortSignal) => Promise<void>;
  /** An upload run ended (submitted, paused, failed or stopped): reservations and the run change
   * the Documents list, the index and the runs, so the screen refreshes them. */
  readonly onChanged?: () => void;
}

export class UploadBatch {
  private state: UploadState = INITIAL;
  private readonly listeners = new Set<() => void>();
  private readonly api: UploadApi;
  private readonly hash: (file: File) => Promise<string>;
  private readonly sleep: (ms: number, signal: AbortSignal) => Promise<void>;
  private readonly onChanged: (() => void) | undefined;
  private keys = 0;
  /** The hashing and preflight of the current pass. */
  private checks: AbortController | null = null;
  /** The request in flight during the upload and the submit. */
  private current: AbortController | null = null;
  private stopRequested = false;
  private submitAfterStop = true;
  private projectId = "";
  /** The files chosen for upload when Upload was pressed, in the table's order. */
  private chosen: readonly string[] = [];
  /** Where Retry resumes after a pause during the upload. */
  private pausedAt = 0;
  private unregister: (() => void) | null = null;

  constructor(options: UploadBatchOptions) {
    this.api = options.api;
    this.hash = options.hash ?? sha256Hex;
    this.sleep = options.sleep ?? abortableSleep;
    this.onChanged = options.onChanged;
  }

  readonly subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  };

  readonly getState = (): UploadState => this.state;

  private set(patch: Partial<UploadState>): void {
    this.state = { ...this.state, ...patch };
    for (const listener of [...this.listeners]) listener();
  }

  private updateRows(change: (row: UploadRow) => UploadRow): void {
    this.set({ rows: this.state.rows.map(change) });
  }

  private updateRow(key: string, change: (row: UploadRow) => UploadRow): void {
    this.updateRows((row) => (row.key === key ? change(row) : row));
  }

  private row(key: string): UploadRow | undefined {
    return this.state.rows.find((row) => row.key === key);
  }

  /** Whether a selection would be taken now: not while checking, uploading or submitting, nor
   * while a step is halted mid-batch or paused until its retry time. */
  canChoose(now: number = Date.now()): boolean {
    const { phase, failure } = this.state;
    if (phase === "checking" || phase === "uploading" || phase === "submitting") return false;
    if (failure === null) return true;
    if (!failure.paused) return true;
    const at = retryAtOf(failure.error);
    return at === null || at <= now;
  }

  /** A new selection (section 10.1): refused whole when it would take the panel past
   * `maxFilesPerBatch`; otherwise each file is checked, and the files that pass are hashed and
   * preflighted with the files already held. After a finished batch it starts a new one. */
  choose(files: readonly File[], limits: UploadLimits): void {
    if (files.length === 0 || !this.canChoose()) return;
    if (this.state.phase === "done") this.reset();
    const check = checkSelection(this.state.rows.length, files, limits);
    if (check.tooMany) {
      this.set({ tooMany: true });
      return;
    }
    const added = files.map((file, index): UploadRow => {
      this.keys += 1;
      return {
        key: `file-${String(this.keys)}`,
        file,
        sha256: null,
        check: checkOf(check.files[index] ?? "accepted"),
        confirmUpdate: false,
        upload: { kind: "none" },
      };
    });
    this.set({ rows: [...this.state.rows, ...added], tooMany: false, failure: null });
    void this.runChecks();
  }

  /** Hashing, one file at a time (section 10.2), then the preflight. */
  private async runChecks(): Promise<void> {
    this.checks?.abort();
    const controller = new AbortController();
    this.checks = controller;
    const pending = this.state.rows.filter((row) => row.check.kind === "pending");
    this.set({ phase: "checking", progress: { done: 0, total: pending.length } });
    for (const [index, row] of pending.entries()) {
      let check: RowCheck;
      let sha256: string | null = null;
      try {
        sha256 = await this.hash(row.file);
        check = { kind: "hashed" };
      } catch {
        check = { kind: "unreadable" };
      }
      if (controller.signal.aborted) return;
      this.updateRow(row.key, (each) => ({ ...each, sha256, check }));
      this.set({ progress: { done: index + 1, total: pending.length } });
    }
    await this.preflight(controller);
  }

  /** One preflight for every remaining file (section 10.3). Earlier results are cleared first,
   * so a failure leaves nothing to upload. */
  private async preflight(controller: AbortController): Promise<void> {
    const candidates = this.state.rows.filter(
      (row) => row.check.kind === "hashed" || row.check.kind === "preflight",
    );
    if (candidates.length === 0) {
      this.set({ phase: this.state.rows.length === 0 ? "idle" : "ready", progress: null });
      return;
    }
    const keys = new Set(candidates.map((row) => row.key));
    this.updateRows((row) => (keys.has(row.key) ? { ...row, check: { kind: "hashed" } } : row));
    this.set({
      phase: "checking",
      progress: { done: candidates.length, total: candidates.length },
      failure: null,
    });
    const files = candidates.map((row) => ({
      name: row.file.name,
      sizeBytes: row.file.size,
      sha256: row.sha256 ?? "",
    }));
    try {
      const results = await this.withBurstWaits(
        () => this.api.preflight(files, controller.signal),
        controller.signal,
      );
      if (controller.signal.aborted) return;
      const byKey = new Map(candidates.map((row, index) => [row.key, results[index]]));
      this.updateRows((row) => {
        const result = byKey.get(row.key);
        if (result === undefined) return row;
        return {
          ...row,
          check: { kind: "preflight", result },
          confirmUpdate: result.status === "update" && row.confirmUpdate,
        };
      });
      this.set({ phase: "ready", progress: null });
    } catch (reason) {
      if (controller.signal.aborted) return;
      const error = apiErrorOf(reason) ?? clientError("network");
      if (error.kind === "aborted") return;
      this.set({
        phase: "ready",
        progress: null,
        failure: { step: "preflight", error, paused: isScanningPause(error) },
      });
    }
  }

  /** The confirmation checkbox of an `update` row (section 10.4). */
  setConfirm(key: string, confirmed: boolean): void {
    if (this.state.phase !== "ready") return;
    this.updateRow(key, (row) => ({ ...row, confirmUpdate: confirmed }));
  }

  /** Upload: each chosen file in the table's order, one at a time, then one submit. */
  start(projectId: string): void {
    if (this.state.phase !== "ready" || this.state.failure !== null) return;
    const chosen = this.state.rows.filter(isChosen).map((row) => row.key);
    if (chosen.length === 0) return;
    this.projectId = projectId;
    this.chosen = chosen;
    this.stopRequested = false;
    this.submitAfterStop = true;
    this.register();
    this.set({ phase: "uploading", summary: null });
    void this.uploadFrom(0);
  }

  /** Stop uploading: aborts the current request, skips the files not yet sent and goes to the
   * submit. */
  stop(): void {
    if (this.state.phase !== "uploading" || this.state.failure !== null) return;
    this.stopRequested = true;
    this.current?.abort();
  }

  /** A sign-out: the same as Stop, without the submit, because nothing may be sent any more. */
  private abortAll(): void {
    this.stopRequested = true;
    this.submitAfterStop = false;
    this.current?.abort();
    this.checks?.abort();
  }

  /** Retry repeats the refused step: the preflight, the paused file onwards, or the submit. */
  retry(): void {
    const { failure } = this.state;
    if (failure === null) return;
    if (failure.step === "preflight") {
      this.checks?.abort();
      const controller = new AbortController();
      this.checks = controller;
      void this.preflight(controller);
      return;
    }
    this.stopRequested = false;
    this.submitAfterStop = true;
    this.register();
    if (failure.step === "upload") {
      const waiting = new Set(this.chosen.slice(this.pausedAt));
      this.updateRows((row) =>
        waiting.has(row.key) && row.upload.kind === "paused"
          ? { ...row, upload: { kind: "none" } }
          : row,
      );
      this.set({ failure: null, phase: "uploading" });
      void this.uploadFrom(this.pausedAt);
      return;
    }
    this.set({ failure: null });
    void this.submit();
  }

  /** The panel closes: an upload under way stops and still submits what went up; a submit
   * finishes; anything else is discarded. */
  close(): void {
    const { phase, failure } = this.state;
    if (phase === "uploading" && failure === null) {
      this.stop();
      return;
    }
    if (phase === "submitting" && failure === null) return;
    this.reset();
  }

  /** The screen unmounts: the same as closing the panel. The batch stays usable, so a remount
   * finds it as it was left. */
  dispose(): void {
    this.close();
  }

  private reset(): void {
    this.checks?.abort();
    this.checks = null;
    this.chosen = [];
    this.pausedAt = 0;
    this.release();
    this.set(INITIAL);
  }

  private register(): void {
    this.release();
    this.unregister = registerActiveWork({
      abort: () => {
        this.abortAll();
      },
    });
  }

  private release(): void {
    this.unregister?.();
    this.unregister = null;
  }

  /** An upload run ended: it is no longer active work, and the screen refreshes. */
  private endRun(): void {
    this.release();
    this.onChanged?.();
  }

  /** Section 10.8: a request answered `429 too_many_requests` waits `retryAfterSeconds` and is
   * sent once more, at most 3 times, before the step's failure rules apply. */
  private async withBurstWaits<T>(send: () => Promise<T>, signal: AbortSignal): Promise<T> {
    for (let waits = 0; ; waits += 1) {
      try {
        return await send();
      } catch (reason) {
        const error = apiErrorOf(reason);
        const at = error === null ? null : retryAtOf(error);
        if (
          error === null ||
          at === null ||
          !isProblem(error, 429, "too_many_requests") ||
          waits >= BATCH_RATE_LIMIT_WAITS
        ) {
          throw reason;
        }
        await this.sleep(at - Date.now(), signal);
        if (signal.aborted) throw new ApiFailure(clientError("aborted"));
      }
    }
  }

  private async uploadFrom(start: number): Promise<void> {
    const keys = this.chosen;
    for (let index = start; index < keys.length; index += 1) {
      const key = keys[index] ?? "";
      if (this.stopRequested) {
        this.markRemaining(keys.slice(index), { kind: "stopped" });
        break;
      }
      const controller = new AbortController();
      this.current = controller;
      const outcome = await this.uploadOne(key, controller.signal);
      this.current = null;
      if (outcome.kind === "paused") {
        this.pausedAt = index;
        this.markRemaining(keys.slice(index), { kind: "paused" });
        this.set({ failure: { step: "upload", error: outcome.error, paused: true } });
        this.endRun();
        return;
      }
      this.updateRow(key, (row) => ({ ...row, upload: outcome }));
    }
    if (!this.submitAfterStop) {
      this.endRun();
      return;
    }
    await this.submit();
  }

  /** Marks the files of `keys` that aren't uploaded yet. */
  private markRemaining(keys: readonly string[], upload: RowUpload): void {
    const set = new Set(keys);
    this.updateRows((row) =>
      set.has(row.key) && row.upload.kind !== "done" ? { ...row, upload } : row,
    );
  }

  /** One file: reserve, then send its bytes (section 10.5 steps 1 and 2). */
  private async uploadOne(key: string, signal: AbortSignal): Promise<FileOutcome> {
    const row = this.row(key);
    if (row?.check.kind !== "preflight" || row.sha256 === null) return STOPPED;
    const body: UploadReservationRequest = {
      name: row.file.name,
      sizeBytes: row.file.size,
      sha256: row.sha256,
      projectId: this.projectId,
      // True only for an update the admin confirmed (section 10.4).
      confirmUpdate: row.confirmUpdate && row.check.result.status === "update",
    };
    this.updateRow(key, (each) => ({ ...each, upload: { kind: "uploading", percent: 0 } }));
    let reservation: UploadReservation;
    try {
      reservation = await this.withBurstWaits(() => this.api.reserve(body, signal), signal);
    } catch (reason) {
      const error = apiErrorOf(reason);
      if (signal.aborted || error === null || error.kind === "aborted") return STOPPED;
      if (isScanningPause(error)) return { kind: "paused", error };
      if (isSkip(error)) return { kind: "skipped", reason: error.detail ?? "" };
      return { kind: "failed", reason: failureReason(error) };
    }
    return this.sendContent(key, row.file, reservation.versionId, signal);
  }

  /** The bytes, by `XMLHttpRequest` in `http.ts`, with the retries and waits of section 10.5. */
  private async sendContent(
    key: string,
    file: File,
    versionId: string,
    signal: AbortSignal,
  ): Promise<FileOutcome> {
    let failures = 0;
    let busyWaits = 0;
    let burstWaits = 0;
    for (;;) {
      if (signal.aborted) return STOPPED;
      try {
        await this.api.content(versionId, file, signal, (loaded, total) => {
          const percent = total > 0 ? Math.floor((loaded / total) * 100) : 0;
          this.updateRow(key, (row) =>
            row.upload.kind === "uploading"
              ? { ...row, upload: { kind: "uploading", percent } }
              : row,
          );
        });
        return { kind: "done", versionId };
      } catch (reason) {
        const error = apiErrorOf(reason);
        if (wasAborted(signal) || error === null || error.kind === "aborted") return STOPPED;
        if (isScanningPause(error)) return { kind: "paused", error };
        const at = retryAtOf(error);
        if (isProblem(error, 429, "upload_busy") && at !== null && busyWaits < UPLOAD_BUSY_WAITS) {
          busyWaits += 1;
          await this.sleep(at - Date.now(), signal);
          continue;
        }
        if (
          isProblem(error, 429, "too_many_requests") &&
          at !== null &&
          burstWaits < BATCH_RATE_LIMIT_WAITS
        ) {
          burstWaits += 1;
          await this.sleep(at - Date.now(), signal);
          continue;
        }
        if (isTransientUploadFailure(error)) {
          if (failures < UPLOAD_RETRIES) {
            await this.sleep(UPLOAD_RETRY_DELAYS_MS[failures] ?? 0, signal);
            failures += 1;
            continue;
          }
          return { kind: "failed", reason: failureReason(error) };
        }
        if (isSkip(error)) return { kind: "skipped", reason: error.detail ?? "" };
        return { kind: "failed", reason: failureReason(error) };
      }
    }
  }

  /** One submit with every uploaded version (section 10.6). */
  private async submit(): Promise<void> {
    const versionIds = this.state.rows.flatMap((row) =>
      row.upload.kind === "done" ? [row.upload.versionId] : [],
    );
    if (versionIds.length === 0) {
      this.set({ phase: "done", summary: null });
      this.endRun();
      return;
    }
    this.set({ phase: "submitting" });
    const controller = new AbortController();
    this.current = controller;
    try {
      const result = await this.withBurstWaits(
        () => this.api.submit(versionIds, controller.signal),
        controller.signal,
      );
      const submitted = result.results.filter(
        (each) => each.outcome === "queued" || each.outcome === "already_queued",
      ).length;
      const summary: UploadSummary = {
        runId: result.runId,
        submitted,
        total: this.chosen.length,
      };
      this.set({ phase: "done", summary });
    } catch (reason) {
      const error = controller.signal.aborted ? null : apiErrorOf(reason);
      if (error !== null && error.kind !== "aborted") {
        this.set({ failure: { step: "submit", error, paused: isScanningPause(error) } });
      }
    } finally {
      this.current = null;
      this.endRun();
    }
  }
}
