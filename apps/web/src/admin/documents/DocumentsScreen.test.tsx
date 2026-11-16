import { act, screen, waitFor, within } from "@testing-library/react";
import { HttpResponse, delay, http } from "msw";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

import type { DocumentSummary, IndexDetail } from "../../api/generated/types.gen";
import {
  documentSummary,
  indexDetail,
  indexOptions,
  page,
  rowVersion,
  uuid,
} from "../../test/adminData";
import { findToast, renderAdmin } from "../../test/adminRender";
import { apiUrl, problemResponse } from "../../test/handlers";
import { server } from "../../test/server";
import { formatDateTime } from "../../ui/format";
import { DocumentsScreen, documentsPoll, initialProject, rowAfterRemoval } from "./DocumentsScreen";

const INDEX = uuid(500);
const BASE = `/admin/indexes/${INDEX}/documents`;
const PROJECT_A = { projectId: uuid(1), name: "Alpha" };
const PROJECT_B = { projectId: uuid(2), name: "Beta" };
const ROUTES = [
  { path: "/admin/indexes", element: <p>indexes list</p> },
  { path: "/admin/indexes/:indexId", element: <p>index page</p> },
  { path: "/admin/indexes/:indexId/documents", element: <DocumentsScreen /> },
  { path: "/admin/projects", element: <p>projects list</p> },
  { path: "/admin/runs", element: <p>runs list</p> },
  { path: "/admin/runs/:runId", element: <p>run page</p> },
];

interface Sent {
  readonly method: string;
  readonly path: string;
  readonly ifMatch: string | null;
  readonly body: unknown;
}

interface ApiOptions {
  readonly index?: () => IndexDetail | Response;
  /** Answers each list read, given its URL. */
  readonly list?: (url: URL, call: number) => readonly DocumentSummary[] | Response;
  readonly write?: (sent: Sent) => Response | null;
}

function documentsApi(options: ApiOptions = {}) {
  const sent: Sent[] = [];
  const lists: URL[] = [];
  server.use(
    http.get(apiUrl(`/v1/admin/indexes/${INDEX}`), () => {
      const value = options.index?.() ?? indexDetail({ mappedProjects: [PROJECT_A] });
      return value instanceof Response ? value : HttpResponse.json(value);
    }),
    http.get(apiUrl("/v1/admin/index-options"), () => HttpResponse.json(indexOptions())),
    http.get(apiUrl(`/v1/admin/indexes/${INDEX}/documents`), ({ request }) => {
      const url = new URL(request.url);
      lists.push(url);
      const value = options.list?.(url, lists.length) ?? [documentSummary(1), documentSummary(2)];
      return value instanceof Response ? value : HttpResponse.json(page(value));
    }),
    http.post(
      apiUrl(`/v1/admin/indexes/${INDEX}/documents/:documentId/reingest`),
      async ({ request }) => record(request),
    ),
    http.delete(apiUrl(`/v1/admin/indexes/${INDEX}/documents/:documentId`), async ({ request }) =>
      record(request),
    ),
  );
  async function record(request: Request) {
    const text = await request.text();
    const entry: Sent = {
      method: request.method,
      path: new URL(request.url).pathname,
      ifMatch: request.headers.get("If-Match"),
      body: text === "" ? null : (JSON.parse(text) as unknown),
    };
    sent.push(entry);
    const scripted = options.write?.(entry);
    if (scripted !== undefined && scripted !== null) return scripted;
    if (entry.method === "DELETE") {
      return HttpResponse.json({ runId: uuid(460) }, { status: 202 });
    }
    return HttpResponse.json(
      {
        document: documentSummary(1, { rowVersion: rowVersion(299), displayState: "in_progress" }),
        runId: uuid(461),
        itemCount: 1,
      },
      { status: 202 },
    );
  }
  return { sent, lists };
}

function open(route = BASE) {
  return renderAdmin(ROUTES, route);
}

afterEach(() => {
  vi.useRealTimers();
});

describe("Documents (section 7.9)", () => {
  test("the breadcrumb, the heading, the one mapped project chosen, the status link and the table", async () => {
    documentsApi({
      list: () => [
        documentSummary(1),
        documentSummary(2, {
          latestVersion: null,
          currentVersion: null,
          displayState: "no_content",
        }),
      ],
    });
    open();
    expect(
      await screen.findByRole("heading", { level: 1, name: "Documents in Policy library" }),
    ).toBeInTheDocument();
    expect(document.title).toBe("Documents · Policy library · DocChat");
    expect(screen.getByRole("link", { name: "Indexes" })).toHaveAttribute("href", "/admin/indexes");
    expect(screen.getByRole("link", { name: "Policy library" })).toHaveAttribute(
      "href",
      `/admin/indexes/${INDEX}`,
    );
    expect(screen.getByRole("link", { name: "Ingestion status" })).toHaveAttribute(
      "href",
      `/admin/runs?indexId=${INDEX}`,
    );
    const picker = screen.getByRole("combobox", { name: /Upload through project/ });
    await waitFor(() => {
      expect(picker).toHaveTextContent("Alpha");
    });
    expect(
      screen.getByText(
        "Uploads are recorded against this project. Only projects that use this index are listed.",
      ),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Upload documents" })).toBeEnabled();
    const table = await screen.findByRole("table");
    const rows = within(table).getAllByRole("row");
    expect(
      within(rows[0] ?? table)
        .getAllByRole("columnheader")
        .map((cell) => cell.textContent),
    ).toEqual(["Name", "Version", "State", "Size", "Updated", ""]);
    const first = within(rows[1] ?? table).getAllByRole("cell");
    expect(first.map((cell) => cell.textContent).slice(0, 5)).toEqual([
      "document-1.pdf",
      "1",
      "Ingested",
      "0.5 MB",
      formatDateTime("2026-10-21T09:15:02.123Z"),
    ]);
    const second = within(rows[2] ?? table).getAllByRole("cell");
    expect(second.map((cell) => cell.textContent).slice(0, 4)).toEqual([
      "document-2.pdf",
      "Not set",
      "No content",
      "Not set",
    ]);
    expect(screen.queryByRole("switch", { name: "Auto-refresh" })).toBeNull();
  });

  test("without a mapped project: documents.project.none, Go to Projects and no upload", async () => {
    documentsApi({ index: () => indexDetail({ mappedProjects: [] }) });
    open();
    expect(
      await screen.findByText(
        /No project uses this index yet\. Map it to a project before uploading documents\./,
      ),
    ).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Go to Projects" })).toHaveAttribute(
      "href",
      "/admin/projects",
    );
    expect(screen.getByRole("button", { name: "Upload documents" })).toBeDisabled();
  });

  test("with several projects none is chosen until the admin picks one", async () => {
    documentsApi({ index: () => indexDetail({ mappedProjects: [PROJECT_A, PROJECT_B] }) });
    const { user } = open();
    const picker = await screen.findByRole("combobox", { name: /Upload through project/ });
    await waitFor(() => {
      expect(picker).toBeEnabled();
    });
    expect(screen.getByRole("button", { name: "Upload documents" })).toBeDisabled();
    await user.click(picker);
    await user.click(await screen.findByRole("option", { name: "Beta" }));
    expect(screen.getByRole("button", { name: "Upload documents" })).toBeEnabled();
    expect(picker).toHaveTextContent("Beta");
  });

  test("Loading: skeleton rows and the project picker disabled", async () => {
    server.use(
      http.get(apiUrl(`/v1/admin/indexes/${INDEX}`), async () => {
        await delay("infinite");
        return HttpResponse.json(indexDetail());
      }),
      http.get(apiUrl(`/v1/admin/indexes/${INDEX}/documents`), async () => {
        await delay("infinite");
        return HttpResponse.json(page([]));
      }),
    );
    open();
    expect(await screen.findByRole("combobox", { name: /Upload through project/ })).toBeDisabled();
    expect(document.title).toBe("Documents · DocChat");
    expect(screen.queryByRole("table")).toBeNull();
  });

  test("the filters reach the URL and the request; Empty and No matches", async () => {
    const api = documentsApi({
      list: (url) => (url.searchParams.has("q") || url.searchParams.has("state") ? [] : []),
    });
    const { user, router } = open(`${BASE}?state=bogus`);
    expect(
      await screen.findByText(
        "No documents yet. Upload PDF, Word or PowerPoint files to add them.",
      ),
    ).toBeInTheDocument();
    await waitFor(() => {
      expect(router.state.location.search).toBe("");
    });
    expect(api.lists[0]?.searchParams.get("limit")).toBe("50");
    await user.type(screen.getByRole("searchbox", { name: "Filter by name" }), "trav");
    await waitFor(() => {
      expect(router.state.location.search).toBe("?q=trav");
    });
    expect(await screen.findByText("No documents match these filters.")).toBeInTheDocument();
    expect(api.lists.at(-1)?.searchParams.get("q")).toBe("trav");
    await user.click(screen.getByRole("combobox", { name: "State" }));
    await user.click(await screen.findByRole("option", { name: "Failed" }));
    await waitFor(() => {
      expect(api.lists.at(-1)?.searchParams.get("state")).toBe("failed");
    });
    expect(router.state.location.search).toBe("?q=trav&state=failed");
  });

  test("a failed load shows the error bar with Retry; a missing index shows the way back", async () => {
    let fail = true;
    documentsApi({
      list: () => (fail ? problemResponse(500, "internal_error", { title: "Server error" }) : []),
    });
    const { user } = open();
    expect(await screen.findByText("Server error")).toBeInTheDocument();
    fail = false;
    await user.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByText(/No documents yet/)).toBeInTheDocument();
  });

  test("adminGetIndex 404: the problem's text with Back to Indexes", async () => {
    documentsApi({
      index: () => problemResponse(404, "index_not_found", { title: "Index not found" }),
    });
    open();
    expect(await screen.findByText("Index not found")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Back to Indexes" })).toHaveAttribute(
      "href",
      "/admin/indexes",
    );
  });

  test("another index failure shows its error bar above the screen", async () => {
    documentsApi({
      index: () => problemResponse(503, "dependency_unavailable", { title: "Busy" }),
    });
    open();
    expect(await screen.findByText("Busy")).toBeInTheDocument();
    expect(screen.getByRole("combobox", { name: /Upload through project/ })).toBeDisabled();
  });

  test("Show more appends the next page", async () => {
    const api = documentsApi({
      list: (url) =>
        url.searchParams.get("cursor") === "next"
          ? [documentSummary(3)]
          : HttpResponse.json(page([documentSummary(1)], "next")),
    });
    const { user } = open();
    await user.click(await screen.findByRole("button", { name: "Show more" }));
    expect(await screen.findByText("document-3.pdf")).toBeInTheDocument();
    expect(screen.getByText("document-1.pdf")).toBeInTheDocument();
    expect(api.lists.at(-1)?.searchParams.get("cursor")).toBe("next");
  });

  test("section 7.11: the list polls every 15 s while a document is in progress; Auto-refresh off stops it", async () => {
    expect(documentsPoll([documentSummary(1)])).toBe(false);
    expect(documentsPoll([documentSummary(1, { displayState: "in_progress" })])).toBe(true);
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const api = documentsApi({
      list: (_url, call) =>
        call === 3
          ? problemResponse(500, "internal_error")
          : [documentSummary(1, { displayState: "in_progress" })],
    });
    const { user } = open();
    expect(await screen.findByRole("switch", { name: "Auto-refresh" })).toBeChecked();
    expect(api.lists).toHaveLength(1);
    await act(() => vi.advanceTimersByTimeAsync(15_000));
    await waitFor(() => {
      expect(api.lists).toHaveLength(2);
    });
    await act(() => vi.advanceTimersByTimeAsync(15_000));
    expect(await screen.findByText(/^Couldn't refresh\. Showing results from/)).toBeInTheDocument();
    expect(screen.getByText("document-1.pdf")).toBeInTheDocument();
    await user.click(screen.getByRole("switch", { name: "Auto-refresh" }));
    const reads = api.lists.length;
    await act(() => vi.advanceTimersByTimeAsync(45_000));
    expect(api.lists).toHaveLength(reads);
  });

  test("helpers: the preselected project and the row after a removal", () => {
    expect(initialProject([PROJECT_A])).toBe(PROJECT_A.projectId);
    expect(initialProject([PROJECT_A, PROJECT_B])).toBeNull();
    expect(initialProject([])).toBeNull();
    expect(rowAfterRemoval(["a", "b", "c"], "b")).toBe("c");
    expect(rowAfterRemoval(["a", "b", "c"], "c")).toBe("b");
    expect(rowAfterRemoval(["a"], "a")).toBeNull();
    expect(rowAfterRemoval(["a"], "z")).toBeNull();
  });
});

describe("row actions (section 7.9)", () => {
  test("Re-ingest confirms, sends If-Match and toasts with a link to the run", async () => {
    const api = documentsApi();
    const { user } = open();
    const rows = await screen.findAllByRole("button", { name: "Re-ingest" });
    await user.click(rows[0] ?? document.body);
    const dialog = await screen.findByRole("dialog", { name: "document-1.pdf" });
    expect(
      within(dialog).getByText(/Each document is processed with the current settings\./),
    ).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "Re-ingest" }));
    const toast = await findToast("Re-ingest started for document-1.pdf.");
    expect(within(toast).getByRole("link", { name: "View progress" })).toHaveAttribute(
      "href",
      `/admin/runs/${uuid(461)}`,
    );
    expect(api.sent).toEqual([
      {
        method: "POST",
        path: `/v1/admin/indexes/${INDEX}/documents/${uuid(201)}/reingest`,
        ifMatch: `"${rowVersion(201)}"`,
        body: {},
      },
    ]);
  });

  test("Cancel sends nothing", async () => {
    const api = documentsApi();
    const { user } = open();
    await user.click(
      (await screen.findAllByRole("button", { name: "Delete" }))[0] ?? document.body,
    );
    const dialog = await screen.findByRole("dialog", { name: "Delete document-1.pdf?" });
    await user.click(within(dialog).getByRole("button", { name: "Cancel" }));
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).toBeNull();
    });
    expect(api.sent).toEqual([]);
  });

  test("Delete confirms, sends If-Match, removes the row, toasts and moves focus to the next name", async () => {
    let deleted = false;
    const api = documentsApi({
      list: () => (deleted ? [documentSummary(2)] : [documentSummary(1), documentSummary(2)]),
      write: (sent) => {
        if (sent.method === "DELETE") deleted = true;
        return null;
      },
    });
    const { user } = open();
    await user.click(
      (await screen.findAllByRole("button", { name: "Delete" }))[0] ?? document.body,
    );
    const dialog = await screen.findByRole("dialog", { name: "Delete document-1.pdf?" });
    expect(
      within(dialog).getByText(
        "It's removed from this index and from answers in every project that uses the index. You can't undo this.",
      ),
    ).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "Delete" }));
    expect(await findToast("document-1.pdf deleted.")).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.queryByText("document-1.pdf")).toBeNull();
    });
    expect(api.sent).toEqual([
      {
        method: "DELETE",
        path: `/v1/admin/indexes/${INDEX}/documents/${uuid(201)}`,
        ifMatch: `"${rowVersion(201)}"`,
        body: null,
      },
    ]);
    await waitFor(() => {
      expect(document.activeElement).toHaveTextContent("document-2.pdf");
    });
  });

  test("deleting the last row moves focus to the heading", async () => {
    let deleted = false;
    documentsApi({
      list: () => (deleted ? [] : [documentSummary(1)]),
      write: () => {
        deleted = true;
        return null;
      },
    });
    const { user } = open();
    await user.click(await screen.findByRole("button", { name: "Delete" }));
    await user.click(
      within(await screen.findByRole("dialog")).getByRole("button", { name: "Delete" }),
    );
    expect(await screen.findByText(/No documents yet/)).toBeInTheDocument();
    await waitFor(() => {
      expect(document.activeElement?.tagName).toBe("H1");
    });
  });

  test("412 reloads the list and shows admin.conflict.action", async () => {
    const api = documentsApi({
      write: () => problemResponse(412, "precondition_failed", { title: "Changed meanwhile" }),
    });
    const { user } = open();
    await user.click(
      (await screen.findAllByRole("button", { name: "Delete" }))[0] ?? document.body,
    );
    await user.click(
      within(await screen.findByRole("dialog")).getByRole("button", { name: "Delete" }),
    );
    expect(await screen.findByText("Changed meanwhile")).toBeInTheDocument();
    expect(
      screen.getByText(
        "The latest version is shown now. Check it, then repeat the action if you still need it.",
      ),
    ).toBeInTheDocument();
    await waitFor(() => {
      expect(api.lists.length).toBeGreaterThan(1);
    });
  });

  test("409 no_current_version shows the problem's text; 428 adds the defect line", async () => {
    let answer = 409;
    documentsApi({
      write: () =>
        answer === 409
          ? problemResponse(409, "no_current_version", { title: "Nothing to re-ingest" })
          : problemResponse(428, "precondition_required", { title: "Version missing" }),
    });
    const { user } = open();
    await user.click(
      (await screen.findAllByRole("button", { name: "Re-ingest" }))[0] ?? document.body,
    );
    await user.click(
      within(await screen.findByRole("dialog")).getByRole("button", { name: "Re-ingest" }),
    );
    expect(await screen.findByText("Nothing to re-ingest")).toBeInTheDocument();
    answer = 428;
    await user.click(
      (await screen.findAllByRole("button", { name: "Re-ingest" }))[0] ?? document.body,
    );
    await user.click(
      within(await screen.findByRole("dialog")).getByRole("button", { name: "Re-ingest" }),
    );
    expect(await screen.findByText("Version missing")).toBeInTheDocument();
    expect(
      screen.getByText(
        "Reload the page. If this keeps happening, send the reference ID to your DocChat administrator.",
      ),
    ).toBeInTheDocument();
  });

  test("429 write_rate_limited shows the retry time and disables the action until then", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    documentsApi({
      write: () =>
        problemResponse(429, "write_rate_limited", { title: "Slow down", retryAfterSeconds: 20 }),
    });
    const { user } = open();
    const reingest = async () =>
      (await screen.findAllByRole("button", { name: "Re-ingest" }))[0] ?? document.body;
    await user.click(await reingest());
    await user.click(
      within(await screen.findByRole("dialog")).getByRole("button", { name: "Re-ingest" }),
    );
    expect(await screen.findByText("Slow down")).toBeInTheDocument();
    expect(screen.getByText(/^You can retry at /)).toBeInTheDocument();
    expect(await reingest()).toBeDisabled();
    await act(() => vi.advanceTimersByTimeAsync(21_000));
    await waitFor(async () => {
      expect(await reingest()).toBeEnabled();
    });
  });
});

describe("the document panel (section 7.9)", () => {
  let create: ReturnType<typeof vi.fn<(blob: Blob) => string>>;

  beforeEach(() => {
    create = vi.fn(() => "blob:pdf-1");
    Object.defineProperty(URL, "createObjectURL", { configurable: true, value: create });
    Object.defineProperty(URL, "revokeObjectURL", { configurable: true, value: vi.fn() });
  });

  function panelApi(detail: () => Response | object, pdf?: () => Response) {
    let pdfCalls = 0;
    server.use(
      http.get(apiUrl(`/v1/admin/indexes/${INDEX}/documents/:documentId`), () => {
        const value = detail();
        return value instanceof Response ? value : HttpResponse.json(value);
      }),
      http.get(apiUrl(`/v1/admin/indexes/${INDEX}/documents/:documentId/pdf`), () => {
        pdfCalls += 1;
        return (
          pdf?.() ??
          new HttpResponse(new Uint8Array([37, 80, 68, 70]), {
            headers: { "Content-Type": "application/pdf", "Content-Length": "4" },
          })
        );
      }),
    );
    return { pdfCalls: () => pdfCalls };
  }

  test("the name opens the panel at ?document=; it lists the versions and closes back to the name", async () => {
    documentsApi();
    panelApi(() => ({
      ...documentSummary(1),
      versions: [
        {
          ...documentSummary(1).currentVersion,
          versionNumber: 2,
          versionId: uuid(312),
          uploadedByName: null,
          scanStatus: "awaiting",
          state: "failed",
          failureCode: "parse_failed",
          failureMessage: "The file couldn't be read.",
          uploadedViaProject: { projectId: uuid(2), name: "Beta" },
        },
        documentSummary(1).currentVersion,
      ],
      recentItems: [],
    }));
    const { user, router } = open();
    const name = await screen.findByRole("button", { name: "document-1.pdf" });
    await user.click(name);
    const panel = await screen.findByRole("dialog", { name: "document-1.pdf" });
    expect(router.state.location.search).toBe(`?document=${uuid(201)}`);
    const table = await within(panel).findByRole("table");
    const rows = within(table).getAllByRole("row");
    expect(
      within(rows[0] ?? table)
        .getAllByRole("columnheader")
        .map((cell) => cell.textContent),
    ).toEqual([
      "Version",
      "State",
      "Uploaded",
      "Uploaded by",
      "Through project",
      "Size",
      "Scan result",
      "Problem",
    ]);
    expect(
      within(rows[1] ?? table)
        .getAllByRole("cell")
        .map((cell) => cell.textContent),
    ).toEqual([
      "2",
      "Failed",
      formatDateTime("2026-10-21T09:15:02.123Z"),
      "Not set",
      "Beta",
      "0.5 MB",
      "Waiting for scan",
      "The file couldn't be read. (parse_failed)",
    ]);
    expect(within(rows[2] ?? table).getAllByRole("cell")[3]).toHaveTextContent("Grace Hopper");
    await user.click(within(panel).getByRole("button", { name: "Close" }));
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).toBeNull();
    });
    expect(router.state.location.search).toBe("");
    await waitFor(() => {
      expect(document.activeElement).toBe(screen.getByRole("button", { name: "document-1.pdf" }));
    });
  });

  test("T-388 Download PDF saves the current version's PDF under the document's name", async () => {
    documentsApi({ list: () => [documentSummary(1, { name: 'Travel: "policy".docx' })] });
    const api = panelApi(() => ({
      ...documentSummary(1, { name: 'Travel: "policy".docx' }),
      versions: [],
      recentItems: [],
    }));
    const click = vi
      .spyOn(HTMLAnchorElement.prototype, "click")
      .mockImplementation(() => undefined);
    const { user } = open(`${BASE}?document=${uuid(201)}`);
    const panel = await screen.findByRole("dialog", { name: 'Travel: "policy".docx' });
    await user.click(await within(panel).findByRole("button", { name: "Download PDF" }));
    await waitFor(() => {
      expect(click).toHaveBeenCalledTimes(1);
    });
    expect((click.mock.contexts[0] as HTMLAnchorElement).download).toBe("Travel_ _policy_.pdf");
    expect(create.mock.calls[0]?.[0].type).toBe("application/pdf");
    expect(api.pdfCalls()).toBe(1);
  });

  test("T-388 a too_large version shows documentPanel.pdfTooLarge; no current version shows neither", async () => {
    documentsApi();
    let detail: object = {
      ...documentSummary(1, {
        currentVersion: {
          ...documentSummary(1).currentVersion,
          documentPdfStatus: "too_large",
        } as never,
      }),
      versions: [],
      recentItems: [],
    };
    panelApi(() => detail);
    const { router } = open(`${BASE}?document=${uuid(201)}`);
    const panel = await screen.findByRole("dialog");
    expect(
      await within(panel).findByText(
        "This document is too large to download as one PDF. Its pages still open from chat citations.",
      ),
    ).toBeInTheDocument();
    expect(within(panel).queryByRole("button", { name: "Download PDF" })).toBeNull();
    detail = { ...documentSummary(2, { currentVersion: null }), versions: [], recentItems: [] };
    await act(() => router.navigate(`${BASE}?document=${uuid(202)}`));
    const next = await screen.findByRole("dialog", { name: "document-2.pdf" });
    await within(next).findByRole("table");
    expect(within(next).queryByRole("button", { name: "Download PDF" })).toBeNull();
    expect(within(next).queryByText(/too large to download/)).toBeNull();
  });

  test("409 document_pdf_unavailable shows the problem's text in a warning above the versions", async () => {
    documentsApi();
    panelApi(
      () => ({ ...documentSummary(1), versions: [], recentItems: [] }),
      () =>
        problemResponse(409, "document_pdf_unavailable", {
          title: "No PDF yet",
          detail: "The PDF isn't ready.",
        }),
    );
    const { user } = open(`${BASE}?document=${uuid(201)}`);
    const panel = await screen.findByRole("dialog");
    await user.click(await within(panel).findByRole("button", { name: "Download PDF" }));
    expect(await within(panel).findByText("No PDF yet")).toBeInTheDocument();
    expect(within(panel).getByText("The PDF isn't ready.")).toBeInTheDocument();
    expect(within(panel).queryByRole("button", { name: "Retry" })).toBeNull();
  });

  test("429 too_many_downloads shows the retry time and disables the button until then", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    documentsApi();
    const api = panelApi(
      () => ({ ...documentSummary(1), versions: [], recentItems: [] }),
      () =>
        problemResponse(429, "too_many_downloads", {
          title: "Too many downloads",
          retryAfterSeconds: 10,
        }),
    );
    const { user } = open(`${BASE}?document=${uuid(201)}`);
    const panel = await screen.findByRole("dialog");
    const button = await within(panel).findByRole("button", { name: "Download PDF" });
    await user.click(button);
    expect(await within(panel).findByText("Too many downloads")).toBeInTheDocument();
    expect(within(panel).getByText(/^You can retry at /)).toBeInTheDocument();
    expect(button).toBeDisabled();
    expect(within(panel).getByRole("button", { name: "Retry" })).toBeDisabled();
    expect(api.pdfCalls()).toBe(1);
    await act(() => vi.advanceTimersByTimeAsync(11_000));
    await waitFor(() => {
      expect(button).toBeEnabled();
    });
  });

  test("a cut-short body shows citation.downloadFailed with Retry and the reference ID", async () => {
    documentsApi();
    let calls = 0;
    panelApi(
      () => ({ ...documentSummary(1), versions: [], recentItems: [] }),
      () => {
        calls += 1;
        return calls === 1
          ? new HttpResponse(
              new ReadableStream({
                start(controller) {
                  controller.error(new Error("reset"));
                },
              }),
              { headers: { "Content-Type": "application/pdf" } },
            )
          : new HttpResponse(new Uint8Array([37, 80, 68, 70]), {
              headers: { "Content-Type": "application/pdf", "Content-Length": "4" },
            });
      },
    );
    const click = vi
      .spyOn(HTMLAnchorElement.prototype, "click")
      .mockImplementation(() => undefined);
    const { user } = open(`${BASE}?document=${uuid(201)}`);
    const panel = await screen.findByRole("dialog");
    await user.click(await within(panel).findByRole("button", { name: "Download PDF" }));
    expect(
      await within(panel).findByText("The download didn't finish. Select Retry."),
    ).toBeInTheDocument();
    expect(within(panel).getByText(/Reference ID: [0-9a-f]{32}/)).toBeInTheDocument();
    expect(click).not.toHaveBeenCalled();
    await user.click(within(panel).getByRole("button", { name: "Retry" }));
    await waitFor(() => {
      expect(click).toHaveBeenCalledTimes(1);
    });
  });

  test("a 404 shows the problem's text without Retry; another problem offers Retry", async () => {
    documentsApi();
    let status = 404;
    panelApi(
      () => ({ ...documentSummary(1), versions: [], recentItems: [] }),
      () =>
        status === 404
          ? problemResponse(404, "document_not_found", { title: "Document not found" })
          : problemResponse(503, "dependency_unavailable", { title: "Try later" }),
    );
    const { user } = open(`${BASE}?document=${uuid(201)}`);
    const panel = await screen.findByRole("dialog");
    await user.click(await within(panel).findByRole("button", { name: "Download PDF" }));
    expect(await within(panel).findByText("Document not found")).toBeInTheDocument();
    expect(within(panel).queryByRole("button", { name: "Retry" })).toBeNull();
    status = 503;
    await user.click(within(panel).getByRole("button", { name: "Download PDF" }));
    expect(await within(panel).findByText("Try later")).toBeInTheDocument();
    expect(within(panel).getByRole("button", { name: "Retry" })).toBeEnabled();
  });

  test("the document doesn't load: 404 shows the problem's text, another failure Retry", async () => {
    documentsApi();
    let status = 404;
    panelApi(() =>
      status === 404
        ? problemResponse(404, "document_not_found", { title: "Document not found" })
        : status === 500
          ? problemResponse(500, "internal_error", { title: "Server error" })
          : { ...documentSummary(1), versions: [], recentItems: [] },
    );
    const { router, user } = open(`${BASE}?document=${uuid(201)}`);
    expect(await screen.findByText("Document not found")).toBeInTheDocument();
    status = 500;
    await act(() => router.navigate(`${BASE}?document=${uuid(202)}`));
    expect(await screen.findByText("Server error")).toBeInTheDocument();
    status = 200;
    await user.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByRole("button", { name: "Download PDF" })).toBeInTheDocument();
  });

  test("a ?document= that isn't a UUID is dropped", async () => {
    documentsApi();
    const { router } = open(`${BASE}?document=not-an-id`);
    await screen.findByRole("table");
    await waitFor(() => {
      expect(router.state.location.search).toBe("");
    });
    expect(screen.queryByRole("dialog")).toBeNull();
  });
});
