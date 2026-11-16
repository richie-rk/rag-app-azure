// The only module that calls `fetch` or `XMLHttpRequest` (10-web-client.md sections 5.3 to 5.5,
// 10.9). Every API call gets a token, a new `traceparent`, the exact headers the API's CORS rule
// allows, one repeat after a `401` with a renewed token, and a classified answer: a parsed `2xx`
// or an `ApiFailure`.

import type { z } from "zod";

import "../bootstrap/zodSetup";
import { UPLOAD_STALL_MS } from "../platform/constants";
import { newTraceparent } from "../platform/traceparent";
import type { ChatTurnRequest } from "./generated/types.gen";
import { zProblem } from "./generated/zod.gen";
import {
  operations,
  type FileOperationId,
  type JsonOperationId,
  type OperationId,
  type RequestArgs,
  type ResponseOf,
} from "./operations";
import { ApiFailure, clientError, type ApiError } from "./problem";

export interface HttpDependencies {
  /** The API's origin from `config.json`. */
  readonly apiBaseUrl: string;
  /** The access token for the next request (section 4.4). */
  readonly getToken: () => Promise<string>;
  /** A token renewed from the refresh token, after a `401` (section 4.5). */
  readonly renewToken: () => Promise<string>;
  /** A second `401` in a row: the session has expired. */
  readonly onSessionExpired: () => void;
  /** A `403 role_not_assigned` with a problem body: the app root shows Not assigned. */
  readonly onRoleNotAssigned: () => void;
  /** A `403 admin_required`: the shell refetches `getCurrentUser` and shows Admin only. */
  readonly onAdminRequired: () => void;
}

let dependencies: HttpDependencies | null = null;
let halted = false;

/** Called once at start-up, after sign-in (section 4.2 step 8). */
export function configureHttp(value: HttpDependencies): void {
  dependencies = value;
  halted = false;
}

/** Stops every later call, as `aborted`: after Not assigned no other API call follows, and after
 * a sign-out none may start. */
export function haltApi(): void {
  halted = true;
}

function requireDependencies(): HttpDependencies {
  if (dependencies === null) throw new Error("configureHttp hasn't run");
  return dependencies;
}

/** `If-Match` carries the body's version: a memory ETag already holds its quotes, a `rowVersion`
 * is wrapped in them (section 5.6). */
export function ifMatchValue(version: string): string {
  return version.length >= 2 && version.startsWith('"') && version.endsWith('"')
    ? version
    : `"${version}"`;
}

type ParameterValue = string | number | boolean | null | undefined;

function buildUrl(
  base: string,
  template: string,
  path: Readonly<Record<string, ParameterValue>> | undefined,
  query: Readonly<Record<string, ParameterValue | readonly ParameterValue[]>> | undefined,
): string {
  const resolved = template.replace(/\{(\w+)\}/g, (_, name: string) => {
    const value = path?.[name];
    if (value === undefined || value === null) throw new Error(`missing path parameter ${name}`);
    return encodeURIComponent(String(value));
  });
  const search = new URLSearchParams();
  for (const [name, value] of Object.entries(query ?? {})) {
    for (const item of Array.isArray(value) ? value : [value]) {
      if (item !== undefined && item !== null) search.append(name, String(item));
    }
  }
  const text = search.toString();
  return `${base}${resolved}${text === "" ? "" : `?${text}`}`;
}

function mediaType(response: Response): string {
  return (response.headers.get("Content-Type") ?? "").split(";")[0]?.trim().toLowerCase() ?? "";
}

function isAbort(reason: unknown, signal: AbortSignal | undefined): boolean {
  return (
    signal?.aborted === true || (reason instanceof DOMException && reason.name === "AbortError")
  );
}

/** A token failure from `tokens.ts` is already classified; anything else counts as `network`. */
function tokenFailure(reason: unknown, operationId: OperationId): ApiFailure {
  return reason instanceof ApiFailure
    ? new ApiFailure({ ...reason.error, operationId })
    : new ApiFailure(clientError("network", { operationId, referenceId: null }));
}

/** A non-`2xx` other than the `401`s: a problem when the body is a valid problem, else `http`. */
async function classifyFailure(
  response: Response,
  operationId: OperationId,
  traceId: string,
): Promise<ApiError> {
  const base = clientError("http", { operationId, status: response.status, traceId });
  if (mediaType(response) !== "application/problem+json") return base;
  let body: unknown;
  try {
    body = JSON.parse(await response.text());
  } catch {
    return base;
  }
  const parsed = zProblem.safeParse(body);
  if (!parsed.success) return base;
  const problem = parsed.data;
  const error: ApiError = {
    ...base,
    kind: "problem",
    code: problem.code,
    title: problem.title,
    detail: problem.detail,
    errors: problem.errors ?? [],
    retryAfterSeconds: problem.retryAfterSeconds ?? null,
    referenceId: problem.correlationId,
  };
  const deps = requireDependencies();
  if (response.status === 403 && problem.code === "role_not_assigned") deps.onRoleNotAssigned();
  if (response.status === 403 && problem.code === "admin_required") deps.onAdminRequired();
  return error;
}

interface Sent {
  readonly response: Response;
  readonly traceId: string;
}

interface RawArgs {
  readonly path?: Readonly<Record<string, ParameterValue>>;
  readonly query?: Readonly<Record<string, ParameterValue | readonly ParameterValue[]>>;
  readonly body?: unknown;
  readonly ifMatch?: string;
  readonly signal?: AbortSignal;
}

const ACCEPT: Record<"json" | "ndjson" | "file", string> = {
  json: "application/json",
  ndjson: "application/x-ndjson",
  file: "*/*",
};

/** Sends one call: steps 1 to 4 of section 5.5, the `401` repeat included. Resolves with any
 * `2xx`; rejects with an `ApiFailure` otherwise. */
async function send(operationId: OperationId, args: RawArgs): Promise<Sent> {
  const deps = requireDependencies();
  const spec = operations[operationId];
  const answer = spec.answer === "upload" ? "json" : spec.answer;
  const url = buildUrl(deps.apiBaseUrl, spec.path, args.path, args.query);
  const body = args.body === undefined ? undefined : JSON.stringify(args.body);
  for (let attempt = 0; ; attempt += 1) {
    if (halted) throw new ApiFailure(clientError("aborted", { operationId }));
    // Every attempt, the 401 repeat included, carries a new trace ID.
    const trace = newTraceparent();
    let token: string;
    try {
      token = attempt === 0 ? await deps.getToken() : await deps.renewToken();
    } catch (reason) {
      throw tokenFailure(reason, operationId);
    }
    const headers: Record<string, string> = {
      Authorization: `Bearer ${token}`,
      Accept: ACCEPT[answer],
      traceparent: trace.header,
    };
    if (body !== undefined) headers["Content-Type"] = "application/json";
    if (spec.ifMatch) headers["If-Match"] = ifMatchValue(args.ifMatch ?? "");
    let response: Response;
    try {
      response = await fetch(url, {
        method: spec.method,
        headers,
        ...(body === undefined ? {} : { body }),
        mode: "cors",
        credentials: "omit",
        cache: "no-store",
        redirect: "error",
        ...(args.signal === undefined ? {} : { signal: args.signal }),
      });
    } catch (reason) {
      const kind = isAbort(reason, args.signal) ? "aborted" : "network";
      throw new ApiFailure(clientError(kind, { operationId, traceId: trace.traceId }));
    }
    if (response.status === 401) {
      void response.body?.cancel().catch(() => undefined);
      if (attempt === 0) continue;
      deps.onSessionExpired();
      throw new ApiFailure(
        clientError("session_expired", { operationId, status: 401, traceId: trace.traceId }),
      );
    }
    if (response.ok) return { response, traceId: trace.traceId };
    throw new ApiFailure(await classifyFailure(response, operationId, trace.traceId));
  }
}

/** Reads a JSON `2xx` (section 5.5 step 5): `204` has no body; any other answer must be
 * `application/json` and pass the operation's schema, else it is `unexpected`. */
async function readJson(
  operationId: OperationId,
  schema: z.ZodType | null,
  sent: Sent,
): Promise<unknown> {
  const { response, traceId } = sent;
  if (response.status === 204) return undefined;
  const unexpected = () =>
    new ApiFailure(clientError("unexpected", { operationId, status: response.status, traceId }));
  if (mediaType(response) !== "application/json") throw unexpected();
  let text: string;
  try {
    text = await response.text();
  } catch {
    throw new ApiFailure(clientError("network", { operationId, status: response.status, traceId }));
  }
  let body: unknown;
  try {
    body = JSON.parse(text);
  } catch {
    throw unexpected();
  }
  if (schema === null) return body;
  const parsed = schema.safeParse(body);
  if (!parsed.success) throw unexpected();
  return parsed.data;
}

export interface Answered<T> {
  readonly data: T;
  readonly status: number;
  readonly traceId: string;
}

/** Calls a JSON operation and resolves with its parsed body, status and trace ID. */
export async function requestWithStatus<K extends JsonOperationId>(
  operationId: K,
  args: RequestArgs<K>,
): Promise<Answered<ResponseOf<K>>> {
  const sent = await send(operationId, args);
  const data = await readJson(operationId, operations[operationId].schema, sent);
  return { data: data as ResponseOf<K>, status: sent.response.status, traceId: sent.traceId };
}

/** Calls a JSON operation and resolves with its parsed body. */
export async function request<K extends JsonOperationId>(
  operationId: K,
  args: RequestArgs<K>,
): Promise<ResponseOf<K>> {
  return (await requestWithStatus(operationId, args)).data;
}

export interface Downloaded {
  /** The bytes, typed `application/pdf`. */
  readonly blob: Blob;
  readonly traceId: string;
}

/** Calls one of the three file operations (section 8.14): status `200`, `application/pdf`, at most
 * the operation's byte limit, and exactly `Content-Length` bytes when it is present. A failed
 * check is `unexpected`; a body read that rejects or ends short is `network` with the client
 * outcome `download_interrupted`. */
export async function download<K extends FileOperationId>(
  operationId: K,
  args: RequestArgs<K>,
): Promise<Downloaded> {
  const spec = operations[operationId];
  const { response, traceId } = await send(operationId, args);
  const fail = (kind: "unexpected" | "network", code: string | null = null) => {
    void response.body?.cancel().catch(() => undefined);
    return new ApiFailure(
      clientError(kind, { operationId, status: response.status, code, traceId }),
    );
  };
  const lengthHeader = response.headers.get("Content-Length");
  const declared = lengthHeader === null ? null : Number.parseInt(lengthHeader, 10);
  if (
    response.status !== 200 ||
    mediaType(response) !== "application/pdf" ||
    response.body === null ||
    (declared !== null && (Number.isNaN(declared) || declared > spec.maxBytes))
  ) {
    throw fail("unexpected");
  }
  const reader = response.body.getReader();
  const chunks: Uint8Array[] = [];
  let total = 0;
  for (;;) {
    let result: ReadableStreamReadResult<Uint8Array>;
    try {
      result = await reader.read();
    } catch {
      throw fail("network", "download_interrupted");
    }
    if (result.done) break;
    total += result.value.byteLength;
    if (total > spec.maxBytes) throw fail("unexpected");
    chunks.push(result.value);
  }
  if (declared !== null && total < declared) throw fail("network", "download_interrupted");
  return { blob: new Blob(chunks as BlobPart[], { type: "application/pdf" }), traceId };
}

export interface ChatStreamStart {
  /** The `200` whose body the NDJSON reader takes; its `Content-Type` is checked there. */
  readonly response: Response;
  readonly traceId: string;
}

/** Sends `createChatTurn` (section 8.6 step 4) and resolves with the `2xx` response unread. A
 * `401` is repeated once as the identical request, with the same `clientTurnId`, because
 * authentication fails before the API creates a turn. */
export async function startChatTurn(
  body: ChatTurnRequest,
  signal: AbortSignal,
): Promise<ChatStreamStart> {
  return send("createChatTurn", { body, signal });
}

export interface UploadArgs {
  readonly path: { readonly indexId: string; readonly versionId: string };
  /** The `File` itself, so the browser streams it from disk. */
  readonly body: Blob;
  readonly signal?: AbortSignal;
  readonly onProgress?: (loaded: number, total: number) => void;
}

/** One attempt of `adminUploadContent` (section 10.5 step 2), by `XMLHttpRequest`, because
 * `fetch` reports no upload progress. `120,000` ms without a progress event aborts it as the
 * client outcome `upload_stalled` (kind `network`). The upload flow owns the retries and waits. */
export async function uploadContent(
  args: UploadArgs,
): Promise<Answered<ResponseOf<"adminUploadContent">>> {
  const deps = requireDependencies();
  const operationId = "adminUploadContent";
  const spec = operations[operationId];
  const url = buildUrl(deps.apiBaseUrl, spec.path, args.path, undefined);
  for (let attempt = 0; ; attempt += 1) {
    if (halted) throw new ApiFailure(clientError("aborted", { operationId }));
    const trace = newTraceparent();
    let token: string;
    try {
      token = attempt === 0 ? await deps.getToken() : await deps.renewToken();
    } catch (reason) {
      throw tokenFailure(reason, operationId);
    }
    const response = await sendUpload(url, token, trace.header, args, trace.traceId);
    if (response.status === 401) {
      if (attempt === 0) continue;
      deps.onSessionExpired();
      throw new ApiFailure(
        clientError("session_expired", { operationId, status: 401, traceId: trace.traceId }),
      );
    }
    const sent = { response, traceId: trace.traceId };
    if (!response.ok)
      throw new ApiFailure(await classifyFailure(response, operationId, trace.traceId));
    const data = await readJson(operationId, spec.schema, sent);
    return {
      data: data as ResponseOf<"adminUploadContent">,
      status: response.status,
      traceId: trace.traceId,
    };
  }
}

/** Sends the body once and resolves with the answer as a `Response`, so that one classification
 * serves both transports. */
function sendUpload(
  url: string,
  token: string,
  traceparent: string,
  args: UploadArgs,
  traceId: string,
): Promise<Response> {
  const operationId = "adminUploadContent";
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    let stalled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const armStallTimer = () => {
      clearTimeout(timer);
      timer = setTimeout(() => {
        stalled = true;
        xhr.abort();
      }, UPLOAD_STALL_MS);
    };
    const onAbortSignal = () => {
      xhr.abort();
    };
    const finish = () => {
      clearTimeout(timer);
      args.signal?.removeEventListener("abort", onAbortSignal);
    };
    xhr.open("PUT", url);
    xhr.setRequestHeader("Authorization", `Bearer ${token}`);
    xhr.setRequestHeader("Accept", "application/json");
    xhr.setRequestHeader("Content-Type", "application/octet-stream");
    xhr.setRequestHeader("traceparent", traceparent);
    xhr.upload.onprogress = (event) => {
      armStallTimer();
      args.onProgress?.(event.loaded, event.total);
    };
    xhr.onload = () => {
      finish();
      const contentType = xhr.getResponseHeader("Content-Type");
      resolve(
        new Response(xhr.status === 204 ? null : xhr.responseText, {
          status: xhr.status,
          headers: contentType === null ? {} : { "Content-Type": contentType },
        }),
      );
    };
    xhr.onerror = () => {
      finish();
      reject(new ApiFailure(clientError("network", { operationId, traceId })));
    };
    xhr.onabort = () => {
      finish();
      reject(
        new ApiFailure(
          stalled
            ? clientError("network", { operationId, code: "upload_stalled", traceId })
            : clientError("aborted", { operationId, traceId }),
        ),
      );
    };
    if (args.signal?.aborted === true) {
      finish();
      reject(new ApiFailure(clientError("aborted", { operationId, traceId })));
      return;
    }
    args.signal?.addEventListener("abort", onAbortSignal);
    armStallTimer();
    xhr.send(args.body);
  });
}

/** Fetches `/config.json` (section 2.1): same origin, never cached, no redirect, and without
 * `traceparent` or `Authorization`. Rejects on a network error, a status other than `200` or a
 * body that isn't JSON. */
export async function fetchConfigDocument(): Promise<unknown> {
  const response = await fetch("/config.json", {
    cache: "no-store",
    credentials: "same-origin",
    redirect: "error",
  });
  if (response.status !== 200) throw new Error(`config.json answered ${String(response.status)}`);
  return (await response.json()) as unknown;
}

/** For tests: forgets the dependencies and the halt. */
export function resetHttpForTests(): void {
  dependencies = null;
  halted = false;
}
