// How the stub API answers, the way the API's platform does: CORS for the site's origin on every
// answer (App Service platform CORS, 11-api.md section 1.10), `no-store`, and problem details with
// the catalogue's title and detail.

import type { IncomingMessage, ServerResponse } from "node:http";

import type { FieldError, Problem } from "../../../../../apps/web/src/api/generated/types.gen.ts";
import { hex32 } from "./builders.ts";
import { catalogueEntry, type ProblemCode } from "./problems.ts";
import { APP_ORIGIN } from "./protocol.ts";

/** The headers every answer of an allowed origin carries. */
export function corsHeaders(request: IncomingMessage): Record<string, string> {
  return request.headers.origin === APP_ORIGIN
    ? { "Access-Control-Allow-Origin": APP_ORIGIN, Vary: "Origin" }
    : { Vary: "Origin" };
}

export function baseHeaders(request: IncomingMessage): Record<string, string> {
  return {
    ...corsHeaders(request),
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
  };
}

export function sendJson(
  request: IncomingMessage,
  response: ServerResponse,
  status: number,
  body: unknown,
): void {
  const text = JSON.stringify(body);
  response.writeHead(status, {
    ...baseHeaders(request),
    "Content-Type": "application/json",
    "Content-Length": String(Buffer.byteLength(text)),
  });
  response.end(text);
}

export interface ProblemExtras {
  readonly retryAfterSeconds?: number;
  readonly errors?: readonly FieldError[];
  readonly sessionId?: string;
  readonly turnId?: string;
  readonly correlationId?: string;
}

export function problemBody(code: ProblemCode, extras: ProblemExtras = {}): Problem {
  const entry = catalogueEntry(code);
  return {
    type: `urn:docchat:problem:${code}`,
    title: entry.title,
    status: entry.status,
    detail: entry.detail,
    code,
    correlationId: extras.correlationId ?? hex32(),
    ...(extras.retryAfterSeconds === undefined
      ? {}
      : { retryAfterSeconds: extras.retryAfterSeconds }),
    ...(extras.errors === undefined ? {} : { errors: [...extras.errors] }),
    ...(extras.sessionId === undefined ? {} : { sessionId: extras.sessionId }),
    ...(extras.turnId === undefined ? {} : { turnId: extras.turnId }),
  };
}

export function sendProblem(
  request: IncomingMessage,
  response: ServerResponse,
  code: ProblemCode,
  extras: ProblemExtras = {},
): void {
  const body = problemBody(code, extras);
  const text = JSON.stringify(body);
  response.writeHead(body.status, {
    ...baseHeaders(request),
    "Content-Type": "application/problem+json",
    "Content-Length": String(Buffer.byteLength(text)),
    ...(extras.retryAfterSeconds === undefined
      ? {}
      : { "Retry-After": String(extras.retryAfterSeconds) }),
  });
  response.end(text);
}

export function sendPdf(
  request: IncomingMessage,
  response: ServerResponse,
  bytes: Buffer,
  fileName: string,
): void {
  response.writeHead(200, {
    ...baseHeaders(request),
    "Content-Type": "application/pdf",
    "Content-Length": String(bytes.byteLength),
    "Content-Disposition": `attachment; filename*=UTF-8''${encodeURIComponent(fileName)}`,
  });
  response.end(bytes);
}

/** A `200 application/pdf` whose body stops halfway through its declared length. */
export function sendTruncatedPdf(
  request: IncomingMessage,
  response: ServerResponse,
  bytes: Buffer,
): void {
  response.writeHead(200, {
    ...baseHeaders(request),
    "Content-Type": "application/pdf",
    "Content-Length": String(bytes.byteLength),
  });
  response.write(bytes.subarray(0, Math.floor(bytes.byteLength / 2)), () => {
    response.socket?.destroy();
  });
}
