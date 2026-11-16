// Server state: the one TanStack Query client, its query keys, freshness and retry policy
// (10-web-client.md sections 1.8, 5.7 and 5.8).

import {
  QueryClient,
  infiniteQueryOptions,
  queryOptions,
  type InfiniteData,
} from "@tanstack/react-query";

import {
  SESSION_PAGE_SIZE,
  QUERY_RETRIES,
  QUERY_RETRY_AFTER_LIMIT_S,
  QUERY_RETRY_DELAYS_MS,
  QUERY_RETRY_JITTER_MS,
} from "../platform/constants";
import { request } from "./http";
import { operations, type OperationId, type ResponseOf } from "./operations";
import { apiErrorOf, type ApiError } from "./problem";

/** Cached data is kept this long after its last use. */
export const GC_TIME_MS = 300_000;

/** Every page of `listMyProjects`: `limit=100`, at most 20 pages (section 5.8). */
export const MY_PROJECTS_PAGE_SIZE = 100;
export const MY_PROJECTS_MAX_PAGES = 20;

export const queryKeys = {
  me: ["me"] as const,
  myProjects: ["projects", "mine"] as const,
  sessions: ["sessions"] as const,
  session: (sessionId: string) => ["session", sessionId] as const,
  sessionEarlier: (sessionId: string) => ["session", sessionId, "earlier"] as const,
  citation: (sessionId: string, turnId: string, citationNumber: number) =>
    ["citation", sessionId, turnId, citationNumber] as const,
  memory: ["memory"] as const,
  /** Every admin list and read not listed below. */
  admin: (resource: string, params: unknown) => ["admin", resource, params] as const,
  adminProject: (projectId: string) => ["admin", "project", projectId] as const,
  adminMembers: (projectId: string) => ["admin", "members", projectId] as const,
  adminProjectIndexes: (projectId: string) => ["admin", "project-indexes", projectId] as const,
  adminUsers: (q: string) => ["admin", "users", q] as const,
  adminIndexOptions: ["admin", "index-options"] as const,
};

interface Freshness {
  readonly queryKey: readonly unknown[];
  readonly staleTime: number;
  readonly refetchOnWindowFocus: boolean;
}

/** Section 5.8's table. Registered general first: TanStack merges every matching default in
 * registration order, so a longer key overrides the shorter one it starts with. */
export const FRESHNESS: readonly Freshness[] = [
  { queryKey: ["me"], staleTime: 300_000, refetchOnWindowFocus: true },
  { queryKey: ["projects", "mine"], staleTime: 60_000, refetchOnWindowFocus: true },
  { queryKey: ["sessions"], staleTime: 30_000, refetchOnWindowFocus: true },
  { queryKey: ["session"], staleTime: 300_000, refetchOnWindowFocus: false },
  { queryKey: ["citation"], staleTime: 300_000, refetchOnWindowFocus: false },
  { queryKey: ["memory"], staleTime: 0, refetchOnWindowFocus: true },
  { queryKey: ["admin"], staleTime: 30_000, refetchOnWindowFocus: true },
  // The project query pauses its own refetches while the Members or Indexes tab shows; the tab
  // screen turns refetchOnWindowFocus off for that time (section 5.6).
  { queryKey: ["admin", "project"], staleTime: 30_000, refetchOnWindowFocus: true },
  // Refetched on each tab opening.
  { queryKey: ["admin", "members"], staleTime: 0, refetchOnWindowFocus: true },
  { queryKey: ["admin", "project-indexes"], staleTime: 0, refetchOnWindowFocus: true },
  { queryKey: ["admin", "users"], staleTime: 30_000, refetchOnWindowFocus: false },
  { queryKey: ["admin", "index-options"], staleTime: 900_000, refetchOnWindowFocus: false },
];

const FILE_OPERATIONS: ReadonlySet<string> = new Set(
  Object.entries(operations)
    .filter(([, spec]) => spec.answer === "file")
    .map(([id]) => id),
);

const RETRIED_STATUSES: ReadonlySet<number> = new Set([408, 500, 502, 503, 504]);

/** Whether a failed query may be repeated automatically (section 5.7). `failureCount` is the
 * number of earlier failures, as TanStack passes it. Writes never repeat: their mutations have
 * retries off, and a write that reaches this function is refused here too. */
export function shouldRetry(failureCount: number, reason: unknown): boolean {
  const error = apiErrorOf(reason);
  if (failureCount >= QUERY_RETRIES || error?.operationId == null) return false;
  const operationId = error.operationId;
  if (!(operationId in operations) || operations[operationId as OperationId].method !== "GET") {
    return false;
  }
  if (FILE_OPERATIONS.has(operationId)) {
    return error.kind === "problem" && error.status === 429 && error.code === "too_many_requests";
  }
  if (error.kind === "network") return true;
  if (error.status === null || (error.kind !== "problem" && error.kind !== "http")) return false;
  if (RETRIED_STATUSES.has(error.status)) return true;
  return (
    error.status === 429 &&
    error.retryAfterSeconds !== null &&
    error.retryAfterSeconds <= QUERY_RETRY_AFTER_LIMIT_S
  );
}

/** The wait before repeat number `failureCount + 1`: the answer's `retryAfterSeconds`, otherwise
 * 1,000 ms then 4,000 ms, each plus 0 to 250 ms of jitter. */
export function retryDelay(
  failureCount: number,
  reason: unknown,
  random: () => number = Math.random,
): number {
  const error: ApiError | null = apiErrorOf(reason);
  if (error?.retryAfterSeconds != null) return error.retryAfterSeconds * 1000;
  const base = QUERY_RETRY_DELAYS_MS[Math.min(failureCount, QUERY_RETRY_DELAYS_MS.length - 1)] ?? 0;
  return base + Math.floor(random() * (QUERY_RETRY_JITTER_MS + 1));
}

export function createQueryClient(): QueryClient {
  const client = new QueryClient({
    defaultOptions: {
      queries: {
        gcTime: GC_TIME_MS,
        retry: shouldRetry,
        retryDelay: (failureCount, error) => retryDelay(failureCount, error),
      },
      mutations: { retry: false },
    },
  });
  for (const { queryKey, staleTime, refetchOnWindowFocus } of FRESHNESS) {
    client.setQueryDefaults(queryKey, { staleTime, refetchOnWindowFocus });
  }
  return client;
}

/** `getCurrentUser`: the first call after sign-in, which every other query waits for. */
export function meQuery() {
  return queryOptions({
    queryKey: queryKeys.me,
    queryFn: ({ signal }) => request("getCurrentUser", { signal }),
  });
}

export type ProjectSummary = ResponseOf<"listMyProjects">["items"][number];

/** Every reachable project: each page of `listMyProjects`, at most 20. */
export function myProjectsQuery() {
  return queryOptions({
    queryKey: queryKeys.myProjects,
    queryFn: async ({ signal }) => {
      const items: ProjectSummary[] = [];
      let cursor: string | null = null;
      for (let page = 0; page < MY_PROJECTS_MAX_PAGES; page += 1) {
        const answer: ResponseOf<"listMyProjects"> = await request("listMyProjects", {
          query: { limit: MY_PROJECTS_PAGE_SIZE, ...(cursor === null ? {} : { cursor }) },
          signal,
        });
        items.push(...answer.items);
        cursor = answer.nextCursor ?? null;
        if (cursor === null) break;
      }
      return items;
    },
  });
}

export type SessionPage = ResponseOf<"listSessions">;
export type SessionSummary = SessionPage["items"][number];

/** `['sessions']`: `listSessions` with `limit=30`, one page at a time, newest first. The session
 * panel shows Show more chats while `hasNextPage`; Recent chats shows the first six. */
export function sessionsQuery() {
  return infiniteQueryOptions({
    queryKey: queryKeys.sessions,
    queryFn: ({ pageParam, signal }) =>
      request("listSessions", {
        query: { limit: SESSION_PAGE_SIZE, ...(pageParam === null ? {} : { cursor: pageParam }) },
        signal,
      }),
    initialPageParam: null as string | null,
    getNextPageParam: (last: SessionPage) => last.nextCursor,
  });
}

/** Every loaded session in the API's order, each once: a session that receives a turn while its
 * owner pages moves to the top, so a later page can repeat it (section 5.8). */
export function sessionsOf(data: InfiniteData<SessionPage> | undefined): SessionSummary[] {
  const seen = new Set<string>();
  const sessions: SessionSummary[] = [];
  for (const page of data?.pages ?? []) {
    for (const session of page.items) {
      if (seen.has(session.sessionId)) continue;
      seen.add(session.sessionId);
      sessions.push(session);
    }
  }
  return sessions;
}

/** Writes a membership or mapping answer's `projectRowVersion` into the cached project, so one
 * admin's consecutive changes chain without a reload (section 5.6 step 2). */
export function applyProjectRowVersion(
  client: QueryClient,
  projectId: string,
  projectRowVersion: string,
): void {
  client.setQueryData<ResponseOf<"adminGetProject">>(
    queryKeys.adminProject(projectId),
    (project) => (project === undefined ? project : { ...project, rowVersion: projectRowVersion }),
  );
}
