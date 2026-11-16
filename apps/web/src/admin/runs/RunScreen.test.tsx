import { act, screen, waitFor, within } from "@testing-library/react";
import { HttpResponse, http } from "msw";
import { afterEach, describe, expect, test, vi } from "vitest";

import type { IngestionRunDetail } from "../../api/generated/types.gen";
import { runItem, runSummary, uuid } from "../../test/adminData";
import { renderAdmin } from "../../test/adminRender";
import { apiUrl, problemResponse } from "../../test/handlers";
import { server } from "../../test/server";
import { formatDateTime } from "../../ui/format";
import { RunScreen } from "./RunScreen";

const RUN = uuid(401);
const ROUTES = [
  { path: "/admin/runs", element: <p>runs list</p> },
  { path: "/admin/runs/:runId", element: <RunScreen /> },
];

function detail(overrides: Partial<IngestionRunDetail> = {}): IngestionRunDetail {
  return { run: runSummary(1), items: [runItem(1)], nextItemsCursor: null, ...overrides };
}

function runApi(answer: (url: URL, call: number) => IngestionRunDetail | Response) {
  const reads: URL[] = [];
  server.use(
    http.get(apiUrl(`/v1/admin/ingestion-runs/${RUN}`), ({ request }) => {
      const url = new URL(request.url);
      reads.push(url);
      const value = answer(url, reads.length);
      return value instanceof Response ? value : HttpResponse.json(value);
    }),
  );
  return reads;
}

function open() {
  return renderAdmin(ROUTES, `/admin/runs/${RUN}`);
}

afterEach(() => {
  vi.useRealTimers();
});

describe("Ingestion run (section 7.10)", () => {
  test("the breadcrumb, the fields, the progress and the items", async () => {
    const reads = runApi(() =>
      detail({
        run: runSummary(1, {
          counts: {
            total: 4,
            queued: 0,
            running: 0,
            succeeded: 2,
            skipped: 1,
            failed: 1,
            cancelled: 0,
          },
          state: "completed_with_errors",
        }),
        items: [
          runItem(1),
          runItem(2, {
            state: "failed",
            stage: "extract",
            attempts: 3,
            outcomeCode: "parse_failed",
            outcomeMessage: "The file couldn't be read.",
          }),
          runItem(3, {
            document: null,
            operation: "create_physical_index",
            versionNumber: null,
            stage: null,
            state: "skipped",
          }),
        ],
      }),
    );
    open();
    expect(
      await screen.findByRole("heading", { level: 1, name: "Ingestion run" }),
    ).toBeInTheDocument();
    expect(document.title).toBe("Ingestion run · DocChat");
    expect(reads[0]?.searchParams.get("itemsLimit")).toBe("100");
    expect(screen.getByRole("link", { name: "Ingestion runs" })).toHaveAttribute(
      "href",
      "/admin/runs",
    );
    expect(
      await screen.findByText(formatDateTime("2026-10-21T09:15:02.123Z"), {
        selector: "button",
      }),
    ).toBeInTheDocument();
    const fields = document.querySelector("dl");
    expect(fields).toHaveTextContent("StateCompleted with errors");
    expect(fields).toHaveTextContent("TypeUpload");
    expect(fields).toHaveTextContent("IndexPolicy library");
    expect(fields).toHaveTextContent("Started byGrace Hopper");
    expect(fields).toHaveTextContent(`Started${formatDateTime("2026-10-21T09:15:02.123Z")}`);
    expect(fields).toHaveTextContent(`Finished${formatDateTime("2026-10-22T10:00:00.000Z")}`);
    expect(screen.getByRole("progressbar")).toHaveAttribute("aria-valuenow", "4");
    expect(screen.getByText("4 of 4 documents")).toBeInTheDocument();
    expect(screen.getByText("1 failed")).toBeInTheDocument();
    expect(
      screen.getByRole("heading", { level: 2, name: "Documents in this run" }),
    ).toBeInTheDocument();
    const table = screen.getByRole("table");
    const rows = within(table).getAllByRole("row");
    expect(
      within(rows[0] ?? table)
        .getAllByRole("columnheader")
        .map((cell) => cell.textContent),
    ).toEqual(["Document", "Version", "Stage", "State", "Attempts", "Problem"]);
    expect(
      within(rows[2] ?? table)
        .getAllByRole("cell")
        .map((cell) => cell.textContent),
    ).toEqual([
      "document-2.pdf",
      "1",
      "Extract",
      "Failed",
      "3",
      "The file couldn't be read. (parse_failed)",
    ]);
    expect(
      within(rows[3] ?? table)
        .getAllByRole("cell")
        .map((cell) => cell.textContent),
    ).toEqual(["Create search index", "", "", "Skipped", "1", ""]);
    expect(screen.queryByRole("switch", { name: "Auto-refresh" })).toBeNull();
  });

  test("an unfinished run reads Not finished, and Show more passes itemsCursor", async () => {
    const reads = runApi((url) =>
      url.searchParams.get("itemsCursor") === "more"
        ? detail({ run: runSummary(1, { state: "completed" }), items: [runItem(9)] })
        : detail({
            run: runSummary(1, { state: "failed", completedAt: null }),
            nextItemsCursor: "more",
          }),
    );
    const { user } = open();
    expect(await screen.findByText("Not finished")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Show more" }));
    expect(await screen.findByText("document-9.pdf")).toBeInTheDocument();
    expect(screen.getByText("document-1.pdf")).toBeInTheDocument();
    expect(reads.at(-1)?.searchParams.get("itemsCursor")).toBe("more");
    expect(reads.at(-1)?.searchParams.get("itemsLimit")).toBe("100");
  });

  test("a run 404 shows the problem's text with Back to Ingestion runs; another failure Retry", async () => {
    let status = 404;
    runApi(() =>
      status === 404
        ? problemResponse(404, "run_not_found", { title: "Run not found" })
        : status === 500
          ? problemResponse(500, "internal_error", { title: "Server error" })
          : detail(),
    );
    const first = open();
    expect(await screen.findByText("Run not found")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Back to Ingestion runs" })).toHaveAttribute(
      "href",
      "/admin/runs",
    );
    first.result.unmount();
    status = 500;
    const { user } = open();
    expect(await screen.findByText("Server error")).toBeInTheDocument();
    status = 200;
    await user.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByText("document-1.pdf")).toBeInTheDocument();
  });

  test("T-402 the run page polls every 5,000 ms while running and stops when it finishes (fake timers)", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const reads = runApi((_url, call) =>
      detail({
        run: runSummary(1, {
          state: call < 3 ? "running" : "completed",
          completedAt: call < 3 ? null : "2026-10-22T10:00:00.000Z",
        }),
      }),
    );
    open();
    expect(await screen.findByRole("switch", { name: "Auto-refresh" })).toBeChecked();
    expect(reads).toHaveLength(1);
    await act(() => vi.advanceTimersByTimeAsync(4_900));
    expect(reads).toHaveLength(1);
    await act(() => vi.advanceTimersByTimeAsync(100));
    await waitFor(() => {
      expect(reads).toHaveLength(2);
    });
    await act(() => vi.advanceTimersByTimeAsync(5_000));
    await waitFor(() => {
      expect(reads).toHaveLength(3);
    });
    await waitFor(() => {
      expect(screen.getByRole("status")).toHaveTextContent("Run finished: Completed.");
    });
    expect(screen.queryByRole("switch", { name: "Auto-refresh" })).toBeNull();
    await act(() => vi.advanceTimersByTimeAsync(20_000));
    expect(reads).toHaveLength(3);
  });

  test("T-402 Auto-refresh off stops polling", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const reads = runApi(() =>
      detail({ run: runSummary(1, { state: "queued", completedAt: null }) }),
    );
    const { user } = open();
    await user.click(await screen.findByRole("switch", { name: "Auto-refresh" }));
    expect(screen.getByRole("switch", { name: "Auto-refresh" })).not.toBeChecked();
    await act(() => vi.advanceTimersByTimeAsync(20_000));
    expect(reads).toHaveLength(1);
    await user.click(screen.getByRole("switch", { name: "Auto-refresh" }));
    await act(() => vi.advanceTimersByTimeAsync(5_000));
    await waitFor(() => {
      expect(reads).toHaveLength(2);
    });
  });

  test("T-402 3 failed polls in a row stop polling and show Refresh, which restarts it", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const reads = runApi((_url, call) =>
      call >= 2 && call <= 4
        ? problemResponse(503, "dependency_unavailable")
        : detail({ run: runSummary(1, { state: "running", completedAt: null }) }),
    );
    const { user } = open();
    await screen.findByRole("switch", { name: "Auto-refresh" });
    await act(() => vi.advanceTimersByTimeAsync(5_000));
    expect(await screen.findByText(/^Couldn't refresh\. Showing results from/)).toBeInTheDocument();
    expect(screen.getByText("document-1.pdf")).toBeInTheDocument();
    await act(() => vi.advanceTimersByTimeAsync(5_000));
    await act(() => vi.advanceTimersByTimeAsync(5_000));
    expect(
      await screen.findByText("Automatic refresh stopped because DocChat couldn't be reached."),
    ).toBeInTheDocument();
    expect(reads).toHaveLength(4);
    await act(() => vi.advanceTimersByTimeAsync(20_000));
    expect(reads).toHaveLength(4);
    await user.click(screen.getByRole("button", { name: "Refresh" }));
    await waitFor(() => {
      expect(reads).toHaveLength(5);
    });
    await waitFor(() => {
      expect(screen.queryByText(/Automatic refresh stopped/)).toBeNull();
    });
    await act(() => vi.advanceTimersByTimeAsync(5_000));
    await waitFor(() => {
      expect(reads).toHaveLength(6);
    });
  });
});
