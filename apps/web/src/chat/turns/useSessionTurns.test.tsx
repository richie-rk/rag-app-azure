import { QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import { HttpResponse, http } from "msw";
import type { ReactNode } from "react";
import { describe, expect, test } from "vitest";

import { sessionDetail, storedTurn } from "../../test/chat";
import { IDS, apiUrl, problemResponse } from "../../test/handlers";
import { configureTestHttp, createTestQueryClient } from "../../test/render";
import { server } from "../../test/server";
import { keepDroppedTurns, useSessionTurns } from "./useSessionTurns";

const turns = (from: number, to: number) =>
  Array.from({ length: to - from + 1 }, (_, index) => storedTurn({ seq: from + index }));

function hook(sessionId: string | null) {
  configureTestHttp();
  const client = createTestQueryClient();
  const wrapper = ({ children }: { readonly children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
  return { client, ...renderHook(() => useSessionTurns(sessionId), { wrapper }) };
}

describe("an open session's stored turns", () => {
  test("getSession asks for the newest 30 turns; nothing is read for a new chat", async () => {
    const queries: string[] = [];
    server.use(
      http.get(apiUrl(`/v1/sessions/${IDS.session}`), ({ request }) => {
        queries.push(new URL(request.url).search);
        return HttpResponse.json(sessionDetail(turns(1, 2)));
      }),
    );
    const { result } = hook(IDS.session);
    await waitFor(() => {
      expect(result.current.stored).toHaveLength(2);
    });
    expect(queries).toEqual(["?turnsLimit=30"]);
    expect(result.current.earlier.available).toBe(false);
    expect(hook(null).result.current.stored).toEqual([]);
  });

  test("Show earlier messages reads listSessionTurns from the cursor, then the next page", async () => {
    const cursors: (string | null)[] = [];
    server.use(
      http.get(apiUrl(`/v1/sessions/${IDS.session}`), () =>
        HttpResponse.json(sessionDetail(turns(61, 90), { earlierTurnsCursor: "before-61" })),
      ),
      http.get(apiUrl(`/v1/sessions/${IDS.session}/turns`), ({ request }) => {
        const params = new URL(request.url).searchParams;
        cursors.push(params.get("cursor"));
        expect(params.get("limit")).toBe("30");
        return params.get("cursor") === "before-61"
          ? HttpResponse.json({ items: turns(31, 60), nextCursor: "before-31" })
          : HttpResponse.json({ items: turns(1, 30), nextCursor: null });
      }),
    );
    const { result } = hook(IDS.session);
    await waitFor(() => {
      expect(result.current.earlier.available).toBe(true);
    });
    act(() => {
      result.current.earlier.load();
    });
    await waitFor(() => {
      expect(result.current.stored).toHaveLength(60);
    });
    act(() => {
      result.current.earlier.load();
    });
    await waitFor(() => {
      expect(result.current.stored).toHaveLength(90);
    });
    expect(result.current.stored.map((turn) => turn.seq)).toEqual(
      turns(1, 90).map((turn) => turn.seq),
    );
    expect(cursors).toEqual(["before-61", "before-31"]);
    expect(result.current.earlier.available).toBe(false);
  });

  test("a failed earlier page reports its error", async () => {
    server.use(
      http.get(apiUrl(`/v1/sessions/${IDS.session}`), () =>
        HttpResponse.json(sessionDetail(turns(31, 60), { earlierTurnsCursor: "c" })),
      ),
      http.get(apiUrl(`/v1/sessions/${IDS.session}/turns`), () =>
        problemResponse(503, "dependency_unavailable"),
      ),
    );
    const { result } = hook(IDS.session);
    await waitFor(() => {
      expect(result.current.earlier.available).toBe(true);
    });
    act(() => {
      result.current.earlier.load();
    });
    await waitFor(() => {
      expect(result.current.earlier.error?.code).toBe("dependency_unavailable");
    });
  });

  test("turns that leave getSession's window move into the earlier pages", async () => {
    let window = turns(11, 40);
    server.use(
      http.get(apiUrl(`/v1/sessions/${IDS.session}`), () =>
        HttpResponse.json(sessionDetail(window, { earlierTurnsCursor: "c" })),
      ),
      http.get(apiUrl(`/v1/sessions/${IDS.session}/turns`), () =>
        HttpResponse.json({ items: turns(1, 10), nextCursor: null }),
      ),
    );
    const { result, client } = hook(IDS.session);
    await waitFor(() => {
      expect(result.current.earlier.available).toBe(true);
    });
    act(() => {
      result.current.earlier.load();
    });
    await waitFor(() => {
      expect(result.current.stored).toHaveLength(40);
    });
    window = turns(12, 41);
    await act(async () => {
      await client.invalidateQueries({ queryKey: ["session", IDS.session] });
    });
    await waitFor(() => {
      expect(result.current.stored).toHaveLength(41);
    });
    expect(result.current.stored.map((turn) => turn.seq)).toEqual(
      turns(1, 41).map((turn) => turn.seq),
    );
  });

  test("keepDroppedTurns leaves data without pages or dropped turns alone", () => {
    expect(keepDroppedTurns(undefined, turns(1, 1))).toBeUndefined();
    const data = { pages: [{ items: turns(1, 2), nextCursor: null }], pageParams: ["c"] };
    expect(keepDroppedTurns(data, [])).toBe(data);
    expect(keepDroppedTurns({ pages: [], pageParams: [] }, turns(1, 1))).toEqual({
      pages: [],
      pageParams: [],
    });
    expect(keepDroppedTurns(data, turns(2, 3))?.pages[0]?.items.map((turn) => turn.seq)).toEqual([
      1, 2, 3,
    ]);
  });
});
