// The admin screens' queries (10-web-client.md sections 5.8 and 7.1): the keys under `['admin', …]`,
// the reads several screens share, and paged lists that request `limit=50` and append each next
// page in the API's order.

import { infiniteQueryOptions, queryOptions, type InfiniteData } from "@tanstack/react-query";

import { request } from "../../api/http";
import type { ResponseOf } from "../../api/operations";
import { queryKeys } from "../../api/queryClient";
import { ADMIN_LIST_PAGE_SIZE, USER_SEARCH_PAGE_SIZE } from "../../platform/constants";

export type AdminProject = ResponseOf<"adminGetProject">;
export type AdminProjectSummary = ResponseOf<"adminListProjects">["items"][number];
export type ProjectMember = ResponseOf<"adminListProjectMembers">["items"][number];
export type ProjectIndexMapping = ResponseOf<"adminListProjectIndexes">["items"][number];
export type UserSummary = ResponseOf<"adminSearchUsers">["items"][number];
export type IndexSummary = ResponseOf<"adminListIndexes">["items"][number];
export type IndexDetail = ResponseOf<"adminGetIndex">;
export type IndexOptions = ResponseOf<"adminGetIndexOptions">;
export type MethodOption = IndexOptions["methods"][number];
export type IndexStatus = IndexSummary["status"];
export type DocumentSummary = ResponseOf<"adminListDocuments">["items"][number];
export type DocumentDetail = ResponseOf<"adminGetDocument">;
export type RunSummary = ResponseOf<"adminListIngestionRuns">["items"][number];
export type IngestionRunDetail = ResponseOf<"adminGetIngestionRun">;
export type RunItem = IngestionRunDetail["items"][number];

/** Every page shape of an admin list. */
export interface Page<T> {
  readonly items: readonly T[];
  readonly nextCursor: string | null;
}

/** The most pages a "every page" read follows (section 7.6, Map an index). */
export const ALL_PAGES_MAX = 20;

/** The keys of the admin reads that more than one screen uses. */
export const adminKeys = {
  project: queryKeys.adminProject,
  /** `['admin', 'projects', {q, status}]`: the Projects list for one filter. */
  projects: (params: { readonly q: string; readonly status: string }) =>
    queryKeys.admin("projects", params),
  /** `['admin', 'index', indexId]`: `adminGetIndex`, shared by Index and Documents. */
  index: (indexId: string) => queryKeys.admin("index", indexId),
  /** `['admin', 'indexes', params]`: the Indexes list, and every page of it (`{all: true}`). */
  indexes: (params: object) => queryKeys.admin("indexes", params),
  /** The prefix of every index list, invalidated after a create or a delete. */
  indexLists: ["admin", "indexes"] as const,
  indexOptions: queryKeys.adminIndexOptions,
  /** `['admin', 'users', q]`, with the project when the search names one (its `isMember`). */
  users: (q: string, projectId?: string) =>
    projectId === undefined
      ? queryKeys.adminUsers(q)
      : ([...queryKeys.adminUsers(q), { projectId }] as const),
  /** `['admin', 'documents', {indexId, q, state}]`: one index's Documents list for one filter. */
  documents: (params: { readonly indexId: string; readonly q: string; readonly state: string }) =>
    queryKeys.admin("documents", params),
  /** The prefix of every Documents list, invalidated after an upload, a re-ingest or a delete. */
  documentLists: ["admin", "documents"] as const,
  /** `['admin', 'document', documentId]`: `adminGetDocument` for the document panel. */
  document: (documentId: string) => queryKeys.admin("document", documentId),
  /** `['admin', 'runs', {indexId, state}]`: the Ingestion runs list for one filter. */
  runs: (params: { readonly indexId: string; readonly state: string }) =>
    queryKeys.admin("runs", params),
  /** The prefix of every Ingestion runs list, invalidated when an action starts a run. */
  runLists: ["admin", "runs"] as const,
  /** `['admin', 'run', runId]`: `adminGetIngestionRun` and its further pages of items. */
  run: (runId: string) => queryKeys.admin("run", runId),
};

/** Every item of an infinite list, in the API's order: the client never sorts a page. */
export function itemsOf<T>(data: InfiniteData<Page<T>> | undefined): T[] {
  return (data?.pages ?? []).flatMap((page) => page.items);
}

/** A paged admin list: `fetchPage` asks for one page after `cursor` (null for the first). A new
 * `params` value is a new key, so a filter change starts from the first page. */
export function adminListQuery<T>(
  resource: string,
  params: unknown,
  fetchPage: (cursor: string | null, signal: AbortSignal) => Promise<Page<T>>,
) {
  return infiniteQueryOptions({
    queryKey: queryKeys.admin(resource, params),
    queryFn: ({ pageParam, signal }) => fetchPage(pageParam, signal),
    initialPageParam: null as string | null,
    getNextPageParam: (last: Page<T>) => last.nextCursor,
  });
}

/** The `cursor` query member, present only after the first page. */
export function cursorQuery(cursor: string | null): { cursor?: string } {
  return cursor === null ? {} : { cursor };
}

/** `adminGetProject` under `['admin', 'project', projectId]`. */
export function adminProjectQuery(projectId: string) {
  return queryOptions({
    queryKey: adminKeys.project(projectId),
    queryFn: ({ signal }) => request("adminGetProject", { path: { projectId }, signal }),
  });
}

/** `adminGetIndex` under `['admin', 'index', indexId]`. */
export function adminIndexQuery(indexId: string) {
  return queryOptions({
    queryKey: adminKeys.index(indexId),
    queryFn: ({ signal }) => request("adminGetIndex", { path: { indexId }, signal }),
  });
}

/** `adminGetIndexOptions`, fresh for 900 s. */
export function indexOptionsQuery() {
  return queryOptions({
    queryKey: adminKeys.indexOptions,
    queryFn: ({ signal }) => request("adminGetIndexOptions", { signal }),
  });
}

/** Every index that isn't deleted: `adminListIndexes` with `limit=50`, every page, at most 20
 * (Map an index; the runs filter). */
export function allIndexesQuery() {
  return queryOptions({
    queryKey: adminKeys.indexes({ all: true }),
    queryFn: async ({ signal }) => {
      const items: IndexSummary[] = [];
      let cursor: string | null = null;
      for (let page = 0; page < ALL_PAGES_MAX; page += 1) {
        const answer: ResponseOf<"adminListIndexes"> = await request("adminListIndexes", {
          query: { limit: ADMIN_LIST_PAGE_SIZE, ...cursorQuery(cursor) },
          signal,
        });
        items.push(...answer.items);
        cursor = answer.nextCursor;
        if (cursor === null) break;
      }
      return items;
    },
  });
}

/** The people picker's search: `adminSearchUsers` with `limit=20`, and `projectId` when the
 * picker adds to a project, so that current members come back with `isMember: true`. */
export function userSearchQuery(q: string, projectId?: string) {
  return queryOptions({
    queryKey: adminKeys.users(q, projectId),
    queryFn: ({ signal }) =>
      request("adminSearchUsers", {
        query: {
          q,
          limit: USER_SEARCH_PAGE_SIZE,
          ...(projectId === undefined ? {} : { projectId }),
        },
        signal,
      }),
  });
}
