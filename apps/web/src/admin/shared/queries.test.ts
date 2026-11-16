import { QueryClient } from "@tanstack/react-query";
import { HttpResponse, http } from "msw";
import { beforeEach, describe, expect, test } from "vitest";

import { indexSummary, page, userSummary, uuid } from "../../test/adminData";
import { apiUrl } from "../../test/handlers";
import { configureTestHttp } from "../../test/render";
import { server } from "../../test/server";
import {
  ALL_PAGES_MAX,
  adminIndexQuery,
  adminKeys,
  adminProjectQuery,
  allIndexesQuery,
  cursorQuery,
  indexOptionsQuery,
  itemsOf,
  userSearchQuery,
} from "./queries";

const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });

beforeEach(() => {
  configureTestHttp();
  client.clear();
});

describe("the admin queries", () => {
  test("the keys of section 5.8", () => {
    expect(adminKeys.project("p")).toEqual(["admin", "project", "p"]);
    expect(adminKeys.index("i")).toEqual(["admin", "index", "i"]);
    expect(adminKeys.indexes({ all: true })).toEqual(["admin", "indexes", { all: true }]);
    expect(adminKeys.indexOptions).toEqual(["admin", "index-options"]);
    expect(adminKeys.users("ad")).toEqual(["admin", "users", "ad"]);
    expect(adminKeys.users("ad", "p")).toEqual(["admin", "users", "ad", { projectId: "p" }]);
    expect(adminKeys.projects({ q: "", status: "active" })).toEqual([
      "admin",
      "projects",
      { q: "", status: "active" },
    ]);
    expect(adminKeys.documents({ indexId: "i", q: "", state: "all" })).toEqual([
      "admin",
      "documents",
      { indexId: "i", q: "", state: "all" },
    ]);
    expect(adminKeys.documentLists).toEqual(["admin", "documents"]);
    expect(adminKeys.document("d")).toEqual(["admin", "document", "d"]);
    expect(adminKeys.runs({ indexId: "all", state: "all" })).toEqual([
      "admin",
      "runs",
      { indexId: "all", state: "all" },
    ]);
    expect(adminKeys.runLists).toEqual(["admin", "runs"]);
    expect(adminKeys.run("r")).toEqual(["admin", "run", "r"]);
    expect(cursorQuery(null)).toEqual({});
    expect(cursorQuery("c")).toEqual({ cursor: "c" });
    expect(itemsOf(undefined)).toEqual([]);
    expect(itemsOf({ pages: [page([1, 2], "c"), page([3])], pageParams: [null, "c"] })).toEqual([
      1, 2, 3,
    ]);
  });

  test("every index: each page with limit=50, at most 20 pages", async () => {
    const cursors: (string | null)[] = [];
    server.use(
      http.get(apiUrl("/v1/admin/indexes"), ({ request }) => {
        const url = new URL(request.url);
        expect(url.searchParams.get("limit")).toBe("50");
        const cursor = url.searchParams.get("cursor");
        cursors.push(cursor);
        return HttpResponse.json(
          page([indexSummary(cursors.length)], `c${String(cursors.length)}`),
        );
      }),
    );
    const items = await client.query(allIndexesQuery());
    expect(items).toHaveLength(ALL_PAGES_MAX);
    expect(cursors.slice(0, 3)).toEqual([null, "c1", "c2"]);
  });

  test("the people search sends q, limit=20 and the project when given", async () => {
    const seen: URLSearchParams[] = [];
    server.use(
      http.get(apiUrl("/v1/admin/users"), ({ request }) => {
        seen.push(new URL(request.url).searchParams);
        return HttpResponse.json(page([userSummary(1)]));
      }),
    );
    await client.query(userSearchQuery("ad"));
    await client.query(userSearchQuery("ad", uuid(1)));
    expect(seen.map((params) => Object.fromEntries(params))).toEqual([
      { q: "ad", limit: "20" },
      { q: "ad", limit: "20", projectId: uuid(1) },
    ]);
  });

  test("project, index and options reads use their operations", () => {
    expect(adminProjectQuery("p").queryKey).toEqual(["admin", "project", "p"]);
    expect(adminIndexQuery("i").queryKey).toEqual(["admin", "index", "i"]);
    expect(indexOptionsQuery().queryKey).toEqual(["admin", "index-options"]);
  });
});
