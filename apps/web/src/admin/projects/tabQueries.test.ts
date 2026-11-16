import { HttpResponse, delay, http } from "msw";
import { beforeEach, describe, expect, test } from "vitest";

import { adminProject, mapping, member, page, rowVersion, uuid } from "../../test/adminData";
import { apiUrl } from "../../test/handlers";
import { configureTestHttp, createTestQueryClient } from "../../test/render";
import { server } from "../../test/server";
import { adminKeys, type AdminProject } from "../shared/queries";
import { membersTabQuery, projectIndexesTabQuery } from "./tabQueries";

const PROJECT = uuid(1);

beforeEach(() => {
  configureTestHttp();
});

describe("the project-first tab reads (section 5.6 step 3)", () => {
  test.each([
    ["members", membersTabQuery, "/members", "100", page([member(1)], "next")],
    ["indexes", projectIndexesTabQuery, "/indexes", "50", page([mapping(1)], "next")],
  ] as const)(
    "T-399 the %s tab reads the project, then its first page, and caches the project only after it",
    async (_, query, suffix, limit, firstPage) => {
      const client = createTestQueryClient();
      client.setQueryData(adminKeys.project(PROJECT), adminProject({ rowVersion: rowVersion(1) }));
      const order: string[] = [];
      let cachedWhileListing: string | undefined;
      server.use(
        http.get(apiUrl(`/v1/admin/projects/${PROJECT}`), () => {
          order.push("project");
          return HttpResponse.json(adminProject({ rowVersion: rowVersion(2) }));
        }),
        http.get(apiUrl(`/v1/admin/projects/${PROJECT}${suffix}`), async ({ request }) => {
          const url = new URL(request.url);
          order.push(`list:${url.searchParams.get("cursor") ?? "first"}`);
          expect(url.searchParams.get("limit")).toBe(limit);
          cachedWhileListing = client.getQueryData<AdminProject>(
            adminKeys.project(PROJECT),
          )?.rowVersion;
          await delay(5);
          return HttpResponse.json(url.searchParams.has("cursor") ? page([]) : firstPage);
        }),
      );
      // The query function is typed per tab; both take the client and the project.
      const options = query(client, PROJECT) as ReturnType<typeof membersTabQuery>;
      const data = await client.infiniteQuery(options);
      expect(order).toEqual(["project", "list:first"]);
      expect(cachedWhileListing).toBe(rowVersion(1));
      expect(client.getQueryData<AdminProject>(adminKeys.project(PROJECT))?.rowVersion).toBe(
        rowVersion(2),
      );
      expect(data.pages[0]?.nextCursor).toBe("next");
    },
  );

  test("a later page reads only the list", async () => {
    const client = createTestQueryClient();
    const order: string[] = [];
    server.use(
      http.get(apiUrl(`/v1/admin/projects/${PROJECT}`), () => {
        order.push("project");
        return HttpResponse.json(adminProject());
      }),
      http.get(apiUrl(`/v1/admin/projects/${PROJECT}/members`), ({ request }) => {
        const cursor = new URL(request.url).searchParams.get("cursor");
        order.push(`list:${cursor ?? "first"}`);
        return HttpResponse.json(cursor === null ? page([member(1)], "c2") : page([member(2)]));
      }),
    );
    const options = membersTabQuery(client, PROJECT);
    const queryFn = options.queryFn;
    if (typeof queryFn !== "function") throw new Error("no query function");
    const signal = new AbortController().signal;
    const later = await queryFn({
      pageParam: "c2",
      signal,
      queryKey: options.queryKey,
      client,
      direction: "forward",
      meta: undefined,
    });
    expect(order).toEqual(["list:c2"]);
    expect(later.items).toHaveLength(1);
  });
});
