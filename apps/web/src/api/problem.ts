// The client's one failure shape (10-web-client.md section 5.5) and the helpers screens use to
// read it: the reference ID, the retry time and the field errors of a `422`.

import type { FieldError } from "./generated/types.gen";

/**
 * - `network`: no response (the request failed, a CORS refusal, a cut-short download);
 * - `problem`: a non-`2xx` with a problem details body;
 * - `http`: a non-`2xx` without one (platform authentication, App Service while the site stops);
 * - `unexpected`: a `2xx` whose body or `Content-Type` fails the operation's contract;
 * - `aborted`: the user, a navigation or a sign-out stopped it; nothing is shown;
 * - `session_expired`: the sign-in has to be renewed before any call can go out (section 4.5).
 */
export type ApiErrorKind =
  "network" | "problem" | "http" | "unexpected" | "aborted" | "session_expired";

export interface ApiError {
  readonly kind: ApiErrorKind;
  /** The operation that failed; null for the `config.json` fetch. */
  readonly operationId: string | null;
  readonly status: number | null;
  /** The problem's `code`, or a client outcome code such as `download_interrupted`. */
  readonly code: string | null;
  readonly title: string | null;
  readonly detail: string | null;
  readonly errors: readonly FieldError[];
  readonly retryAfterSeconds: number | null;
  /** The trace ID of the request's `traceparent`; null when no request went out. */
  readonly traceId: string | null;
  /** The problem's `correlationId`, else the trace ID, else MSAL's correlation ID; null when
   * no server trace exists. */
  readonly referenceId: string | null;
  /** When the client classified the answer, in epoch milliseconds; `retryAfterSeconds` counts
   * from here. */
  readonly receivedAt: number;
}

/** The rejection value of every API call. */
export class ApiFailure extends Error {
  readonly error: ApiError;

  constructor(error: ApiError) {
    super(error.code === null ? error.kind : `${error.kind}: ${error.code}`);
    this.name = "ApiFailure";
    this.error = error;
  }
}

export interface ClientErrorFields {
  readonly operationId?: string | null;
  readonly status?: number | null;
  readonly code?: string | null;
  readonly traceId?: string | null;
  readonly referenceId?: string | null;
}

/** A failure the client detected itself; its reference ID defaults to the trace ID. */
export function clientError(kind: ApiErrorKind, fields: ClientErrorFields = {}): ApiError {
  const traceId = fields.traceId ?? null;
  return {
    kind,
    operationId: fields.operationId ?? null,
    status: fields.status ?? null,
    code: fields.code ?? null,
    title: null,
    detail: null,
    errors: [],
    retryAfterSeconds: null,
    traceId,
    referenceId: fields.referenceId === undefined ? traceId : fields.referenceId,
    receivedAt: Date.now(),
  };
}

/** The `ApiError` inside a rejection, or null for anything else. */
export function apiErrorOf(reason: unknown): ApiError | null {
  return reason instanceof ApiFailure ? reason.error : null;
}

/** Whether `error` is a problem with this status and, when given, this code. */
export function isProblem(
  error: ApiError | null | undefined,
  status: number,
  code?: string,
): boolean {
  return (
    error?.kind === "problem" &&
    error.status === status &&
    (code === undefined || error.code === code)
  );
}

/** The epoch milliseconds from which a refused request may be sent again, or null. */
export function retryAtOf(error: ApiError): number | null {
  return error.retryAfterSeconds === null
    ? null
    : error.receivedAt + error.retryAfterSeconds * 1000;
}

export interface FieldErrorSplit {
  /** Reason per form field, keyed by the field's dotted name (`exampleQuestions.2`). */
  readonly byField: ReadonlyMap<string, string>;
  /** Entries the error bar lists: `query.`, `path.` and `header.` paths and unmatched `body.`. */
  readonly unmatched: readonly FieldError[];
}

/** Routes each `422` entry to the form field its path names (section 7.1): an entry at `body.<f>`
 * goes to the field whose dotted name is `<f>`; everything else goes to the error bar. */
export function splitFieldErrors(error: ApiError, fields: Iterable<string>): FieldErrorSplit {
  const known = new Set(fields);
  const byField = new Map<string, string>();
  const unmatched: FieldError[] = [];
  for (const entry of error.errors) {
    const field = entry.path.startsWith("body.") ? entry.path.slice("body.".length) : null;
    if (field !== null && known.has(field)) {
      if (!byField.has(field)) byField.set(field, entry.reason);
    } else {
      unmatched.push(entry);
    }
  }
  return { byField, unmatched };
}
