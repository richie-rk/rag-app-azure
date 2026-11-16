import { HttpResponse, http } from "msw";
import { describe, expect, test } from "vitest";

import { CURRENT_USER, IDS, PROJECTS, apiUrl } from "../test/handlers";
import { configureTestHttp } from "../test/render";
import { server } from "../test/server";
import { ApiFailure, clientError, type ApiError } from "./problem";
import {
  MY_PROJECTS_MAX_PAGES,
  applyProjectRowVersion,
  createQueryClient,
  meQuery,
  myProjectsQuery,
  queryKeys,
  retryDelay,
  sessionsOf,
  sessionsQuery,
  shouldRetry,
  type SessionPage,
} from "./queryClient";

function failure(fields: Partial<ApiError> & Pick<ApiError, "kind">): ApiFailure {
  return new ApiFailure({ ...clientError(fields.kind), ...fields });
}

const problem = (
  operationId: string,
  status: number,
  code: string,
  retryAfterSeconds: number | null = null,
) => failure({ kind: "problem", operationId, status, code, retryAfterSeconds });

describe("automatic retries", () => {
  test("T-362 queries retry at most twice on 503", () => {
    const error = problem("getCurrentUser", 503, "dependency_unavailable");
    expect(shouldRetry(0, error)).toBe(true);
    expect(shouldRetry(1, error)).toBe(true);
    expect(shouldRetry(2, error)).toBe(false);
  });

  test("T-362 the waits are 1,000 ms then 4,000 ms, each plus at most 250 ms of jitter", () => {
    const error = problem("getCurrentUser", 503, "dependency_unavailable");
    expect(retryDelay(0, error, () => 0)).toBe(1_000);
    expect(retryDelay(1, error, () => 0)).toBe(4_000);
    expect(retryDelay(0, error, () => 0.9999)).toBe(1_250);
    expect(retryDelay(1, error, () => 0.9999)).toBe(4_250);
    expect(retryDelay(5, error, () => 0)).toBe(4_000);
    for (let index = 0; index < 100; index += 1) {
      const wait = retryDelay(0, error);
      expect(wait).toBeGreaterThanOrEqual(1_000);
      expect(wait).toBeLessThanOrEqual(1_250);
    }
  });

  test("T-362 a 429 with retryAfterSeconds 1 is retried after 1,000 ms, one with 31 isn't", () => {
    const soon = problem("listMyProjects", 429, "too_many_requests", 1);
    expect(shouldRetry(0, soon)).toBe(true);
    expect(retryDelay(0, soon)).toBe(1_000);
    expect(shouldRetry(0, problem("listMyProjects", 429, "rate_limited", 30))).toBe(true);
    expect(shouldRetry(0, problem("listMyProjects", 429, "rate_limited", 31))).toBe(false);
    expect(shouldRetry(0, problem("listMyProjects", 429, "rate_limited"))).toBe(false);
  });

  test.each([408, 500, 502, 503, 504])(
    "T-362 queries retry on %i, with or without a problem body",
    (status) => {
      expect(
        shouldRetry(0, failure({ kind: "http", operationId: "adminListIndexes", status })),
      ).toBe(true);
      expect(shouldRetry(0, problem("adminListIndexes", status, "x"))).toBe(true);
    },
  );

  test("T-362 network failures retry; other answers and client outcomes don't", () => {
    expect(shouldRetry(0, failure({ kind: "network", operationId: "getSession" }))).toBe(true);
    expect(shouldRetry(0, problem("getSession", 404, "session_not_found"))).toBe(false);
    expect(shouldRetry(0, failure({ kind: "http", operationId: "getSession", status: 403 }))).toBe(
      false,
    );
    expect(
      shouldRetry(0, failure({ kind: "unexpected", operationId: "getSession", status: 200 })),
    ).toBe(false);
    expect(shouldRetry(0, failure({ kind: "aborted", operationId: "getSession" }))).toBe(false);
    expect(shouldRetry(0, failure({ kind: "session_expired", operationId: "getSession" }))).toBe(
      false,
    );
    expect(shouldRetry(0, new Error("bug"))).toBe(false);
    expect(shouldRetry(0, failure({ kind: "network", operationId: null }))).toBe(false);
    expect(shouldRetry(0, failure({ kind: "network", operationId: "noSuchOperation" }))).toBe(
      false,
    );
  });

  test("T-362 writes never retry", () => {
    for (const operationId of [
      "adminUpdateProject",
      "createChatTurn",
      "putTurnFeedback",
      "adminDeleteIndex",
    ]) {
      expect(shouldRetry(0, problem(operationId, 503, "dependency_unavailable"))).toBe(false);
      expect(shouldRetry(0, failure({ kind: "network", operationId }))).toBe(false);
    }
  });

  test("T-362 getCitationContent retries on 429 too_many_requests only", () => {
    expect(shouldRetry(0, problem("getCitationContent", 429, "too_many_requests", 1))).toBe(true);
    expect(shouldRetry(1, problem("getCitationContent", 429, "too_many_requests", 1))).toBe(true);
    expect(shouldRetry(2, problem("getCitationContent", 429, "too_many_requests", 1))).toBe(false);
    expect(shouldRetry(0, problem("getCitationContent", 429, "rate_limited", 1))).toBe(false);
    expect(shouldRetry(0, problem("getCitationContent", 429, "too_many_downloads", 1))).toBe(false);
    expect(shouldRetry(0, problem("getCitationDocument", 503, "dependency_unavailable"))).toBe(
      false,
    );
    expect(shouldRetry(0, failure({ kind: "network", operationId: "adminGetDocumentPdf" }))).toBe(
      false,
    );
  });

  test("the query client uses the policy and never retries mutations", () => {
    const client = createQueryClient();
    const defaults = client.getDefaultOptions();
    expect(defaults.queries?.retry).toBe(shouldRetry);
    expect(defaults.queries?.gcTime).toBe(300_000);
    expect(defaults.mutations?.retry).toBe(false);
    const delay = defaults.queries?.retryDelay;
    expect(typeof delay === "function" ? delay(0, problem("getMemory", 429, "x", 3)) : null).toBe(
      3_000,
    );
  });
});

describe("query keys and freshness", () => {
  test("section 5.8's freshness applies per key, the longer key winning", () => {
    const client = createQueryClient();
    const freshness = (key: readonly unknown[]) => {
      const options = client.getQueryDefaults(key);
      return [options.staleTime, options.refetchOnWindowFocus];
    };
    expect(freshness(queryKeys.me)).toEqual([300_000, true]);
    expect(freshness(queryKeys.myProjects)).toEqual([60_000, true]);
    expect(freshness(queryKeys.sessions)).toEqual([30_000, true]);
    expect(freshness(queryKeys.session(IDS.session))).toEqual([300_000, false]);
    expect(freshness(queryKeys.sessionEarlier(IDS.session))).toEqual([300_000, false]);
    expect(freshness(queryKeys.citation(IDS.session, IDS.turn, 1))).toEqual([300_000, false]);
    expect(freshness(queryKeys.memory)).toEqual([0, true]);
    expect(freshness(queryKeys.admin("indexes", { limit: 50 }))).toEqual([30_000, true]);
    expect(freshness(queryKeys.adminProject(IDS.projectA))).toEqual([30_000, true]);
    expect(freshness(queryKeys.adminMembers(IDS.projectA))).toEqual([0, true]);
    expect(freshness(queryKeys.adminProjectIndexes(IDS.projectA))).toEqual([0, true]);
    expect(freshness(queryKeys.adminUsers("ad"))).toEqual([30_000, false]);
    expect(freshness(queryKeys.adminIndexOptions)).toEqual([900_000, false]);
  });

  test("T-363 a projectRowVersion replaces the cached project's rowVersion", () => {
    const client = createQueryClient();
    applyProjectRowVersion(client, IDS.projectA, "AAAAAAAAB9F=");
    expect(client.getQueryData(queryKeys.adminProject(IDS.projectA))).toBeUndefined();
    client.setQueryData(queryKeys.adminProject(IDS.projectA), {
      projectId: IDS.projectA,
      rowVersion: "AAAAAAAAB9E=",
    });
    applyProjectRowVersion(client, IDS.projectA, "AAAAAAAAB9F=");
    expect(client.getQueryData(queryKeys.adminProject(IDS.projectA))).toEqual({
      projectId: IDS.projectA,
      rowVersion: "AAAAAAAAB9F=",
    });
  });
});

describe("shared queries", () => {
  test("getCurrentUser is the me query", async () => {
    configureTestHttp();
    const client = createQueryClient();
    expect((await client.query(meQuery())).email).toBe(CURRENT_USER.email);
  });

  test("listMyProjects loads every page with limit=100", async () => {
    configureTestHttp();
    const urls: string[] = [];
    server.use(
      http.get(apiUrl("/v1/projects"), ({ request }) => {
        urls.push(request.url);
        const cursor = new URL(request.url).searchParams.get("cursor");
        return HttpResponse.json(
          cursor === null
            ? { items: PROJECTS, nextCursor: "page2" }
            : {
                items: [{ ...PROJECTS[0], projectId: IDS.projectB, name: "Beta" }],
                nextCursor: null,
              },
        );
      }),
    );
    const projects = await createQueryClient().query(myProjectsQuery());
    expect(projects.map((project) => project.name)).toEqual(["Alpha", "Beta"]);
    expect(urls).toEqual([
      apiUrl("/v1/projects?limit=100"),
      apiUrl("/v1/projects?limit=100&cursor=page2"),
    ]);
  });

  test("listMyProjects stops after 20 pages", async () => {
    configureTestHttp();
    let calls = 0;
    server.use(
      http.get(apiUrl("/v1/projects"), () => {
        calls += 1;
        return HttpResponse.json({ items: [], nextCursor: "more" });
      }),
    );
    await createQueryClient().query(myProjectsQuery());
    expect(calls).toBe(MY_PROJECTS_MAX_PAGES);
  });

  test("listSessions pages with limit=30 and its sessions are de-duplicated", async () => {
    configureTestHttp();
    const session = (sessionId: string, title: string) => ({
      sessionId,
      projectId: IDS.projectA,
      title,
      createdAt: "2026-11-01T10:00:00Z",
      lastTurnAt: "2026-11-02T10:00:00Z",
      turnCount: 1,
    });
    const urls: string[] = [];
    server.use(
      http.get(apiUrl("/v1/sessions"), ({ request }) => {
        urls.push(request.url);
        return HttpResponse.json({ items: [session(IDS.session, "One")], nextCursor: "next" });
      }),
    );
    const client = createQueryClient();
    await client.infiniteQuery(sessionsQuery());
    expect(urls).toEqual([apiUrl("/v1/sessions?limit=30")]);
    const page: SessionPage = { items: [], nextCursor: "next" };
    expect(sessionsQuery().getNextPageParam(page, [page], null, [null])).toBe("next");
    const twoPages = {
      pages: [
        { items: [session(IDS.session, "One"), session(IDS.turn, "Two")], nextCursor: "x" },
        {
          items: [session(IDS.session, "One again"), session(IDS.clientTurn, "Three")],
          nextCursor: null,
        },
      ],
      pageParams: [null, "x"],
    };
    expect(sessionsOf(twoPages).map((item) => item.title)).toEqual(["One", "Two", "Three"]);
    expect(sessionsOf(undefined)).toEqual([]);
  });
});
