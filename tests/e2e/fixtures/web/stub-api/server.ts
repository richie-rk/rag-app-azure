// The browser suites' stub API (10-web-client.md section 13.2): Node's `http` module and no
// dependency, the API on 127.0.0.1:4010 and the specs' control port on 127.0.0.1:4011. It
// answers CORS preflights the way App Service platform CORS does, records every request, refuses
// (and reports to the owning test) any request without a valid `traceparent` or bearer token, and
// serves each operation of section 5.2 from the requesting test's world. Run with
// `node fixtures/web/stub-api/server.ts`; Node strips the type annotations itself.

import { createServer, type IncomingMessage, type ServerResponse } from "node:http";

import { hex32 } from "./builders.ts";
import { FILE_OPERATIONS, IF_MATCH_OPERATIONS, findRoute } from "./operations.ts";
import { isProblemCode } from "./problems.ts";
import {
  ACCESS_TOKEN_PATTERN,
  API_HOST,
  API_PORT,
  APP_ORIGIN,
  CONTROL_PORT,
  type OperationId,
  type RecordedRequest,
  type Script,
  type StreamScript,
  type WorldInit,
} from "./protocol.ts";
import { baseHeaders, corsHeaders, sendProblem } from "./reply.ts";
import { World } from "./world.ts";

const TRACEPARENT = /^00-([0-9a-f]{32})-([0-9a-f]{16})-01$/;
/** Request headers the client never sends (10-web-client.md section 5.3). */
const FORBIDDEN_HEADERS = ["tracestate", "baggage", "request-id", "x-requested-with", "cookie"];

const worlds = new Map<string, World>();
/** Worlds whose test has ended; a request naming one is a late tab, not an app defect. */
const retired = new Set<string>();
/** Refused requests that named no world: an app defect, reported to whichever test asks. */
const orphans: { readonly at: number; readonly message: string }[] = [];

function readBody(request: IncomingMessage): Promise<Buffer> {
  return new Promise((resolve, reject) => {
    const chunks: Buffer[] = [];
    request.on("data", (chunk: Buffer) => chunks.push(chunk));
    request.on("end", () => {
      resolve(Buffer.concat(chunks));
    });
    request.on("error", reject);
  });
}

function headerText(request: IncomingMessage, name: string): string | null {
  const value = request.headers[name];
  if (value === undefined) return null;
  return Array.isArray(value) ? value.join(", ") : value;
}

function expectedAccept(operation: OperationId): string {
  if (operation === "createChatTurn") return "application/x-ndjson";
  if (FILE_OPERATIONS.has(operation)) return "*/*";
  return "application/json";
}

/** The header rules every API request meets; each failure is one message. */
function headerFailures(request: IncomingMessage, operation: OperationId | null): string[] {
  const failures: string[] = [];
  const traceparent = headerText(request, "traceparent");
  const trace = traceparent === null ? null : TRACEPARENT.exec(traceparent);
  if (trace === null) failures.push(`missing or invalid traceparent: ${String(traceparent)}`);
  else if (/^0+$/.test(trace[1] ?? "") || /^0+$/.test(trace[2] ?? "")) {
    failures.push(`all-zero traceparent ID: ${String(traceparent)}`);
  }
  for (const name of FORBIDDEN_HEADERS) {
    if (request.headers[name] !== undefined) failures.push(`forbidden request header ${name}`);
  }
  if (operation !== null) {
    const accept = headerText(request, "accept");
    if (accept !== expectedAccept(operation)) {
      failures.push(`${operation} sent Accept ${String(accept)}`);
    }
    const ifMatch = request.headers["if-match"] !== undefined;
    if (ifMatch !== IF_MATCH_OPERATIONS.has(operation)) {
      failures.push(`${operation} ${ifMatch ? "sent" : "lacks"} If-Match`);
    }
  }
  return failures;
}

function worldIdOf(request: IncomingMessage): string | null {
  const authorization = headerText(request, "authorization");
  if (!authorization?.startsWith("Bearer ")) return null;
  return ACCESS_TOKEN_PATTERN.exec(authorization.slice("Bearer ".length))?.[1] ?? null;
}

function refuse(request: IncomingMessage, response: ServerResponse, status: number, text: string) {
  response.writeHead(status, { ...baseHeaders(request), "Content-Type": "text/plain" });
  response.end(`stub API: ${text}`);
}

async function applyScript(
  script: Script,
  request: IncomingMessage,
  response: ServerResponse,
): Promise<boolean> {
  const answer = script.answer;
  switch (answer.kind) {
    case "problem":
      sendProblem(request, response, answer.code, {
        ...(answer.retryAfterSeconds === undefined
          ? {}
          : { retryAfterSeconds: answer.retryAfterSeconds }),
        ...(answer.errors === undefined ? {} : { errors: answer.errors }),
        ...(answer.sessionId === undefined ? {} : { sessionId: answer.sessionId }),
        ...(answer.turnId === undefined ? {} : { turnId: answer.turnId }),
        ...(answer.correlationId === undefined ? {} : { correlationId: answer.correlationId }),
      });
      return true;
    case "bare":
      response.writeHead(answer.status, { ...corsHeaders(request), "Content-Length": "0" });
      response.end();
      return true;
    case "delay":
      await new Promise((resolve) => setTimeout(resolve, answer.ms));
      return false;
    case "truncated":
      return false;
  }
}

async function serveApi(request: IncomingMessage, response: ServerResponse): Promise<void> {
  const method = request.method ?? "GET";
  if (method === "OPTIONS") {
    // App Service platform CORS for the site's origin: any method and any header.
    const allowed = request.headers.origin === APP_ORIGIN;
    response.writeHead(allowed ? 204 : 403, {
      ...corsHeaders(request),
      ...(allowed
        ? {
            "Access-Control-Allow-Methods":
              headerText(request, "access-control-request-method") ?? "GET",
            "Access-Control-Allow-Headers":
              headerText(request, "access-control-request-headers") ?? "",
          }
        : {}),
      "Content-Length": "0",
    });
    response.end();
    return;
  }
  const rawBody = await readBody(request);
  const url = new URL(request.url ?? "/", `http://${API_HOST}`);
  const found = findRoute(method, url.pathname);
  const operation = found?.route.operation ?? null;
  const worldId = worldIdOf(request);
  const world = worldId === null ? null : (worlds.get(worldId) ?? null);
  const contentType = headerText(request, "content-type");
  let body: unknown = null;
  if (contentType?.startsWith("application/json") === true && rawBody.byteLength > 0) {
    try {
      body = JSON.parse(rawBody.toString("utf8"));
    } catch {
      body = null;
    }
  }
  const recorded: RecordedRequest = {
    method,
    path: url.pathname,
    query: Object.fromEntries(url.searchParams),
    operation,
    headers: Object.fromEntries(
      ["authorization", "traceparent", "accept", "content-type", "if-match", "origin"]
        .map((name) => [name, headerText(request, name)] as const)
        .filter((entry): entry is readonly [string, string] => entry[1] !== null),
    ),
    body,
    bodyBytes: rawBody.byteLength,
    contentType,
    at: Date.now(),
  };
  if (world === null && worldId !== null && retired.has(worldId)) {
    refuse(request, response, 401, "this test's world has ended");
    return;
  }
  if (world === null) {
    orphans.push({
      at: Date.now(),
      message: `${method} ${url.pathname} without a valid bearer token`,
    });
    refuse(request, response, 401, "a valid bearer token is required");
    return;
  }
  world.requests.push(recorded);
  const failures = headerFailures(request, operation);
  if (failures.length > 0) {
    world.violations.push(...failures.map((failure) => `${method} ${url.pathname}: ${failure}`));
    refuse(request, response, 400, failures.join("; "));
    return;
  }
  if (found === null) {
    world.violations.push(`${method} ${url.pathname}: no such operation`);
    sendProblem(request, response, "route_not_found");
    return;
  }
  const script = world.takeScript(found.route.operation, url.searchParams);
  if (script !== null && (await applyScript(script, request, response))) return;
  if (found.route.operation.startsWith("admin") && !world.user.isAdmin) {
    sendProblem(request, response, "admin_required");
    return;
  }
  await found.route.handler({
    world,
    request,
    response,
    params: found.params,
    query: url.searchParams,
    body,
    rawBody,
    truncate: script?.answer.kind === "truncated",
  });
}

// --- control ---------------------------------------------------------------------------------

function sendControl(response: ServerResponse, status: number, body: unknown): void {
  const text = body === undefined ? "" : JSON.stringify(body);
  response.writeHead(status, { "Content-Type": "application/json" });
  response.end(text);
}

const BLANK_PAGE =
  '<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Stub helper</title></head>' +
  "<body><main><h1>Stub helper</h1></main></body></html>";

async function serveControl(request: IncomingMessage, response: ServerResponse): Promise<void> {
  const method = request.method ?? "GET";
  const url = new URL(request.url ?? "/", `http://${API_HOST}`);
  const parts = url.pathname.split("/").filter(Boolean);
  const raw = await readBody(request);
  const input: unknown = raw.byteLength === 0 ? {} : JSON.parse(raw.toString("utf8"));

  if (method === "GET" && url.pathname === "/health") {
    sendControl(response, 200, { worlds: worlds.size });
    return;
  }
  if (method === "GET" && url.pathname === "/blank") {
    response.writeHead(200, { "Content-Type": "text/html; charset=utf-8" });
    response.end(BLANK_PAGE);
    return;
  }
  if (method === "GET" && url.pathname === "/orphans") {
    const since = Number.parseInt(url.searchParams.get("since") ?? "0", 10);
    sendControl(
      response,
      200,
      orphans.filter((item) => item.at >= since),
    );
    return;
  }
  if (method === "POST" && url.pathname === "/worlds") {
    const id = hex32();
    worlds.set(id, new World(id, input as WorldInit));
    sendControl(response, 201, { id });
    return;
  }
  const world = parts[0] === "worlds" ? worlds.get(parts[1] ?? "") : undefined;
  if (world === undefined) {
    sendControl(response, 404, { error: `no world at ${url.pathname}` });
    return;
  }
  const action = `${method} ${parts.slice(2).join("/")}`;
  switch (action) {
    case "GET ":
      sendControl(response, 200, world.snapshot());
      return;
    case "PATCH ":
      world.update(input as WorldInit);
      sendControl(response, 200, world.snapshot());
      return;
    case "DELETE ":
      worlds.delete(world.id);
      retired.add(world.id);
      world.release();
      sendControl(response, 204, undefined);
      return;
    case "POST scripts": {
      const script = input as Script;
      const code: string = script.answer.kind === "problem" ? script.answer.code : "";
      if (script.answer.kind === "problem" && !isProblemCode(code)) {
        sendControl(response, 400, { error: `unknown problem code ${code}` });
        return;
      }
      world.addScript(script);
      sendControl(response, 201, {});
      return;
    }
    case "POST streams":
      world.addStream(input as StreamScript);
      sendControl(response, 201, {});
      return;
    case "POST release":
      world.release();
      sendControl(response, 200, {});
      return;
    case "GET requests":
      sendControl(response, 200, world.requests);
      return;
    default:
      sendControl(response, 404, { error: `unknown control action ${action}` });
  }
}

function guard(
  serve: (request: IncomingMessage, response: ServerResponse) => Promise<void>,
): (request: IncomingMessage, response: ServerResponse) => void {
  return (request, response) => {
    serve(request, response).catch((error: unknown) => {
      process.stderr.write(`stub API: ${String(error)}\n`);
      if (!response.headersSent) {
        response.writeHead(500, { "Content-Type": "text/plain" });
        response.end("stub API failure");
      } else {
        response.destroy();
      }
    });
  };
}

createServer(guard(serveApi)).listen(API_PORT, API_HOST);
createServer(guard(serveControl)).listen(CONTROL_PORT, API_HOST, () => {
  process.stdout.write(
    `stub API on ${API_HOST}:${String(API_PORT)}, control on ${String(CONTROL_PORT)}\n`,
  );
});
