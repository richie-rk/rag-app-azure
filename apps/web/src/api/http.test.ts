import { HttpResponse, http } from "msw";
import { afterEach, describe, expect, test, vi } from "vitest";

import { PDF_PAGE_MAX_BYTES } from "../platform/constants";
import {
  API_BASE,
  CORRELATION_ID,
  CURRENT_USER,
  IDS,
  apiUrl,
  problemResponse,
} from "../test/handlers";
import { configureTestHttp } from "../test/render";
import { server } from "../test/server";
import {
  configureHttp,
  download,
  fetchConfigDocument,
  haltApi,
  ifMatchValue,
  request,
  requestWithStatus,
  resetHttpForTests,
  startChatTurn,
  uploadContent,
} from "./http";
import { ApiFailure, clientError, type ApiError } from "./problem";

const TRACEPARENT = /^00-([0-9a-f]{32})-[0-9a-f]{16}-01$/;

async function failureOf(promise: Promise<unknown>): Promise<ApiError> {
  try {
    await promise;
  } catch (reason) {
    if (reason instanceof ApiFailure) return reason.error;
    throw reason;
  }
  throw new Error("the call succeeded");
}

/** Records each request the handler sees. */
function recordRequests(
  method: "get" | "put" | "post" | "patch" | "delete",
  path: string,
  answer: () => Response,
) {
  const seen: Request[] = [];
  server.use(
    http[method](apiUrl(path), ({ request: incoming }) => {
      seen.push(incoming.clone());
      return answer();
    }),
  );
  return seen;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("building a request", () => {
  test("T-357 every call carries a new traceparent and exactly the allowed headers", async () => {
    configureTestHttp();
    const spy = vi.spyOn(globalThis, "fetch");
    await request("getCurrentUser", {});
    await request("getCurrentUser", {});
    const headers = spy.mock.calls.map((call) => call[1]?.headers as Record<string, string>);
    const [first, second] = headers;
    if (first === undefined || second === undefined) throw new Error("no requests");
    const traceIds = [first, second].map(
      (each) => TRACEPARENT.exec(each["traceparent"] ?? "")?.[1],
    );
    expect(traceIds[0]).toBeDefined();
    expect(traceIds[0]).not.toBe(traceIds[1]);
    expect(Object.keys(first).sort()).toEqual(["Accept", "Authorization", "traceparent"]);
    expect(first["Authorization"]).toBe("Bearer test-token");
    expect(first["Accept"]).toBe("application/json");
    spy.mockRestore();
  });

  test("the fetch options never send cookies, follow redirects or use the cache", async () => {
    configureTestHttp();
    const spy = vi.spyOn(globalThis, "fetch");
    await request("getCurrentUser", {});
    const init = spy.mock.calls[0]?.[1];
    expect(init).toMatchObject({
      mode: "cors",
      credentials: "omit",
      cache: "no-store",
      redirect: "error",
    });
    spy.mockRestore();
  });

  test("path parameters are encoded and absent query parameters are left out", async () => {
    configureTestHttp();
    const seen: string[] = [];
    server.use(
      http.get(apiUrl("/v1/admin/users"), ({ request: incoming }) => {
        seen.push(incoming.url);
        return HttpResponse.json({ items: [], nextCursor: null });
      }),
      http.get(`${API_BASE}/v1/admin/projects/:projectId`, ({ request: incoming }) => {
        seen.push(incoming.url);
        return HttpResponse.json({});
      }),
    );
    await request("adminSearchUsers", { query: { q: "a b&c", limit: 20, cursor: null } });
    expect(seen[0]).toBe(`${API_BASE}/v1/admin/users?q=a+b%26c&limit=20`);
    await request("adminSearchUsers", {});
    expect(seen[1]).toBe(`${API_BASE}/v1/admin/users`);
    await failureOf(request("adminGetProject", { path: { projectId: "a/b?" } }));
    expect(seen[2]).toBe(`${API_BASE}/v1/admin/projects/a%2Fb%3F`);
  });

  test("a missing path parameter is a programming error", async () => {
    configureTestHttp();
    await expect(request("adminGetProject", { path: {} as { projectId: string } })).rejects.toThrow(
      "missing path parameter projectId",
    );
  });

  test("a JSON body is sent with its Content-Type", async () => {
    configureTestHttp();
    const seen = recordRequests("post", "/v1/admin/projects", () =>
      problemResponse(409, "project_name_taken"),
    );
    await failureOf(request("adminCreateProject", { body: { name: "Alpha" } }));
    expect(seen[0]?.headers.get("content-type")).toBe("application/json");
    await expect(seen[0]?.json()).resolves.toEqual({ name: "Alpha" });
  });

  test("T-363 If-Match is sent verbatim for a quoted ETag and quoted for a rowVersion", async () => {
    expect(ifMatchValue('"etag-1"')).toBe('"etag-1"');
    expect(ifMatchValue("AAAAAAAAB9E=")).toBe('"AAAAAAAAB9E="');
    expect(ifMatchValue('"')).toBe('"""');
    configureTestHttp();
    const seen = recordRequests("patch", `/v1/admin/projects/${IDS.projectA}`, () =>
      problemResponse(412, "precondition_failed"),
    );
    await failureOf(
      request("adminUpdateProject", {
        path: { projectId: IDS.projectA },
        body: { name: "B" },
        ifMatch: "AAAAAAAAB9E=",
      }),
    );
    expect(seen[0]?.headers.get("if-match")).toBe('"AAAAAAAAB9E="');
    const memory = recordRequests("patch", "/v1/memory", () =>
      problemResponse(412, "precondition_failed"),
    );
    await failureOf(request("updateMemorySettings", { body: { paused: true }, ifMatch: '"0x1"' }));
    expect(memory[0]?.headers.get("if-match")).toBe('"0x1"');
  });

  test("T-363 feedback, upload and create operations send no If-Match", async () => {
    configureTestHttp();
    const feedback = recordRequests(
      "put",
      `/v1/sessions/${IDS.session}/turns/${IDS.turn}/feedback`,
      () => problemResponse(404, "turn_not_found"),
    );
    await failureOf(
      request("putTurnFeedback", {
        path: { sessionId: IDS.session, turnId: IDS.turn },
        body: { rating: "up", category: null, comment: null },
      }),
    );
    expect(feedback[0]?.headers.has("if-match")).toBe(false);
  });

  test("calls fail before configureHttp, and as aborted after haltApi", async () => {
    resetHttpForTests();
    await expect(request("getCurrentUser", {})).rejects.toThrow("configureHttp hasn't run");
    configureTestHttp();
    haltApi();
    const spy = vi.spyOn(globalThis, "fetch");
    expect(await failureOf(request("getCurrentUser", {}))).toMatchObject({ kind: "aborted" });
    expect(spy).not.toHaveBeenCalled();
    spy.mockRestore();
  });
});

describe("classifying the answer", () => {
  test("T-359 a 2xx JSON body that passes its schema resolves with it", async () => {
    configureTestHttp();
    const answered = await requestWithStatus("getCurrentUser", {});
    expect(answered.status).toBe(200);
    expect(answered.data.displayName).toBe(CURRENT_USER.displayName);
    expect(answered.traceId).toMatch(/^[0-9a-f]{32}$/);
  });

  test("T-359 unknown members are stripped", async () => {
    configureTestHttp();
    server.use(http.get(apiUrl("/v1/me"), () => HttpResponse.json({ ...CURRENT_USER, added: 1 })));
    expect(await request("getCurrentUser", {})).not.toHaveProperty("added");
  });

  test.each([
    ["a body failing its schema", () => HttpResponse.json({ ...CURRENT_USER, isAdmin: "yes" })],
    [
      "an enumeration value the schema doesn't list",
      () => HttpResponse.json({ ...CURRENT_USER, roles: ["Owner"] }),
    ],
    [
      "another Content-Type",
      () =>
        new HttpResponse(JSON.stringify(CURRENT_USER), {
          headers: { "Content-Type": "text/plain" },
        }),
    ],
    ["no Content-Type", () => new HttpResponse(JSON.stringify(CURRENT_USER))],
    [
      "invalid JSON",
      () =>
        new HttpResponse("{", { headers: { "Content-Type": "application/json; charset=utf-8" } }),
    ],
  ])("T-359 a 2xx with %s is unexpected", async (_, answer) => {
    configureTestHttp();
    server.use(http.get(apiUrl("/v1/me"), answer));
    const error = await failureOf(request("getCurrentUser", {}));
    expect(error).toMatchObject({ kind: "unexpected", status: 200, operationId: "getCurrentUser" });
    expect(error.referenceId).toBe(error.traceId);
  });

  test("a 204 resolves with no body", async () => {
    configureTestHttp();
    server.use(http.get(apiUrl("/v1/me"), () => new HttpResponse(null, { status: 204 })));
    expect(await request("getCurrentUser", {})).toBeUndefined();
  });

  test("T-359 a problem body gives a problem with the correlationId as reference ID", async () => {
    configureTestHttp();
    server.use(
      http.get(apiUrl("/v1/me"), () =>
        problemResponse(429, "too_many_requests", {
          title: "Too many requests",
          detail: "Wait a moment.",
          retryAfterSeconds: 1,
          errors: [{ path: "query.limit", reason: "too_large" }],
        }),
      ),
    );
    const error = await failureOf(request("getCurrentUser", {}));
    expect(error).toMatchObject({
      kind: "problem",
      status: 429,
      code: "too_many_requests",
      title: "Too many requests",
      detail: "Wait a moment.",
      retryAfterSeconds: 1,
      errors: [{ path: "query.limit", reason: "too_large" }],
      referenceId: CORRELATION_ID,
    });
    expect(error.traceId).not.toBe(CORRELATION_ID);
  });

  test.each([
    [
      "a body that isn't application/problem+json",
      () => HttpResponse.json({ code: "x" }, { status: 500 }),
    ],
    [
      "a problem body that fails zProblem",
      () =>
        HttpResponse.json(
          { code: "x" },
          { status: 503, headers: { "Content-Type": "application/problem+json" } },
        ),
    ],
    [
      "a problem body that isn't JSON",
      () =>
        new HttpResponse("<html>", {
          status: 502,
          headers: { "Content-Type": "application/problem+json" },
        }),
    ],
    ["no body at all", () => new HttpResponse(null, { status: 403 })],
  ])("T-359 a non-2xx with %s is http with the trace ID as reference ID", async (_, answer) => {
    const deps = configureTestHttp();
    server.use(http.get(apiUrl("/v1/me"), answer));
    const error = await failureOf(request("getCurrentUser", {}));
    expect(error.kind).toBe("http");
    expect(error.referenceId).toBe(error.traceId);
    expect(error.referenceId).toMatch(/^[0-9a-f]{32}$/);
    expect(deps.onRoleNotAssigned).not.toHaveBeenCalled();
  });

  test("T-351 403 role_not_assigned tells the app root, and admin_required the shell", async () => {
    const deps = configureTestHttp();
    server.use(http.get(apiUrl("/v1/me"), () => problemResponse(403, "role_not_assigned")));
    await failureOf(request("getCurrentUser", {}));
    expect(deps.onRoleNotAssigned).toHaveBeenCalledTimes(1);
    server.use(
      http.get(apiUrl("/v1/admin/projects"), () => problemResponse(403, "admin_required")),
    );
    await failureOf(request("adminListProjects", {}));
    expect(deps.onAdminRequired).toHaveBeenCalledTimes(1);
    expect(deps.onRoleNotAssigned).toHaveBeenCalledTimes(1);
  });

  test("T-359 a request that fails without a response is network, with its trace ID", async () => {
    configureTestHttp();
    server.use(http.get(apiUrl("/v1/me"), () => HttpResponse.error()));
    const error = await failureOf(request("getCurrentUser", {}));
    expect(error).toMatchObject({ kind: "network", status: null });
    expect(error.referenceId).toMatch(/^[0-9a-f]{32}$/);
  });

  test("a body that can't be read is network", async () => {
    configureTestHttp();
    server.use(
      http.get(apiUrl("/v1/me"), () => {
        const body = new ReadableStream({
          start(controller) {
            controller.error(new TypeError("cut"));
          },
        });
        return new HttpResponse(body, { headers: { "Content-Type": "application/json" } });
      }),
    );
    expect((await failureOf(request("getCurrentUser", {}))).kind).toBe("network");
  });

  test("an aborted call is aborted", async () => {
    configureTestHttp();
    const controller = new AbortController();
    controller.abort();
    expect((await failureOf(request("getCurrentUser", { signal: controller.signal }))).kind).toBe(
      "aborted",
    );
    vi.stubGlobal("fetch", () => Promise.reject(new DOMException("aborted", "AbortError")));
    expect((await failureOf(request("getCurrentUser", {}))).kind).toBe("aborted");
  });

  test("a token failure rejects the call with that failure", async () => {
    configureTestHttp({
      getToken: () =>
        Promise.reject(new ApiFailure(clientError("session_expired", { referenceId: null }))),
    });
    expect(await failureOf(request("getCurrentUser", {}))).toMatchObject({
      kind: "session_expired",
      operationId: "getCurrentUser",
    });
    configureTestHttp({ getToken: () => Promise.reject(new Error("bug")) });
    expect(await failureOf(request("getCurrentUser", {}))).toMatchObject({
      kind: "network",
      referenceId: null,
    });
  });
});

describe("a 401", () => {
  test("T-351 a 401 is repeated once with a renewed token and a new traceparent", async () => {
    const deps = configureTestHttp();
    let calls = 0;
    const seen = recordRequests("get", "/v1/me", () => {
      calls += 1;
      return calls === 1
        ? new HttpResponse(null, { status: 401 })
        : HttpResponse.json(CURRENT_USER);
    });
    expect((await request("getCurrentUser", {})).email).toBe(CURRENT_USER.email);
    expect(deps.renewToken).toHaveBeenCalledTimes(1);
    expect(seen[1]?.headers.get("authorization")).toBe("Bearer renewed-token");
    expect(seen[0]?.headers.get("traceparent")).not.toBe(seen[1]?.headers.get("traceparent"));
    expect(deps.onSessionExpired).not.toHaveBeenCalled();
  });

  test("T-354 two 401s in a row set the session-expired state", async () => {
    const deps = configureTestHttp();
    server.use(http.get(apiUrl("/v1/me"), () => problemResponse(401, "invalid_token")));
    expect(await failureOf(request("getCurrentUser", {}))).toMatchObject({
      kind: "session_expired",
      status: 401,
    });
    expect(deps.onSessionExpired).toHaveBeenCalledTimes(1);
  });

  test("createChatTurn repeats the identical request after a 401", async () => {
    configureTestHttp();
    const bodies: unknown[] = [];
    server.use(
      http.post(apiUrl("/v1/chat/turns"), async ({ request: incoming }) => {
        bodies.push(await incoming.json());
        return bodies.length === 1
          ? new HttpResponse(null, { status: 401 })
          : new HttpResponse('{"type":"heartbeat"}\n', {
              headers: { "Content-Type": "application/x-ndjson" },
            });
      }),
    );
    const body = { projectId: IDS.projectA, clientTurnId: IDS.clientTurn, question: "Q" };
    const started = await startChatTurn(body, new AbortController().signal);
    expect(bodies).toEqual([body, body]);
    expect(started.response.headers.get("Content-Type")).toBe("application/x-ndjson");
    expect(await started.response.text()).toBe('{"type":"heartbeat"}\n');
    expect(started.traceId).toMatch(/^[0-9a-f]{32}$/);
  });

  test("createChatTurn asks for NDJSON", async () => {
    configureTestHttp();
    const seen = recordRequests("post", "/v1/chat/turns", () =>
      problemResponse(409, "turn_in_progress"),
    );
    await failureOf(
      startChatTurn(
        { projectId: IDS.projectA, clientTurnId: IDS.clientTurn, question: "Q" },
        new AbortController().signal,
      ),
    );
    expect(seen[0]?.headers.get("accept")).toBe("application/x-ndjson");
  });
});

describe("file downloads", () => {
  const path = `/v1/sessions/${IDS.session}/turns/${IDS.turn}/citations/1/content`;
  const args = { path: { sessionId: IDS.session, turnId: IDS.turn, citationNumber: 1 } };

  test("a PDF resolves with its bytes typed application/pdf, asking for any type", async () => {
    configureTestHttp();
    const seen = recordRequests(
      "get",
      path,
      () =>
        new HttpResponse(new Uint8Array([37, 80, 68, 70]), {
          headers: { "Content-Type": "application/pdf", "Content-Length": "4" },
        }),
    );
    const downloaded = await download("getCitationContent", args);
    expect(downloaded.blob.type).toBe("application/pdf");
    expect(downloaded.blob.size).toBe(4);
    expect(seen[0]?.headers.get("accept")).toBe("*/*");
  });

  test.each([
    ["another type", () => new HttpResponse("x", { headers: { "Content-Type": "text/html" } })],
    [
      "a status other than 200",
      () => new HttpResponse("x", { status: 203, headers: { "Content-Type": "application/pdf" } }),
    ],
    [
      "a Content-Length above the limit",
      () =>
        new HttpResponse("x", {
          headers: {
            "Content-Type": "application/pdf",
            "Content-Length": String(PDF_PAGE_MAX_BYTES + 1),
          },
        }),
    ],
  ])("a file with %s fails the content check", async (_, answer) => {
    configureTestHttp();
    server.use(http.get(apiUrl(path), answer));
    expect(await failureOf(download("getCitationContent", args))).toMatchObject({
      kind: "unexpected",
      code: null,
    });
  });

  test("a body larger than the limit fails while it is read", async () => {
    configureTestHttp();
    server.use(
      http.get(apiUrl(path), () => {
        const body = new ReadableStream<Uint8Array>({
          start(controller) {
            controller.enqueue(new Uint8Array(PDF_PAGE_MAX_BYTES));
            controller.enqueue(new Uint8Array(1));
            controller.close();
          },
        });
        return new HttpResponse(body, { headers: { "Content-Type": "application/pdf" } });
      }),
    );
    expect((await failureOf(download("getCitationContent", args))).kind).toBe("unexpected");
  });

  // These answers are handed to the client directly: a mocked network can't send a body that
  // disagrees with its own Content-Length or breaks off.
  function answerWith(response: Response) {
    vi.stubGlobal("fetch", () => Promise.resolve(response));
  }

  test.each([
    [
      "a Content-Length that isn't a number",
      { "Content-Type": "application/pdf", "Content-Length": "many" },
      new Uint8Array(1),
    ],
    ["no body", { "Content-Type": "application/pdf" }, null],
  ])("a file with %s fails the content check", async (_, headers, body) => {
    configureTestHttp();
    answerWith(new Response(body, { status: 200, headers }));
    expect(await failureOf(download("getCitationContent", args))).toMatchObject({
      kind: "unexpected",
    });
  });

  test("T-390 a body that ends before its Content-Length is download_interrupted", async () => {
    configureTestHttp();
    answerWith(
      new Response(new Uint8Array(10), {
        headers: { "Content-Type": "application/pdf", "Content-Length": "20" },
      }),
    );
    expect(await failureOf(download("getCitationContent", args))).toMatchObject({
      kind: "network",
      code: "download_interrupted",
      status: 200,
    });
  });

  test("T-390 a body read that rejects is download_interrupted, with the trace ID", async () => {
    configureTestHttp();
    let pulls = 0;
    const body = new ReadableStream<Uint8Array>({
      pull(controller) {
        pulls += 1;
        if (pulls === 1) controller.enqueue(new Uint8Array(3));
        else controller.error(new TypeError("cut"));
      },
    });
    answerWith(new Response(body, { headers: { "Content-Type": "application/pdf" } }));
    const error = await failureOf(download("getCitationContent", args));
    expect(error).toMatchObject({ kind: "network", code: "download_interrupted" });
    expect(error.referenceId).toBe(error.traceId);
  });
});

/** A scriptable XMLHttpRequest: each instance is answered by `respond`. */
interface ScriptedXhr {
  status: number;
  readonly headers: Record<string, string>;
  readonly upload: { onprogress: ((event: { loaded: number; total: number }) => void) | null };
  onload: (() => void) | null;
  onerror: (() => void) | null;
  answer(status: number, body: unknown, contentType?: string): void;
}

function installFakeXhr(respond: (xhr: ScriptedXhr) => void) {
  const instances: FakeXhr[] = [];
  class FakeXhr implements ScriptedXhr {
    status = 0;
    responseText = "";
    method = "";
    url = "";
    body: unknown = null;
    readonly headers: Record<string, string> = {};
    responseHeaders: Record<string, string> = {};
    readonly upload: { onprogress: ((event: { loaded: number; total: number }) => void) | null } = {
      onprogress: null,
    };
    onload: (() => void) | null = null;
    onerror: (() => void) | null = null;
    onabort: (() => void) | null = null;
    constructor() {
      instances.push(this);
    }
    open(method: string, url: string) {
      this.method = method;
      this.url = url;
    }
    setRequestHeader(name: string, value: string) {
      this.headers[name] = value;
    }
    getResponseHeader(name: string) {
      return this.responseHeaders[name] ?? null;
    }
    send(body: unknown) {
      this.body = body;
      respond(this);
    }
    abort() {
      this.onabort?.();
    }
    answer(status: number, body: unknown, contentType = "application/json") {
      this.status = status;
      this.responseText = typeof body === "string" ? body : JSON.stringify(body);
      this.responseHeaders = { "Content-Type": contentType };
      this.onload?.();
    }
  }
  vi.stubGlobal("XMLHttpRequest", FakeXhr);
  return instances;
}

describe("upload bodies", () => {
  const args = {
    path: { indexId: IDS.projectA, versionId: IDS.session },
    body: new Blob(["%PDF"]),
  };
  const result = {
    version: {
      committedAt: null,
      contentType: "application/pdf",
      documentPdfStatus: null,
      failureCode: null,
      failureMessage: null,
      ingestedAt: null,
      reservedAt: "2026-11-02T10:00:00Z",
      scanStatus: "awaiting",
      sha256: "a".repeat(64),
      sizeBytes: 4,
      state: "pending",
      uploadedBy: IDS.admin,
      uploadedByName: "Grace Hopper",
      uploadedViaProject: { projectId: IDS.projectA, name: "Alpha" },
      versionId: IDS.session,
      versionNumber: 1,
    },
  };

  test("an upload sends the file with its token, type and traceparent, and reports progress", async () => {
    configureTestHttp();
    const progress = vi.fn();
    const instances = installFakeXhr((xhr) => {
      xhr.upload.onprogress?.({ loaded: 2, total: 4 });
      xhr.answer(200, result);
    });
    const answered = await failureOrValue(uploadContent({ ...args, onProgress: progress }));
    const xhr = instances[0];
    expect(xhr?.method).toBe("PUT");
    expect(xhr?.url).toBe(
      `${API_BASE}/v1/admin/indexes/${IDS.projectA}/uploads/${IDS.session}/content`,
    );
    expect(xhr?.headers).toMatchObject({
      Authorization: "Bearer test-token",
      Accept: "application/json",
      "Content-Type": "application/octet-stream",
    });
    expect(xhr?.headers["traceparent"]).toMatch(TRACEPARENT);
    expect(xhr?.body).toBe(args.body);
    expect(progress).toHaveBeenCalledWith(2, 4);
    expect(answered).toMatchObject({ status: 200 });
  });

  test("a 401 repeats the upload once with a renewed token, and a second sets the session expired", async () => {
    const deps = configureTestHttp();
    let attempt = 0;
    const instances = installFakeXhr((xhr) => {
      attempt += 1;
      if (attempt === 2) xhr.answer(200, result);
      else xhr.answer(401, "", "text/plain");
    });
    await failureOrValue(uploadContent(args));
    expect(instances[1]?.headers["Authorization"]).toBe("Bearer renewed-token");
    attempt = 2;
    const error = await failureOf(uploadContent(args));
    expect(error.kind).toBe("session_expired");
    expect(deps.onSessionExpired).toHaveBeenCalledTimes(1);
  });

  test("a refused upload is classified like any call", async () => {
    configureTestHttp();
    installFakeXhr((xhr) => {
      xhr.answer(
        429,
        {
          type: "urn:docchat:problem:upload_busy",
          title: "Busy",
          status: 429,
          detail: "Wait.",
          code: "upload_busy",
          correlationId: CORRELATION_ID,
          retryAfterSeconds: 2,
        },
        "application/problem+json",
      );
    });
    expect(await failureOf(uploadContent(args))).toMatchObject({
      kind: "problem",
      code: "upload_busy",
      retryAfterSeconds: 2,
    });
  });

  test("an upload whose answer fails its schema is unexpected", async () => {
    configureTestHttp();
    installFakeXhr((xhr) => {
      xhr.answer(200, { versionId: "x" });
    });
    expect((await failureOf(uploadContent(args))).kind).toBe("unexpected");
  });

  test("an upload without a response is network", async () => {
    configureTestHttp();
    installFakeXhr((xhr) => xhr.onerror?.());
    expect((await failureOf(uploadContent(args))).kind).toBe("network");
  });

  test("120,000 ms without progress aborts the upload as upload_stalled", async () => {
    vi.useFakeTimers();
    configureTestHttp();
    installFakeXhr(() => undefined);
    const pending = failureOf(uploadContent(args));
    await vi.advanceTimersByTimeAsync(120_000);
    expect(await pending).toMatchObject({ kind: "network", code: "upload_stalled" });
    vi.useRealTimers();
  });

  test("the signal aborts an upload, before or during the attempt", async () => {
    configureTestHttp();
    installFakeXhr(() => undefined);
    const controller = new AbortController();
    const pending = failureOf(uploadContent({ ...args, signal: controller.signal }));
    await Promise.resolve();
    await Promise.resolve();
    controller.abort();
    expect((await pending).kind).toBe("aborted");
    expect((await failureOf(uploadContent({ ...args, signal: controller.signal }))).kind).toBe(
      "aborted",
    );
  });

  test("an upload token failure or a halt rejects before sending", async () => {
    configureTestHttp({ getToken: () => Promise.reject(new Error("bug")) });
    const instances = installFakeXhr(() => undefined);
    expect((await failureOf(uploadContent(args))).kind).toBe("network");
    configureTestHttp();
    haltApi();
    expect((await failureOf(uploadContent(args))).kind).toBe("aborted");
    expect(instances).toHaveLength(0);
  });

  test("a 204 upload answer has no body", async () => {
    configureTestHttp();
    installFakeXhr((xhr) => {
      xhr.status = 204;
      xhr.onload?.();
    });
    await expect(uploadContent(args)).resolves.toMatchObject({ status: 204, data: undefined });
  });
});

async function failureOrValue<T>(promise: Promise<T>): Promise<T> {
  return promise;
}

describe("config.json", () => {
  test("T-358 config.json is fetched same-origin, uncached, with neither traceparent nor a token", async () => {
    const seen: Request[] = [];
    server.use(
      http.get("*/config.json", ({ request: incoming }) => {
        seen.push(incoming);
        return HttpResponse.json({ tenantId: "x" });
      }),
    );
    const spy = vi.spyOn(globalThis, "fetch");
    await expect(fetchConfigDocument()).resolves.toEqual({ tenantId: "x" });
    expect(spy.mock.calls[0]?.[0]).toBe("/config.json");
    expect(spy.mock.calls[0]?.[1]).toEqual({
      cache: "no-store",
      credentials: "same-origin",
      redirect: "error",
    });
    expect(seen[0]?.headers.has("traceparent")).toBe(false);
    expect(seen[0]?.headers.has("authorization")).toBe(false);
    spy.mockRestore();
  });

  test("config.json answering anything but 200 rejects", async () => {
    server.use(http.get("*/config.json", () => new HttpResponse(null, { status: 404 })));
    await expect(fetchConfigDocument()).rejects.toThrow("404");
  });

  test("configureHttp clears an earlier halt", async () => {
    configureTestHttp();
    haltApi();
    configureHttp({ ...configureTestHttp() });
    await expect(request("getCurrentUser", {})).resolves.toBeDefined();
  });
});
