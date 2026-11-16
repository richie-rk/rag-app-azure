import { afterEach, describe, expect, test, vi } from "vitest";

import type {
  UploadPreflightFile,
  UploadPreflightResult,
  UploadReservation,
  UploadReservationRequest,
  UploadSubmitResult,
} from "../../api/generated/types.gen";
import { ApiFailure, clientError, type ApiError } from "../../api/problem";
import { abortAllWork, hasActiveWork } from "../../platform/activeWork";
import { indexOptions, uuid } from "../../test/adminData";
import { CORRELATION_ID } from "../../test/handlers";
import {
  UploadBatch,
  abortableSleep,
  chosenCount,
  failureReason,
  isTransientUploadFailure,
  nothingToUpload,
  type UploadApi,
  type UploadState,
} from "./uploadBatch";

const LIMITS = indexOptions().uploadLimits;
const PROJECT = uuid(1);

function pdf(name: string, size = 6): File {
  return new File(["%PDF-1".slice(0, size).padEnd(size, "x")], name);
}

interface ProblemFields {
  readonly detail?: string;
  readonly retryAfterSeconds?: number;
}

function problem(status: number, code: string, fields: ProblemFields = {}): ApiFailure {
  const error: ApiError = {
    ...clientError("problem", { status, code, traceId: "a".repeat(32) }),
    title: `Title of ${code}`,
    detail: fields.detail ?? `Detail of ${code}.`,
    retryAfterSeconds: fields.retryAfterSeconds ?? null,
    referenceId: CORRELATION_ID,
  };
  return new ApiFailure(error);
}

function failure(kind: ApiError["kind"], fields: Parameters<typeof clientError>[1] = {}) {
  return new ApiFailure(clientError(kind, fields));
}

type Status = UploadPreflightResult["status"];

/** Preflight results by file name: `update…`, `dup…`, `same…` and `bad…` names, else `new`. */
function preflightOf(file: UploadPreflightFile): UploadPreflightResult {
  const status: Status = file.name.startsWith("update")
    ? "update"
    : file.name.startsWith("dup")
      ? "duplicate"
      : file.name.startsWith("same")
        ? "unchanged"
        : file.name.startsWith("bad")
          ? "rejected"
          : "new";
  return {
    name: file.name,
    status,
    reason: status === "rejected" ? "invalid_name" : null,
    documentId: status === "new" ? null : uuid(200),
    existingDocument: status === "duplicate" ? { documentId: uuid(201), name: "other.pdf" } : null,
    currentVersionNumber: status === "new" ? null : 1,
  };
}

interface Script {
  preflight?: (
    files: readonly UploadPreflightFile[],
    call: number,
  ) => Promise<UploadPreflightResult[]>;
  reserve?: (body: UploadReservationRequest, call: number) => Promise<UploadReservation>;
  content?: (
    versionId: string,
    signal: AbortSignal,
    onProgress: (loaded: number, total: number) => void,
    call: number,
  ) => Promise<unknown>;
  submit?: (versionIds: readonly string[], call: number) => Promise<UploadSubmitResult>;
}

/** A scripted API recording every call; by default every file is new and uploads. */
function fakeApi(script: Script = {}) {
  const calls = {
    preflight: [] as (readonly UploadPreflightFile[])[],
    reserve: [] as UploadReservationRequest[],
    content: [] as string[],
    submit: [] as (readonly string[])[],
  };
  let inFlight = 0;
  let mostInFlight = 0;
  const track = async <T>(work: () => Promise<T>): Promise<T> => {
    inFlight += 1;
    mostInFlight = Math.max(mostInFlight, inFlight);
    try {
      return await work();
    } finally {
      inFlight -= 1;
    }
  };
  const api: UploadApi = {
    preflight: (files) => {
      calls.preflight.push(files);
      const call = calls.preflight.length;
      return script.preflight?.(files, call) ?? Promise.resolve(files.map(preflightOf));
    },
    reserve: (body) => {
      calls.reserve.push(body);
      const call = calls.reserve.length;
      return track(
        () =>
          script.reserve?.(body, call) ??
          Promise.resolve({
            documentId: uuid(200 + call),
            versionId: uuid(300 + call),
            versionNumber: 1,
            reused: false,
          }),
      );
    },
    content: (versionId, _file, signal, onProgress) => {
      calls.content.push(versionId);
      const call = calls.content.length;
      return track(
        () => script.content?.(versionId, signal, onProgress, call) ?? Promise.resolve({}),
      );
    },
    submit: (versionIds) => {
      calls.submit.push(versionIds);
      const call = calls.submit.length;
      return (
        script.submit?.(versionIds, call) ??
        Promise.resolve({
          runId: uuid(400),
          results: versionIds.map((versionId) => ({ versionId, outcome: "queued" as const })),
        })
      );
    },
  };
  return { api, calls, mostInFlight: () => mostInFlight };
}

/** The SHA-256 the fake hasher gives a file: its name, padded. */
function shaOf(file: File): string {
  return file.name.replace(/\W/g, "").padStart(64, "0").slice(-64);
}

function newBatch(
  api: UploadApi,
  extra: Partial<ConstructorParameters<typeof UploadBatch>[0]> = {},
) {
  const sleeps: number[] = [];
  const onChanged = vi.fn();
  const batch = new UploadBatch({
    api,
    hash: (file) => Promise.resolve(shaOf(file)),
    sleep: (ms) => {
      sleeps.push(ms);
      return Promise.resolve();
    },
    onChanged,
    ...extra,
  });
  return { batch, sleeps, onChanged };
}

/** Resolves once the batch's state passes `predicate`. */
function until(
  batch: UploadBatch,
  predicate: (state: UploadState) => boolean,
): Promise<UploadState> {
  return new Promise((resolve) => {
    let unsubscribe: () => void = () => undefined;
    const check = () => {
      const state = batch.getState();
      if (!predicate(state)) return;
      unsubscribe();
      resolve(state);
    };
    unsubscribe = batch.subscribe(check);
    check();
  });
}

const ready = (state: UploadState) => state.phase === "ready";
const done = (state: UploadState) => state.phase === "done";
const halted = (state: UploadState) => state.failure !== null;

function rowOf(state: UploadState, name: string) {
  const row = state.rows.find((each) => each.file.name === name);
  if (row === undefined) throw new Error(`no row ${name}`);
  return row;
}

afterEach(() => {
  abortAllWork();
  vi.useRealTimers();
});

describe("checks and preflight (sections 10.1 to 10.4)", () => {
  test("T-403 rejected files are never hashed; the rest are hashed one at a time, then preflighted once", async () => {
    const { api, calls } = fakeApi();
    const hashed: string[] = [];
    let hashing = 0;
    let mostHashing = 0;
    const { batch } = newBatch(api, {
      hash: async (file) => {
        hashing += 1;
        mostHashing = Math.max(mostHashing, hashing);
        hashed.push(file.name);
        await Promise.resolve();
        hashing -= 1;
        return shaOf(file);
      },
    });
    const big = pdf("big.pdf");
    Object.defineProperty(big, "size", { value: LIMITS.maxFileBytes + 1 });
    batch.choose([pdf("a.pdf"), pdf("B.PDF"), pdf("sheet.xlsx"), pdf("image.png"), big], LIMITS);
    const state = await until(batch, ready);
    expect(hashed).toEqual(["a.pdf", "B.PDF"]);
    expect(mostHashing).toBe(1);
    expect(calls.preflight).toEqual([
      [
        { name: "a.pdf", sizeBytes: 6, sha256: shaOf(pdf("a.pdf")) },
        { name: "B.PDF", sizeBytes: 6, sha256: shaOf(pdf("B.PDF")) },
      ],
    ]);
    expect(rowOf(state, "sheet.xlsx").check).toEqual({ kind: "rejectedType" });
    expect(rowOf(state, "image.png").check).toEqual({ kind: "rejectedType" });
    expect(rowOf(state, "big.pdf").check).toEqual({ kind: "rejectedSize" });
    expect(rowOf(state, "a.pdf").check.kind).toBe("preflight");
    expect(chosenCount(state)).toBe(2);
  });

  test("T-403 a selection that would pass maxFilesPerBatch adds nothing and says so", async () => {
    const { api, calls } = fakeApi();
    const { batch } = newBatch(api);
    const limits = { ...LIMITS, maxFilesPerBatch: 2 };
    batch.choose([pdf("a.pdf"), pdf("b.pdf"), pdf("c.pdf")], limits);
    expect(batch.getState()).toMatchObject({ tooMany: true, rows: [], phase: "idle" });
    batch.choose([pdf("a.pdf")], limits);
    expect(batch.getState().tooMany).toBe(false);
    await until(batch, ready);
    batch.choose([pdf("b.pdf"), pdf("c.pdf")], limits);
    expect(batch.getState()).toMatchObject({ tooMany: true });
    expect(batch.getState().rows).toHaveLength(1);
    expect(calls.preflight).toHaveLength(1);
  });

  test("progress counts the files hashed; an unreadable file is listed and left out", async () => {
    const { api, calls } = fakeApi();
    const progress: string[] = [];
    const { batch } = newBatch(api, {
      hash: (file) =>
        file.name === "broken.pdf"
          ? Promise.reject(new Error("unreadable"))
          : Promise.resolve(shaOf(file)),
    });
    batch.subscribe(() => {
      const value = batch.getState().progress;
      if (value !== null) progress.push(`${String(value.done)}/${String(value.total)}`);
    });
    batch.choose([pdf("broken.pdf"), pdf("a.pdf")], LIMITS);
    const state = await until(batch, ready);
    expect(progress).toContain("0/2");
    expect(progress).toContain("1/2");
    expect(progress).toContain("2/2");
    expect(rowOf(state, "broken.pdf").check).toEqual({ kind: "unreadable" });
    expect(calls.preflight[0]?.map((file) => file.name)).toEqual(["a.pdf"]);
  });

  test("updates wait for their checkbox; skipped files never count", async () => {
    const { api } = fakeApi();
    const { batch } = newBatch(api);
    batch.choose([pdf("new.pdf"), pdf("update.pdf"), pdf("dup.pdf"), pdf("same.pdf")], LIMITS);
    let state = await until(batch, ready);
    expect(chosenCount(state)).toBe(1);
    expect(nothingToUpload(state)).toBe(false);
    batch.setConfirm(rowOf(state, "update.pdf").key, true);
    state = batch.getState();
    expect(chosenCount(state)).toBe(2);
    batch.setConfirm(rowOf(state, "update.pdf").key, false);
    expect(chosenCount(batch.getState())).toBe(1);
  });

  test("nothing to upload when every file was skipped", async () => {
    const { api, calls } = fakeApi();
    const { batch } = newBatch(api);
    batch.choose([pdf("dup.pdf"), pdf("same.pdf"), pdf("bad.pdf"), pdf("x.zip")], LIMITS);
    const state = await until(batch, ready);
    expect(nothingToUpload(state)).toBe(true);
    expect(chosenCount(state)).toBe(0);
    batch.start(PROJECT);
    expect(batch.getState().phase).toBe("ready");
    expect(calls.reserve).toEqual([]);
  });

  test("only rejected files need no preflight", async () => {
    const { api, calls } = fakeApi();
    const { batch } = newBatch(api);
    batch.choose([pdf("x.zip")], LIMITS);
    const state = await until(batch, ready);
    expect(calls.preflight).toEqual([]);
    expect(nothingToUpload(state)).toBe(true);
  });

  test("a second selection preflights every remaining file again and keeps confirmations", async () => {
    const { api, calls } = fakeApi();
    const { batch } = newBatch(api);
    batch.choose([pdf("update.pdf")], LIMITS);
    let state = await until(batch, ready);
    batch.setConfirm(rowOf(state, "update.pdf").key, true);
    batch.choose([pdf("b.pdf")], LIMITS);
    expect(batch.getState().phase).toBe("checking");
    // A selection while checking is ignored.
    batch.choose([pdf("c.pdf")], LIMITS);
    state = await until(batch, ready);
    expect(calls.preflight.map((files) => files.map((file) => file.name))).toEqual([
      ["update.pdf"],
      ["update.pdf", "b.pdf"],
    ]);
    expect(rowOf(state, "update.pdf").confirmUpdate).toBe(true);
    expect(chosenCount(state)).toBe(2);
  });

  test("a failed preflight shows on the batch; Retry repeats it with the same hashes", async () => {
    const { api, calls } = fakeApi({
      preflight: (files, call) =>
        call === 1 ? Promise.reject(failure("network")) : Promise.resolve(files.map(preflightOf)),
    });
    const { batch } = newBatch(api);
    batch.choose([pdf("a.pdf")], LIMITS);
    let state = await until(batch, halted);
    expect(state.failure).toMatchObject({ step: "preflight", paused: false });
    expect(chosenCount(state)).toBe(0);
    batch.retry();
    state = await until(batch, (each) => each.phase === "ready" && each.failure === null);
    expect(calls.preflight[1]).toEqual(calls.preflight[0]);
    expect(chosenCount(state)).toBe(1);
  });

  test("a preflight answered 429 too_many_requests waits and resends, at most 3 times", async () => {
    const { api, calls } = fakeApi({
      preflight: (files, call) =>
        call <= 4
          ? Promise.reject(problem(429, "too_many_requests", { retryAfterSeconds: 1 }))
          : Promise.resolve(files.map(preflightOf)),
    });
    const { batch, sleeps } = newBatch(api);
    batch.choose([pdf("a.pdf")], LIMITS);
    const state = await until(batch, halted);
    expect(calls.preflight).toHaveLength(4);
    expect(sleeps).toHaveLength(3);
    expect(state.failure?.error.code).toBe("too_many_requests");
  });

  test("T-407 503 malware_scanning_not_ready from the preflight pauses the batch", async () => {
    const { api, calls } = fakeApi({
      preflight: () =>
        Promise.reject(problem(503, "malware_scanning_not_ready", { retryAfterSeconds: 3600 })),
    });
    const { batch } = newBatch(api);
    batch.choose([pdf("a.pdf")], LIMITS);
    const state = await until(batch, halted);
    expect(state.failure).toMatchObject({ step: "preflight", paused: true });
    expect(calls.preflight).toHaveLength(1);
    // No new selection is taken until the retry time.
    expect(batch.canChoose()).toBe(false);
    batch.choose([pdf("b.pdf")], LIMITS);
    expect(batch.getState().rows).toHaveLength(1);
    expect(batch.canChoose(Date.now() + 3_600_000)).toBe(true);
  });
});

describe("uploading and submitting (sections 10.5 and 10.6)", () => {
  async function readyBatch(names: string[], script: Script = {}, confirm: string[] = []) {
    const fake = fakeApi(script);
    const made = newBatch(fake.api);
    made.batch.choose(
      names.map((name) => pdf(name)),
      LIMITS,
    );
    const state = await until(made.batch, ready);
    for (const name of confirm) made.batch.setConfirm(rowOf(state, name).key, true);
    return { ...fake, ...made };
  }

  test("T-404 only the new file and the confirmed update go up, one at a time, and one submit carries both", async () => {
    const { batch, calls, mostInFlight, onChanged } = await readyBatch(
      ["new.pdf", "update.pdf", "dup.pdf"],
      {},
      ["update.pdf"],
    );
    batch.start(PROJECT);
    expect(hasActiveWork()).toBe(true);
    const state = await until(batch, done);
    expect(calls.reserve).toEqual([
      {
        name: "new.pdf",
        sizeBytes: 6,
        sha256: shaOf(pdf("new.pdf")),
        projectId: PROJECT,
        confirmUpdate: false,
      },
      {
        name: "update.pdf",
        sizeBytes: 6,
        sha256: shaOf(pdf("update.pdf")),
        projectId: PROJECT,
        confirmUpdate: true,
      },
    ]);
    expect(calls.content).toEqual([uuid(301), uuid(302)]);
    expect(mostInFlight()).toBe(1);
    expect(calls.submit).toEqual([[uuid(301), uuid(302)]]);
    expect(state.summary).toEqual({ runId: uuid(400), submitted: 2, total: 2 });
    expect(rowOf(state, "new.pdf").upload).toEqual({ kind: "done", versionId: uuid(301) });
    expect(rowOf(state, "dup.pdf").upload).toEqual({ kind: "none" });
    expect(hasActiveWork()).toBe(false);
    expect(onChanged).toHaveBeenCalledTimes(1);
  });

  test("progress events move the file's percentage", async () => {
    const percents: number[] = [];
    let release: () => void = () => undefined;
    const { batch } = await readyBatch(["a.pdf"], {
      content: (_id, _signal, onProgress) =>
        new Promise((resolve) => {
          onProgress(1, 4);
          onProgress(3, 4);
          onProgress(0, 0);
          release = () => {
            resolve({});
          };
        }),
    });
    batch.subscribe(() => {
      const upload = batch.getState().rows[0]?.upload;
      if (upload?.kind === "uploading") percents.push(upload.percent);
    });
    batch.start(PROJECT);
    await until(batch, (state) => state.rows[0]?.upload.kind === "uploading");
    await vi.waitFor(() => {
      expect(percents).toContain(75);
    });
    expect(percents).toContain(25);
    release();
    await until(batch, done);
  });

  test("a failed attempt is retried after 2,000 ms, then 6,000 ms, then the file fails", async () => {
    const { batch, calls, sleeps } = await readyBatch(["a.pdf", "b.pdf"], {
      content: (versionId) =>
        versionId === uuid(301)
          ? Promise.reject(failure("network", { traceId: "b".repeat(32) }))
          : Promise.resolve({}),
    });
    batch.start(PROJECT);
    const state = await until(batch, done);
    expect(sleeps).toEqual([2_000, 6_000]);
    expect(calls.content).toEqual([uuid(301), uuid(301), uuid(301), uuid(302)]);
    expect(rowOf(state, "a.pdf").upload).toEqual({
      kind: "failed",
      reason: "Check your connection, then select Retry.",
    });
    expect(calls.submit).toEqual([[uuid(302)]]);
    expect(state.summary).toEqual({ runId: uuid(400), submitted: 1, total: 2 });
  });

  test("a 5xx, a 408 and a stall are retried too, and the third try can succeed", async () => {
    const answers = [
      failure("http", { status: 502 }),
      problem(408, "request_timeout"),
      failure("network", { code: "upload_stalled" }),
    ];
    const { batch, sleeps } = await readyBatch(["a.pdf"], {
      content: (_id, _signal, _progress, call) =>
        call <= 2 ? Promise.reject(answers[call - 1] ?? failure("network")) : Promise.resolve({}),
    });
    batch.start(PROJECT);
    const state = await until(batch, done);
    expect(sleeps).toEqual([2_000, 6_000]);
    expect(rowOf(state, "a.pdf").upload.kind).toBe("done");
  });

  test("the reason after the last retry: the stall's text or the problem's detail", async () => {
    const stalled = await readyBatch(["a.pdf"], {
      content: () => Promise.reject(failure("network", { code: "upload_stalled" })),
    });
    stalled.batch.start(PROJECT);
    expect(rowOf(await until(stalled.batch, done), "a.pdf").upload).toEqual({
      kind: "failed",
      reason: "The upload stopped making progress.",
    });
    const unavailable = await readyBatch(["a.pdf"], {
      content: () =>
        Promise.reject(problem(503, "storage_unavailable", { detail: "Storage is down." })),
    });
    unavailable.batch.start(PROJECT);
    expect(rowOf(await until(unavailable.batch, done), "a.pdf").upload).toEqual({
      kind: "failed",
      reason: "Storage is down.",
    });
  });

  test("T-406 429 upload_busy waits retryAfterSeconds before the next attempt (fake timers)", async () => {
    vi.useFakeTimers();
    const fake = fakeApi({
      content: (_id, _signal, _progress, call) =>
        call === 1
          ? Promise.reject(problem(429, "upload_busy", { retryAfterSeconds: 5 }))
          : Promise.resolve({}),
    });
    const batch = new UploadBatch({ api: fake.api, hash: (file) => Promise.resolve(shaOf(file)) });
    batch.choose([pdf("a.pdf")], LIMITS);
    await vi.advanceTimersByTimeAsync(0);
    expect(batch.getState().phase).toBe("ready");
    batch.start(PROJECT);
    await vi.advanceTimersByTimeAsync(4_999);
    expect(fake.calls.content).toHaveLength(1);
    await vi.advanceTimersByTimeAsync(1);
    expect(fake.calls.content).toHaveLength(2);
    await vi.advanceTimersByTimeAsync(0);
    expect(batch.getState().phase).toBe("done");
    expect(fake.calls.submit).toHaveLength(1);
  });

  test("upload_busy waits at most 6 times and doesn't use up the failure retries", async () => {
    const { batch, calls, sleeps } = await readyBatch(["a.pdf"], {
      content: (_id, _signal, _progress, call) =>
        call === 1
          ? Promise.reject(failure("network"))
          : Promise.reject(problem(429, "upload_busy", { retryAfterSeconds: 5 })),
    });
    batch.start(PROJECT);
    const state = await until(batch, done);
    expect(calls.content).toHaveLength(8);
    expect(sleeps).toHaveLength(7);
    expect(sleeps[0]).toBe(2_000);
    expect(rowOf(state, "a.pdf").upload).toEqual({
      kind: "failed",
      reason: "Detail of upload_busy.",
    });
    expect(calls.submit).toEqual([]);
    expect(state.summary).toBeNull();
  });

  test("T-406 reserve answered 429 too_many_requests waits 1 s and sends the same request; a fourth fails the file (fake timers)", async () => {
    vi.useFakeTimers();
    const fake = fakeApi({
      reserve: (body, call) =>
        body.name === "a.pdf" || call <= 7
          ? Promise.reject(problem(429, "too_many_requests", { retryAfterSeconds: 1 }))
          : Promise.resolve({
              documentId: uuid(250),
              versionId: uuid(350),
              versionNumber: 1,
              reused: false,
            }),
    });
    const batch = new UploadBatch({ api: fake.api, hash: (file) => Promise.resolve(shaOf(file)) });
    batch.choose([pdf("a.pdf"), pdf("b.pdf")], LIMITS);
    await vi.advanceTimersByTimeAsync(0);
    batch.start(PROJECT);
    await vi.advanceTimersByTimeAsync(0);
    expect(fake.calls.reserve).toHaveLength(1);
    await vi.advanceTimersByTimeAsync(999);
    expect(fake.calls.reserve).toHaveLength(1);
    await vi.advanceTimersByTimeAsync(1);
    expect(fake.calls.reserve).toHaveLength(2);
    expect(fake.calls.reserve[1]).toEqual(fake.calls.reserve[0]);
    await vi.advanceTimersByTimeAsync(2_000);
    // The fourth 429 for a.pdf falls to the step's failure rule; b.pdf is still sent.
    expect(fake.calls.reserve).toHaveLength(5);
    expect(rowOf(batch.getState(), "a.pdf").upload).toEqual({
      kind: "failed",
      reason: "Detail of too_many_requests.",
    });
    expect(fake.calls.reserve[4]?.name).toBe("b.pdf");
    await vi.advanceTimersByTimeAsync(3_000);
    expect(batch.getState().phase).toBe("done");
    expect(fake.calls.submit).toEqual([[uuid(350)]]);
  });

  test("the content step waits on 429 too_many_requests up to 3 times, then fails the file", async () => {
    const { batch, calls, sleeps } = await readyBatch(["a.pdf"], {
      content: () => Promise.reject(problem(429, "too_many_requests", { retryAfterSeconds: 1 })),
    });
    batch.start(PROJECT);
    const state = await until(batch, done);
    expect(calls.content).toHaveLength(4);
    expect(sleeps).toHaveLength(3);
    expect(rowOf(state, "a.pdf").upload.kind).toBe("failed");
  });

  test("409 content_unchanged or duplicate_content skips the file with the detail", async () => {
    const { batch, calls } = await readyBatch(["a.pdf", "b.pdf", "c.pdf"], {
      reserve: (body, call) =>
        body.name === "a.pdf"
          ? Promise.reject(problem(409, "content_unchanged", { detail: "Already there." }))
          : Promise.resolve({
              documentId: uuid(200 + call),
              versionId: uuid(300 + call),
              versionNumber: 1,
              reused: true,
            }),
      content: (versionId) =>
        versionId === uuid(302)
          ? Promise.reject(problem(409, "duplicate_content", { detail: "Same bytes." }))
          : Promise.resolve({}),
    });
    batch.start(PROJECT);
    const state = await until(batch, done);
    expect(rowOf(state, "a.pdf").upload).toEqual({ kind: "skipped", reason: "Already there." });
    expect(rowOf(state, "b.pdf").upload).toEqual({ kind: "skipped", reason: "Same bytes." });
    expect(calls.submit).toEqual([[uuid(303)]]);
  });

  test("422 hash_mismatch, another 4xx and a failed reservation fail the file and go on", async () => {
    const { batch, calls } = await readyBatch(["a.pdf", "b.pdf", "c.pdf", "d.pdf"], {
      reserve: (body, call) =>
        body.name === "c.pdf"
          ? Promise.reject(problem(422, "project_not_mapped", { detail: "Not mapped." }))
          : body.name === "d.pdf"
            ? Promise.reject(failure("http", { status: 403 }))
            : Promise.resolve({
                documentId: uuid(200 + call),
                versionId: uuid(300 + call),
                versionNumber: 1,
                reused: false,
              }),
      content: (versionId) =>
        Promise.reject(
          versionId === uuid(301)
            ? problem(422, "hash_mismatch", { detail: "The file changed." })
            : problem(413, "file_too_large", { detail: "Too large." }),
        ),
    });
    batch.start(PROJECT);
    const state = await until(batch, done);
    expect(state.rows.map((row) => row.upload)).toEqual([
      { kind: "failed", reason: "The file changed." },
      { kind: "failed", reason: "Too large." },
      { kind: "failed", reason: "Not mapped." },
      {
        kind: "failed",
        reason:
          "DocChat had a problem on its side. Retry in a few minutes. If it keeps happening, send the reference ID to your DocChat administrator.",
      },
    ]);
    expect(calls.submit).toEqual([]);
  });

  test("T-407 503 malware_scanning_not_ready mid-batch isn't retried, pauses the rest and sends nothing more; Retry resumes", async () => {
    let paused = true;
    const { batch, calls, sleeps, onChanged } = await readyBatch(["a.pdf", "b.pdf", "c.pdf"], {
      content: (versionId) =>
        versionId === uuid(302) && paused
          ? Promise.reject(problem(503, "malware_scanning_not_ready", { retryAfterSeconds: 3600 }))
          : Promise.resolve({}),
    });
    batch.start(PROJECT);
    let state = await until(batch, halted);
    expect(state.failure).toMatchObject({ step: "upload", paused: true });
    expect(state.rows.map((row) => row.upload.kind)).toEqual(["done", "paused", "paused"]);
    expect(calls.content).toEqual([uuid(301), uuid(302)]);
    expect(calls.submit).toEqual([]);
    expect(sleeps).toEqual([]);
    expect(hasActiveWork()).toBe(false);
    expect(onChanged).toHaveBeenCalledTimes(1);
    // While halted, Stop and a new Upload do nothing.
    batch.stop();
    batch.start(PROJECT);
    expect(calls.reserve).toHaveLength(2);
    paused = false;
    batch.retry();
    expect(hasActiveWork()).toBe(true);
    state = await until(batch, done);
    expect(calls.reserve.map((body) => body.name)).toEqual(["a.pdf", "b.pdf", "b.pdf", "c.pdf"]);
    expect(calls.submit).toEqual([[uuid(301), uuid(303), uuid(304)]]);
    expect(state.summary).toEqual({ runId: uuid(400), submitted: 3, total: 3 });
  });

  test("503 malware_scanning_not_ready from the reservation pauses too", async () => {
    const { batch, calls } = await readyBatch(["a.pdf"], {
      reserve: () =>
        Promise.reject(problem(503, "malware_scanning_not_ready", { retryAfterSeconds: 3600 })),
    });
    batch.start(PROJECT);
    const state = await until(batch, halted);
    expect(state.failure).toMatchObject({ step: "upload", paused: true });
    expect(rowOf(state, "a.pdf").upload).toEqual({ kind: "paused" });
    expect(calls.content).toEqual([]);
  });

  test("the submit: a failure keeps the batch for Retry with the same IDs; a pause too", async () => {
    const { batch, calls } = await readyBatch(["a.pdf", "b.pdf"], {
      submit: (versionIds, call) =>
        call === 1
          ? Promise.reject(failure("network"))
          : call === 2
            ? Promise.reject(
                problem(503, "malware_scanning_not_ready", { retryAfterSeconds: 3600 }),
              )
            : Promise.resolve({
                runId: null,
                results: versionIds.map((versionId, index) => ({
                  versionId,
                  outcome: index === 0 ? ("already_queued" as const) : ("not_pending" as const),
                })),
              }),
    });
    batch.start(PROJECT);
    let state = await until(batch, halted);
    expect(state).toMatchObject({
      phase: "submitting",
      failure: { step: "submit", paused: false },
    });
    batch.retry();
    state = await until(batch, (each) => each.failure?.paused === true);
    expect(state.failure?.step).toBe("submit");
    batch.retry();
    state = await until(batch, done);
    expect(calls.submit).toEqual([
      [uuid(301), uuid(302)],
      [uuid(301), uuid(302)],
      [uuid(301), uuid(302)],
    ]);
    expect(state.summary).toEqual({ runId: null, submitted: 1, total: 2 });
  });

  test("a submit answered 429 too_many_requests waits and resends", async () => {
    const { batch, calls, sleeps } = await readyBatch(["a.pdf"], {
      submit: (versionIds, call) =>
        call === 1
          ? Promise.reject(problem(429, "too_many_requests", { retryAfterSeconds: 1 }))
          : Promise.resolve({
              runId: uuid(401),
              results: versionIds.map((versionId) => ({ versionId, outcome: "queued" as const })),
            }),
    });
    batch.start(PROJECT);
    const state = await until(batch, done);
    expect(calls.submit).toHaveLength(2);
    expect(sleeps).toHaveLength(1);
    expect(state.summary?.runId).toBe(uuid(401));
  });

  test("Stop uploading aborts the current file, skips the rest and still submits the uploaded ones", async () => {
    const { batch, calls } = await readyBatch(["a.pdf", "b.pdf", "c.pdf"], {
      content: (versionId, signal) =>
        versionId === uuid(302)
          ? new Promise((_resolve, reject) => {
              signal.addEventListener("abort", () => {
                reject(failure("aborted"));
              });
            })
          : Promise.resolve({}),
    });
    batch.start(PROJECT);
    await until(batch, (state) => state.rows[1]?.upload.kind === "uploading");
    await vi.waitFor(() => {
      expect(calls.content).toHaveLength(2);
    });
    batch.stop();
    const state = await until(batch, done);
    expect(state.rows.map((row) => row.upload.kind)).toEqual(["done", "stopped", "stopped"]);
    expect(calls.submit).toEqual([[uuid(301)]]);
    expect(state.summary).toEqual({ runId: uuid(400), submitted: 1, total: 3 });
  });

  test("a stop during a wait ends it at once", async () => {
    const fake = fakeApi({
      content: () => Promise.reject(failure("network")),
    });
    const batch = new UploadBatch({ api: fake.api, hash: (file) => Promise.resolve(shaOf(file)) });
    batch.choose([pdf("a.pdf")], LIMITS);
    await until(batch, ready);
    batch.start(PROJECT);
    await vi.waitFor(() => {
      expect(fake.calls.content).toHaveLength(1);
    });
    batch.stop();
    const state = await until(batch, done);
    expect(state.rows[0]?.upload).toEqual({ kind: "stopped" });
    expect(fake.calls.content).toHaveLength(1);
    expect(fake.calls.submit).toEqual([]);
  });

  test("closing the panel during the upload stops it; otherwise closing discards the batch", async () => {
    const { batch, calls } = await readyBatch(["a.pdf", "b.pdf"], {
      content: (versionId, signal) =>
        versionId === uuid(301)
          ? Promise.resolve({})
          : new Promise((_resolve, reject) => {
              signal.addEventListener("abort", () => {
                reject(failure("aborted"));
              });
            }),
    });
    batch.start(PROJECT);
    await vi.waitFor(() => {
      expect(calls.content).toHaveLength(2);
    });
    batch.close();
    const state = await until(batch, done);
    expect(calls.submit).toEqual([[uuid(301)]]);
    expect(state.summary?.submitted).toBe(1);
    batch.dispose();
    expect(batch.getState()).toMatchObject({ phase: "idle", rows: [], summary: null });
  });

  test("closing while checking discards the batch and its pending checks", async () => {
    const { api, calls } = fakeApi();
    let finish: (value: string) => void = () => undefined;
    const { batch } = newBatch(api, {
      hash: () =>
        new Promise<string>((resolve) => {
          finish = resolve;
        }),
    });
    batch.choose([pdf("a.pdf")], LIMITS);
    expect(batch.getState().phase).toBe("checking");
    batch.close();
    finish("f".repeat(64));
    await Promise.resolve();
    expect(batch.getState()).toMatchObject({ phase: "idle", rows: [] });
    expect(calls.preflight).toEqual([]);
  });

  test("a sign-out aborts the upload and sends no submit", async () => {
    const { batch, calls } = await readyBatch(["a.pdf", "b.pdf"], {
      content: (versionId, signal) =>
        versionId === uuid(301)
          ? Promise.resolve({})
          : new Promise((_resolve, reject) => {
              signal.addEventListener("abort", () => {
                reject(failure("aborted"));
              });
            }),
    });
    batch.start(PROJECT);
    await vi.waitFor(() => {
      expect(calls.content).toHaveLength(2);
    });
    expect(hasActiveWork()).toBe(true);
    abortAllWork();
    await vi.waitFor(() => {
      expect(hasActiveWork()).toBe(false);
    });
    expect(calls.submit).toEqual([]);
    expect(batch.getState().rows[1]?.upload).toEqual({ kind: "stopped" });
  });

  test("a sign-out during the submit leaves no failure", async () => {
    const { batch, calls } = await readyBatch(["a.pdf"], {
      submit: () => Promise.reject(failure("aborted")),
    });
    batch.start(PROJECT);
    await vi.waitFor(() => {
      expect(calls.submit).toHaveLength(1);
    });
    await vi.waitFor(() => {
      expect(hasActiveWork()).toBe(false);
    });
    expect(batch.getState().failure).toBeNull();
  });

  test("with nothing uploaded the batch ends without a submit or a summary", async () => {
    const { batch, calls, onChanged } = await readyBatch(["a.pdf"], {
      reserve: () => Promise.reject(problem(409, "duplicate_content")),
    });
    batch.start(PROJECT);
    const state = await until(batch, done);
    expect(calls.submit).toEqual([]);
    expect(state.summary).toBeNull();
    expect(onChanged).toHaveBeenCalledTimes(1);
  });

  test("after a finished batch a new selection starts a new one", async () => {
    const { batch, calls } = await readyBatch(["a.pdf"]);
    batch.start(PROJECT);
    await until(batch, done);
    // Nothing is taken while the batch can't take a selection.
    batch.retry();
    batch.choose([pdf("b.pdf")], LIMITS);
    const state = await until(batch, ready);
    expect(state.rows.map((row) => row.file.name)).toEqual(["b.pdf"]);
    expect(state.summary).toBeNull();
    expect(calls.preflight).toHaveLength(2);
  });
});

describe("helpers", () => {
  test("transient failures: no response, 408 and 5xx other than the scanning pause", () => {
    expect(isTransientUploadFailure(clientError("network"))).toBe(true);
    expect(isTransientUploadFailure(problem(408, "request_timeout").error)).toBe(true);
    expect(isTransientUploadFailure(clientError("http", { status: 500 }))).toBe(true);
    expect(isTransientUploadFailure(problem(503, "storage_unavailable").error)).toBe(true);
    expect(isTransientUploadFailure(problem(503, "malware_scanning_not_ready").error)).toBe(false);
    expect(isTransientUploadFailure(problem(422, "hash_mismatch").error)).toBe(false);
    expect(isTransientUploadFailure(clientError("unexpected", { status: 200 }))).toBe(false);
    expect(isTransientUploadFailure(clientError("http"))).toBe(false);
  });

  test("the failure reason", () => {
    expect(failureReason(clientError("network", { code: "upload_stalled" }))).toBe(
      "The upload stopped making progress.",
    );
    expect(failureReason(problem(422, "size_mismatch", { detail: "Size differs." }).error)).toBe(
      "Size differs.",
    );
    expect(failureReason({ ...problem(422, "x").error, detail: null })).toBe("Title of x");
    expect(failureReason({ ...problem(422, "x").error, detail: null, title: null })).toBe("");
    expect(failureReason(clientError("unexpected"))).toBe(
      "DocChat sent a reply this page couldn't read. Reload the page, then retry.",
    );
    expect(failureReason(clientError("aborted"))).toBe("");
  });

  test("abortableSleep ends at its time or when the signal aborts", async () => {
    vi.useFakeTimers();
    const controller = new AbortController();
    let woke = false;
    void abortableSleep(1_000, controller.signal).then(() => {
      woke = true;
    });
    await vi.advanceTimersByTimeAsync(500);
    expect(woke).toBe(false);
    controller.abort();
    await vi.advanceTimersByTimeAsync(0);
    expect(woke).toBe(true);
    await expect(abortableSleep(10, controller.signal)).resolves.toBeUndefined();
    let timed = false;
    void abortableSleep(-5, new AbortController().signal).then(() => {
      timed = true;
    });
    await vi.advanceTimersByTimeAsync(0);
    expect(timed).toBe(true);
  });
});
