import { act, screen, waitFor, within } from "@testing-library/react";
import { HttpResponse, http } from "msw";
import { afterEach, describe, expect, test, vi } from "vitest";

import type { RunSummary } from "../../api/generated/types.gen";
import { indexSummary, page, runSummary, uuid } from "../../test/adminData";
import { renderAdmin } from "../../test/adminRender";
import { apiUrl, problemResponse } from "../../test/handlers";
import { server } from "../../test/server";
import { formatDateTime } from "../../ui/format";
import { RunsScreen, runsPoll } from "./RunsScreen";

const ROUTES = [
  { path: "/admin/runs", element: <RunsScreen /> },
  { path: "/admin/runs/:runId", element: <p>run page</p> },
];

function runsApi(
  list: (url: URL, call: number) => readonly RunSummary[] | Response = () => [runSummary(1)],
  indexes: () => Response = () =>
    HttpResponse.json(page([indexSummary(0, { displayName: "Policy library" }), indexSummary(1)])),
) {
  const reads: URL[] = [];
  server.use(
    http.get(apiUrl("/v1/admin/ingestion-runs"), ({ request }) => {
      const url = new URL(request.url);
      reads.push(url);
      const value = list(url, reads.length);
      return value instanceof Response ? value : HttpResponse.json(page(value));
    }),
    http.get(apiUrl("/v1/admin/indexes"), () => indexes()),
  );
  return reads;
}

afterEach(() => {
  vi.useRealTimers();
});

describe("Ingestion runs (section 7.10)", () => {
  test("the heading, the filters and the table in the API's order", async () => {
    runsApi(() => [
      runSummary(1, {
        state: "completed_with_errors",
        counts: {
          total: 5,
          queued: 0,
          running: 0,
          succeeded: 3,
          skipped: 0,
          failed: 1,
          cancelled: 1,
        },
      }),
      runSummary(2, {
        kind: "maintenance",
        requestedBy: null,
        requestedByName: null,
        state: "completed",
        createdAt: "2026-10-20T08:00:00.000Z",
      }),
    ]);
    renderAdmin(ROUTES, "/admin/runs");
    expect(
      await screen.findByRole("heading", { level: 1, name: "Ingestion runs" }),
    ).toBeInTheDocument();
    expect(document.title).toBe("Ingestion runs · DocChat");
    expect(screen.getByRole("combobox", { name: "Index" })).toHaveTextContent("All indexes");
    expect(screen.getByRole("combobox", { name: "State" })).toHaveTextContent("All states");
    const table = await screen.findByRole("table");
    const rows = within(table).getAllByRole("row");
    expect(
      within(rows[0] ?? table)
        .getAllByRole("columnheader")
        .map((cell) => cell.textContent),
    ).toEqual(["Started", "Type", "Index", "Started by", "Progress", "State"]);
    expect(
      within(rows[1] ?? table)
        .getAllByRole("cell")
        .map((cell) => cell.textContent),
    ).toEqual([
      formatDateTime("2026-10-21T09:15:02.123Z"),
      "Upload",
      "Policy library",
      "Grace Hopper",
      "5 of 5 documents 1 failed",
      "Completed with errors",
    ]);
    expect(within(rows[1] ?? table).getByRole("link")).toHaveAttribute(
      "href",
      `/admin/runs/${uuid(401)}`,
    );
    expect(
      within(rows[2] ?? table)
        .getAllByRole("cell")
        .map((cell) => cell.textContent),
    ).toEqual([
      formatDateTime("2026-10-20T08:00:00.000Z"),
      "Maintenance",
      "Policy library",
      "System",
      "3 of 3 documents",
      "Completed",
    ]);
    expect(screen.queryByRole("switch", { name: "Auto-refresh" })).toBeNull();
  });

  test("the filters reach the URL and the request; Empty and No matches", async () => {
    const reads = runsApi(() => []);
    const { user, router } = renderAdmin(ROUTES, "/admin/runs");
    expect(
      await screen.findByText(
        "No ingestion runs yet. Runs appear here when you upload, re-ingest or delete documents.",
      ),
    ).toBeInTheDocument();
    expect(reads[0]?.searchParams.get("limit")).toBe("50");
    expect(reads[0]?.searchParams.has("indexId")).toBe(false);
    await user.click(screen.getByRole("combobox", { name: "Index" }));
    await user.click(await screen.findByRole("option", { name: "Index 1" }));
    await waitFor(() => {
      expect(router.state.location.search).toBe(`?indexId=${uuid(501)}`);
    });
    expect(await screen.findByText("No runs match these filters.")).toBeInTheDocument();
    expect(reads.at(-1)?.searchParams.get("indexId")).toBe(uuid(501));
    expect(screen.getByRole("combobox", { name: "Index" })).toHaveTextContent("Index 1");
    await user.click(screen.getByRole("combobox", { name: "State" }));
    await user.click(await screen.findByRole("option", { name: "Running" }));
    await waitFor(() => {
      expect(reads.at(-1)?.searchParams.get("state")).toBe("running");
    });
    await user.click(screen.getByRole("combobox", { name: "State" }));
    await user.click(await screen.findByRole("option", { name: "All states" }));
    await user.click(screen.getByRole("combobox", { name: "Index" }));
    await user.click(await screen.findByRole("option", { name: "All indexes" }));
    await waitFor(() => {
      expect(router.state.location.search).toBe("");
    });
  });

  test("a filter from a link: an unknown index shows no name; invalid values are dropped", async () => {
    const reads = runsApi(() => []);
    const { router } = renderAdmin(ROUTES, `/admin/runs?indexId=${uuid(999)}&state=finished`);
    await screen.findByText("No runs match these filters.");
    await waitFor(() => {
      expect(router.state.location.search).toBe(`?indexId=${uuid(999)}`);
    });
    expect(reads.at(-1)?.searchParams.get("indexId")).toBe(uuid(999));
    expect(screen.getByRole("combobox", { name: "Index" })).toHaveTextContent("");
    const bad = renderAdmin(ROUTES, "/admin/runs?indexId=nope");
    await waitFor(() => {
      expect(bad.router.state.location.search).toBe("");
    });
  });

  test("the index list failing still offers All indexes", async () => {
    runsApi(
      () => [runSummary(1)],
      () => problemResponse(500, "internal_error"),
    );
    const { user } = renderAdmin(ROUTES, "/admin/runs");
    await screen.findByRole("table");
    await user.click(screen.getByRole("combobox", { name: "Index" }));
    expect(await screen.findAllByRole("option")).toHaveLength(1);
  });

  test("a failed load shows the error bar with Retry; Show more appends the next page", async () => {
    let fail = true;
    const reads = runsApi((url) =>
      fail
        ? problemResponse(500, "internal_error", { title: "Server error" })
        : url.searchParams.get("cursor") === "next"
          ? [runSummary(2, { kind: "index_rebuild" })]
          : HttpResponse.json(page([runSummary(1)], "next")),
    );
    const { user } = renderAdmin(ROUTES, "/admin/runs");
    expect(await screen.findByText("Server error")).toBeInTheDocument();
    fail = false;
    await user.click(screen.getByRole("button", { name: "Retry" }));
    await user.click(await screen.findByRole("button", { name: "Show more" }));
    expect(await screen.findByText("Rebuild index")).toBeInTheDocument();
    expect(reads.at(-1)?.searchParams.get("cursor")).toBe("next");
  });

  test("section 7.11: the list polls every 15 s while a shown run is open and announces it finishing", async () => {
    expect(runsPoll([runSummary(1)])).toBe(false);
    expect(runsPoll([runSummary(1), runSummary(2, { state: "queued" })])).toBe(true);
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const reads = runsApi((_url, call) => [
      runSummary(1, { state: call === 1 ? "running" : "completed" }),
    ]);
    renderAdmin(ROUTES, "/admin/runs");
    expect(await screen.findByRole("switch", { name: "Auto-refresh" })).toBeChecked();
    await act(() => vi.advanceTimersByTimeAsync(14_000));
    expect(reads).toHaveLength(1);
    await act(() => vi.advanceTimersByTimeAsync(1_000));
    await waitFor(() => {
      expect(reads).toHaveLength(2);
    });
    await waitFor(() => {
      expect(screen.getByRole("status")).toHaveTextContent("Run finished: Completed.");
    });
    expect(screen.queryByRole("switch", { name: "Auto-refresh" })).toBeNull();
    await act(() => vi.advanceTimersByTimeAsync(30_000));
    expect(reads).toHaveLength(2);
  });
});
