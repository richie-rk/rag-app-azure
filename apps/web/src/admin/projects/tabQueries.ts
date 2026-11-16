// The Members and Indexes tabs read the project first (10-web-client.md section 5.6 step 3): every
// load or reload of either tab requests `adminGetProject`, then the first page of its list, and
// writes the project into the cache only after that page has arrived. So the version a tab sends
// is never newer than the rows it shows.

import { infiniteQueryOptions, type QueryClient } from "@tanstack/react-query";

import { request } from "../../api/http";
import { queryKeys } from "../../api/queryClient";
import { ADMIN_LIST_PAGE_SIZE, MEMBERS_PAGE_SIZE } from "../../platform/constants";
import {
  adminKeys,
  cursorQuery,
  type Page,
  type ProjectIndexMapping,
  type ProjectMember,
} from "../shared/queries";

/** A tab's first page: the project, then the page, then the project into the cache. */
async function projectThenPage<T>(
  client: QueryClient,
  projectId: string,
  signal: AbortSignal,
  fetchPage: () => Promise<Page<T>>,
): Promise<Page<T>> {
  const project = await request("adminGetProject", { path: { projectId }, signal });
  const page = await fetchPage();
  client.setQueryData(adminKeys.project(projectId), project);
  return page;
}

/** `['admin', 'members', projectId]`: `adminListProjectMembers`, `limit=100`. */
export function membersTabQuery(client: QueryClient, projectId: string) {
  return infiniteQueryOptions({
    queryKey: queryKeys.adminMembers(projectId),
    queryFn: ({ pageParam, signal }): Promise<Page<ProjectMember>> => {
      const fetchPage = () =>
        request("adminListProjectMembers", {
          path: { projectId },
          query: { limit: MEMBERS_PAGE_SIZE, ...cursorQuery(pageParam) },
          signal,
        });
      return pageParam === null
        ? projectThenPage(client, projectId, signal, fetchPage)
        : fetchPage();
    },
    initialPageParam: null as string | null,
    getNextPageParam: (last: Page<ProjectMember>) => last.nextCursor,
  });
}

/** `['admin', 'project-indexes', projectId]`: `adminListProjectIndexes`, `limit=50`. */
export function projectIndexesTabQuery(client: QueryClient, projectId: string) {
  return infiniteQueryOptions({
    queryKey: queryKeys.adminProjectIndexes(projectId),
    queryFn: ({ pageParam, signal }): Promise<Page<ProjectIndexMapping>> => {
      const fetchPage = () =>
        request("adminListProjectIndexes", {
          path: { projectId },
          query: { limit: ADMIN_LIST_PAGE_SIZE, ...cursorQuery(pageParam) },
          signal,
        });
      return pageParam === null
        ? projectThenPage(client, projectId, signal, fetchPage)
        : fetchPage();
    },
    initialPageParam: null as string | null,
    getNextPageParam: (last: Page<ProjectIndexMapping>) => last.nextCursor,
  });
}
