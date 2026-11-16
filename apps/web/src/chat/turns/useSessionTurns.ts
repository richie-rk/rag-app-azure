// An open session's stored turns (10-web-client.md sections 5.8, 8.12 and 8.13): `getSession`
// with its newest 30 turns, and the pages Show earlier messages loads with `listSessionTurns`.
// When a refetch moves `getSession`'s window past turns that were shown, those turns move into the
// earlier pages, so nothing loaded disappears from the conversation.

import {
  useInfiniteQuery,
  useQuery,
  useQueryClient,
  type InfiniteData,
} from "@tanstack/react-query";
import { useEffect, useMemo, useRef } from "react";

import { request } from "../../api/http";
import type { ResponseOf } from "../../api/operations";
import { apiErrorOf, clientError } from "../../api/problem";
import { queryKeys } from "../../api/queryClient";
import { TURNS_PAGE_SIZE } from "../../platform/constants";
import type { EarlierTurns } from "./Conversation";
import type { StoredTurn } from "./turnModel";

type SessionDetail = ResponseOf<"getSession">;
type TurnPage = ResponseOf<"listSessionTurns">;

/** The open session's `getSession` query. */
export function sessionQuery(sessionId: string) {
  return {
    queryKey: queryKeys.session(sessionId),
    queryFn: ({ signal }: { signal: AbortSignal }) =>
      request("getSession", {
        path: { sessionId },
        query: { turnsLimit: TURNS_PAGE_SIZE },
        signal,
      }),
  };
}

/** Puts turns that left `getSession`'s window into the newest earlier page. */
export function keepDroppedTurns(
  data: InfiniteData<TurnPage> | undefined,
  dropped: readonly StoredTurn[],
): InfiniteData<TurnPage> | undefined {
  const [first, ...rest] = data?.pages ?? [];
  if (data === undefined || first === undefined || dropped.length === 0) return data;
  const known = new Set(first.items.map((turn) => turn.turnId));
  const items = [...first.items, ...dropped.filter((turn) => !known.has(turn.turnId))].sort(
    (a, b) => a.seq - b.seq,
  );
  return { ...data, pages: [{ ...first, items }, ...rest] };
}

export function useSessionTurns(sessionId: string | null) {
  const client = useQueryClient();
  const key = sessionId ?? "";
  const session = useQuery({ ...sessionQuery(key), enabled: sessionId !== null });
  const earlier = useInfiniteQuery({
    queryKey: queryKeys.sessionEarlier(key),
    queryFn: ({ pageParam, signal }) =>
      request("listSessionTurns", {
        path: { sessionId: key },
        query: { limit: TURNS_PAGE_SIZE, ...(pageParam === null ? {} : { cursor: pageParam }) },
        signal,
      }),
    initialPageParam: session.data?.earlierTurnsCursor ?? null,
    getNextPageParam: (last: TurnPage) => last.nextCursor,
    // Loaded only when asked for, so an invalidated session never refetches every earlier page.
    enabled: false,
  });

  const previous = useRef<SessionDetail | undefined>(undefined);
  useEffect(() => {
    const before = previous.current;
    const now = session.data;
    previous.current = now;
    if (now === undefined || before?.sessionId !== now.sessionId) return;
    const current = new Set(now.turns.map((turn) => turn.turnId));
    const oldest = now.turns[0]?.seq ?? Number.MAX_SAFE_INTEGER;
    const dropped = before.turns.filter((turn) => !current.has(turn.turnId) && turn.seq < oldest);
    client.setQueryData<InfiniteData<TurnPage>>(queryKeys.sessionEarlier(now.sessionId), (data) =>
      keepDroppedTurns(data, dropped),
    );
  }, [client, session.data]);

  const stored = useMemo(() => {
    const byId = new Map<string, StoredTurn>();
    for (const page of [...(earlier.data?.pages ?? [])].reverse()) {
      for (const turn of page.items) byId.set(turn.turnId, turn);
    }
    for (const turn of session.data?.turns ?? []) byId.set(turn.turnId, turn);
    return [...byId.values()].sort((a, b) => a.seq - b.seq);
  }, [earlier.data, session.data]);

  const loadEarlier = () => {
    if (earlier.data === undefined) void earlier.refetch();
    else void earlier.fetchNextPage();
  };
  const earlierTurns: EarlierTurns = {
    available:
      earlier.data === undefined
        ? (session.data?.earlierTurnsCursor ?? null) !== null
        : earlier.hasNextPage,
    loading: earlier.isFetching,
    error: earlier.isError ? (apiErrorOf(earlier.error) ?? clientError("network")) : null,
    load: loadEarlier,
  };

  return { session, earlier: earlierTurns, stored };
}
