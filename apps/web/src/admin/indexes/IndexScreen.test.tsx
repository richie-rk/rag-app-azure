import { act, screen, waitFor, within } from "@testing-library/react";
import { HttpResponse, http } from "msw";
import { describe, expect, test, vi } from "vitest";

import type { IndexDetail } from "../shared/queries";
import { indexDetail, indexOptions, rowVersion, uuid } from "../../test/adminData";
import { findToast, renderAdmin } from "../../test/adminRender";
import { apiUrl, problemResponse } from "../../test/handlers";
import { server } from "../../test/server";
import { IndexScreen, canDeleteIndex, documentCountsText, indexPolls } from "./IndexScreen";

const INDEX = uuid(500);
const ROUTES = [
  { path: "/admin/indexes", element: <p>indexes list</p> },
  { path: "/admin/indexes/:indexId", element: <IndexScreen /> },
  { path: "/admin/runs/:runId", element: <p>run page</p> },
];

interface Sent {
  readonly method: string;
  readonly path: string;
  readonly ifMatch: string | null;
  readonly body: unknown;
}

function indexApi(
  read: () => IndexDetail | Response,
  write: (sent: Sent) => Response = () => new HttpResponse(null, { status: 500 }),
) {
  const sent: Sent[] = [];
  let reads = 0;
  const record = async (request: Request) => {
    const text = await request.text();
    const entry: Sent = {
      method: request.method,
      path: new URL(request.url).pathname,
      ifMatch: request.headers.get("If-Match"),
      body: text === "" ? null : (JSON.parse(text) as unknown),
    };
    sent.push(entry);
    return write(entry);
  };
  server.use(
    http.get(apiUrl(`/v1/admin/indexes/${INDEX}`), () => {
      reads += 1;
      const value = read();
      return value instanceof Response ? value : HttpResponse.json(value);
    }),
    http.get(apiUrl("/v1/admin/index-options"), () => HttpResponse.json(indexOptions())),
    http.patch(apiUrl(`/v1/admin/indexes/${INDEX}`), ({ request }) => record(request)),
    http.delete(apiUrl(`/v1/admin/indexes/${INDEX}`), ({ request }) => record(request)),
    http.post(apiUrl(`/v1/admin/indexes/${INDEX}/reingest`), ({ request }) => record(request)),
  );
  return { sent, reads: () => reads };
}

function open() {
  return renderAdmin(ROUTES, `/admin/indexes/${INDEX}`);
}

describe("Index", () => {
  test("shows the breadcrumb, the name with its status, the summary and the links", async () => {
    indexApi(() => indexDetail());
    open();
    expect(
      await screen.findByRole("heading", { level: 1, name: /Policy library/ }),
    ).toBeInTheDocument();
    expect(document.title).toBe("Policy library · Indexes · DocChat");
    expect(screen.getByRole("link", { name: "Indexes" })).toHaveAttribute("href", "/admin/indexes");
    const summary = document.querySelector("dl");
    expect(summary).not.toBeNull();
    await waitFor(() => {
      expect(summary).toHaveTextContent("Passage size (tokens): 512");
    });
    expect(summary).toHaveTextContent("MethodBy headings and paragraphs");
    expect(summary).toHaveTextContent("Embedding modeltext-embedding-3-large, 1024 dimensions");
    expect(summary).toHaveTextContent("LanguageAny language");
    expect(summary).toHaveTextContent("Documents12 (Ingested: 10, In progress: 1, Failed: 1)");
    expect(screen.getByRole("link", { name: "Documents" })).toHaveAttribute(
      "href",
      `/admin/indexes/${INDEX}/documents`,
    );
    expect(screen.getByRole("link", { name: "Ingestion runs" })).toHaveAttribute(
      "href",
      `/admin/runs?indexId=${INDEX}`,
    );
    expect(screen.getByRole("button", { name: "Delete index" })).toBeInTheDocument();
    expect(screen.queryByRole("switch", { name: "Auto-refresh" })).toBeNull();
    expect(screen.queryByText(/This index's settings changed/)).toBeNull();
  });

  test("a 404 shows the problem's text with a link back to Indexes", async () => {
    indexApi(() => problemResponse(404, "index_not_found", { title: "Index not found" }));
    open();
    expect(await screen.findByText("Index not found")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Back to Indexes" })).toBeInTheDocument();
  });

  test("Delete index is offered only when no project maps it and its status allows", () => {
    expect(canDeleteIndex(indexDetail())).toBe(true);
    expect(canDeleteIndex(indexDetail({ status: "failed" }))).toBe(true);
    expect(canDeleteIndex(indexDetail({ status: "reingesting" }))).toBe(false);
    expect(
      canDeleteIndex(
        indexDetail({ mappedProjects: [{ projectId: uuid(1), name: "Field notes" }] }),
      ),
    ).toBe(false);
    expect(documentCountsText({ live: 1, ingested: 1, inProgress: 0, failed: 0 })).toBe(
      "1 (Ingested: 1, In progress: 0, Failed: 0)",
    );
  });

  test("section 7.11: the page polls every 15 s while a run is open or the index works", async () => {
    expect(indexPolls(undefined)).toBe(false);
    expect(indexPolls(indexDetail())).toBe(false);
    expect(indexPolls(indexDetail({ status: "provisioning" }))).toBe(true);
    expect(
      indexPolls(indexDetail({ openRun: { runId: uuid(9), kind: "upload", state: "running" } })),
    ).toBe(true);
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      let status: IndexDetail["status"] = "rebuilding";
      const api = indexApi(() =>
        indexDetail({
          status,
          rebuildProgress: status === "rebuilding" ? { servedCurrent: 3, liveDocuments: 12 } : null,
        }),
      );
      const { user } = open();
      expect(await screen.findByText("Rebuilding: 3 of 12 documents ready")).toBeInTheDocument();
      expect(screen.getByRole("progressbar")).toHaveAttribute("aria-valuenow", "3");
      expect(screen.getByRole("switch", { name: "Auto-refresh" })).toBeChecked();
      expect(api.reads()).toBe(1);
      await act(() => vi.advanceTimersByTimeAsync(15_000));
      await waitFor(() => {
        expect(api.reads()).toBe(2);
      });
      await user.click(screen.getByRole("switch", { name: "Auto-refresh" }));
      await act(() => vi.advanceTimersByTimeAsync(30_000));
      expect(api.reads()).toBe(2);
      await user.click(screen.getByRole("switch", { name: "Auto-refresh" }));
      status = "ready";
      await act(() => vi.advanceTimersByTimeAsync(15_000));
      await waitFor(() => {
        expect(screen.queryByRole("switch", { name: "Auto-refresh" })).toBeNull();
      });
      const settled = api.reads();
      await act(() => vi.advanceTimersByTimeAsync(30_000));
      expect(api.reads()).toBe(settled);
    } finally {
      vi.useRealTimers();
    }
  });

  test("Start re-ingest confirms, sends {} with If-Match, and toasts with View progress", async () => {
    const api = indexApi(
      () => indexDetail({ status: "reingest_required", reingestRequired: true }),
      () =>
        HttpResponse.json(
          {
            index: indexDetail({ status: "reingesting" }),
            runId: uuid(77),
            mode: "same_index",
            itemCount: 12,
          },
          { status: 202 },
        ),
    );
    const { user } = open();
    expect(await screen.findAllByText("Re-ingest needed")).toHaveLength(3);
    expect(screen.getByText(/This index's settings changed/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Start re-ingest" }));
    const dialog = await screen.findByRole("dialog", {
      name: "Re-ingest every document in Policy library?",
    });
    await user.click(within(dialog).getByRole("button", { name: "Start re-ingest" }));
    const toast = await findToast("Re-ingest started.");
    expect(within(toast).getByRole("link", { name: "View progress" })).toHaveAttribute(
      "href",
      `/admin/runs/${uuid(77)}`,
    );
    expect(api.sent).toEqual([
      {
        method: "POST",
        path: `/v1/admin/indexes/${INDEX}/reingest`,
        ifMatch: `"${rowVersion(50)}"`,
        body: {},
      },
    ]);
  });

  test("a 412 on re-ingest refetches the index and shows the problem's text with conflict.action", async () => {
    const api = indexApi(
      () => indexDetail({ status: "reingest_required", reingestRequired: true }),
      () => problemResponse(412, "precondition_failed", { title: "Changed by someone else" }),
    );
    const { user } = open();
    await user.click(await screen.findByRole("button", { name: "Start re-ingest" }));
    await user.click(
      within(await screen.findByRole("dialog")).getByRole("button", { name: "Start re-ingest" }),
    );
    expect(await screen.findByText("Changed by someone else")).toBeInTheDocument();
    expect(
      screen.getByText(
        "The latest version is shown now. Check it, then repeat the action if you still need it.",
      ),
    ).toBeInTheDocument();
    await waitFor(() => {
      expect(api.reads()).toBe(2);
    });
  });

  test("409 index_busy on re-ingest shows the problem's text next to the action", async () => {
    indexApi(
      () => indexDetail({ status: "reingest_required", reingestRequired: true }),
      () => problemResponse(409, "index_busy", { title: "Index busy" }),
    );
    const { user } = open();
    await user.click(await screen.findByRole("button", { name: "Start re-ingest" }));
    await user.click(
      within(await screen.findByRole("dialog")).getByRole("button", { name: "Start re-ingest" }),
    );
    expect(await screen.findByText("Index busy")).toBeInTheDocument();
  });

  test("Delete index confirms, sends If-Match and opens Indexes with a toast", async () => {
    const api = indexApi(
      () => indexDetail(),
      () =>
        HttpResponse.json(
          { index: indexDetail({ status: "deleting" }), runId: uuid(78) },
          { status: 202 },
        ),
    );
    const { user, router } = open();
    await user.click(await screen.findByRole("button", { name: "Delete index" }));
    const dialog = await screen.findByRole("dialog", { name: "Delete Policy library?" });
    await user.click(within(dialog).getByRole("button", { name: "Delete" }));
    await waitFor(() => {
      expect(router.state.location.pathname).toBe("/admin/indexes");
    });
    expect(await findToast("Policy library is being deleted.")).toBeInTheDocument();
    expect(api.sent[0]).toMatchObject({ method: "DELETE", ifMatch: `"${rowVersion(50)}"` });
  });

  test("a refused delete shows the problem's text next to the action", async () => {
    indexApi(
      () => indexDetail(),
      () => problemResponse(409, "index_mapped", { title: "Index used by projects" }),
    );
    const { user } = open();
    await user.click(await screen.findByRole("button", { name: "Delete index" }));
    await user.click(
      within(await screen.findByRole("dialog")).getByRole("button", { name: "Delete" }),
    );
    expect(await screen.findByText("Index used by projects")).toBeInTheDocument();
  });

  test("Edit settings: a setting change shows the warning; Save sends the changed fields and toasts", async () => {
    const api = indexApi(
      () => indexDetail(),
      ({ body }) =>
        HttpResponse.json(
          indexDetail({
            ...(body as Partial<IndexDetail>),
            status: "reingest_required",
            reingestRequired: true,
            rowVersion: rowVersion(51),
          }),
        ),
    );
    const { user } = open();
    await user.click(await screen.findByRole("button", { name: "Edit settings" }));
    const name = await screen.findByRole("textbox", { name: /^Name/ });
    expect(name).toHaveValue("Policy library");
    await user.type(name, " 2");
    expect(
      screen.queryByText(/Saving this change means every document must be re-ingested/),
    ).toBeNull();
    await user.click(screen.getByRole("combobox", { name: /^Language/ }));
    await user.click(await screen.findByRole("option", { name: "German" }));
    expect(
      screen.getByText(/Saving this change means every document must be re-ingested/),
    ).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Save changes" }));
    expect(await findToast("Settings saved.")).toBeInTheDocument();
    expect(api.sent).toEqual([
      {
        method: "PATCH",
        path: `/v1/admin/indexes/${INDEX}`,
        ifMatch: `"${rowVersion(50)}"`,
        body: { displayName: "Policy library 2", languageAnalyzer: "de.microsoft" },
      },
    ]);
    await waitFor(() => {
      expect(screen.queryByRole("button", { name: "Save changes" })).toBeNull();
    });
  });

  test("Edit settings: a 412 keeps the draft, shows the latest value and the next save sends the new version", async () => {
    let reads = 0;
    const api = indexApi(
      () => {
        reads += 1;
        return reads === 1
          ? indexDetail()
          : indexDetail({ description: "Changed elsewhere", rowVersion: rowVersion(60) });
      },
      ({ ifMatch, body }) =>
        ifMatch === `"${rowVersion(50)}"`
          ? problemResponse(412, "precondition_failed", { title: "Changed by someone else" })
          : HttpResponse.json(
              indexDetail({ ...(body as Partial<IndexDetail>), rowVersion: rowVersion(61) }),
            ),
    );
    const { user } = open();
    await user.click(await screen.findByRole("button", { name: "Edit settings" }));
    const name = await screen.findByRole("textbox", { name: /^Name/ });
    await user.clear(name);
    await user.type(name, "Renamed");
    await user.click(screen.getByRole("button", { name: "Save changes" }));
    expect(await screen.findByText("Latest saved value: Changed elsewhere")).toBeInTheDocument();
    expect(screen.getByText(/Your edits are still here/)).toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: /^Name/ })).toHaveValue("Renamed");
    await user.click(screen.getByRole("button", { name: "Use latest value" }));
    expect(screen.getByRole("textbox", { name: "Description" })).toHaveValue("Changed elsewhere");
    await user.click(screen.getByRole("button", { name: "Save changes" }));
    expect(await findToast("Settings saved.")).toBeInTheDocument();
    expect(api.sent[1]).toMatchObject({
      ifMatch: `"${rowVersion(60)}"`,
      body: { displayName: "Renamed" },
    });
  });

  test("Edit settings: 409 display_name_taken under Name, 422 on a setting, other refusals in the bar; Cancel closes", async () => {
    let answer = 0;
    indexApi(
      () => indexDetail(),
      () => {
        answer += 1;
        if (answer === 1) {
          return problemResponse(409, "display_name_taken", {
            detail: "Another index uses this name.",
          });
        }
        if (answer === 2) {
          return problemResponse(422, "invalid_method_parameters", {
            errors: [{ path: "body.methodParameters.tableMaxTokens", reason: "out_of_range" }],
          });
        }
        return problemResponse(404, "index_not_found", { title: "Index not found" });
      },
    );
    const { user } = open();
    await user.click(await screen.findByRole("button", { name: "Edit settings" }));
    await user.type(await screen.findByRole("textbox", { name: /^Name/ }), "!");
    await user.click(screen.getByRole("button", { name: "Save changes" }));
    expect(await screen.findByText("Another index uses this name.")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Save changes" }));
    await waitFor(() => {
      expect(screen.getByRole("spinbutton", { name: /Largest table kept whole/ })).toHaveAttribute(
        "aria-invalid",
        "true",
      );
    });
    await user.click(screen.getByRole("button", { name: "Save changes" }));
    expect(await screen.findByText("Index not found")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Back to Indexes" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    expect(await screen.findByRole("button", { name: "Edit settings" })).toBeInTheDocument();
  });
});
