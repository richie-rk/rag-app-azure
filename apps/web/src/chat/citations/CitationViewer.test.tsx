import { act, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { HttpResponse, http } from "msw";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

import { CITATION, present } from "../../test/chat";
import { IDS, apiUrl, problemResponse } from "../../test/handlers";
import { configureTestHttp, renderWithProviders } from "../../test/render";
import { server } from "../../test/server";
import { CitationViewer, type CitationTarget, type CitationViewerProps } from "./CitationViewer";

const base = `/v1/sessions/${IDS.session}/turns/${IDS.turn}/citations/1`;
const target: CitationTarget = { sessionId: IDS.session, turnId: IDS.turn, citation: CITATION };

const DETAIL = {
  ...CITATION,
  fileName: "Expenses guide-page-3.pdf",
  documentPdfAvailable: true,
  documentFileName: "Expenses guide.pdf",
};

function detail(fields: Record<string, unknown> = {}) {
  server.use(http.get(apiUrl(base), () => HttpResponse.json({ ...DETAIL, ...fields })));
}

function pdf(length?: string): Response {
  return new HttpResponse(new Uint8Array([37, 80, 68, 70]), {
    headers: { "Content-Type": "application/pdf", "Content-Length": length ?? "4" },
  });
}

let create: ReturnType<typeof vi.fn<(blob: Blob) => string>>;
let revoke: ReturnType<typeof vi.fn<(url: string) => void>>;

function setPdfViewer(enabled: boolean) {
  Object.defineProperty(navigator, "pdfViewerEnabled", { configurable: true, value: enabled });
}

beforeEach(() => {
  let count = 0;
  create = vi.fn(() => {
    count += 1;
    return `blob:viewer-${String(count)}`;
  });
  revoke = vi.fn();
  Object.defineProperty(URL, "createObjectURL", { configurable: true, value: create });
  Object.defineProperty(URL, "revokeObjectURL", { configurable: true, value: revoke });
  setPdfViewer(false);
  configureTestHttp();
});

afterEach(() => {
  vi.useRealTimers();
});

function show(props: Partial<CitationViewerProps> = {}) {
  const onClose = props.onClose ?? vi.fn();
  const harness = renderWithProviders(
    <CitationViewer target={target} layout="inline" onClose={onClose} {...props} />,
  );
  return { ...harness, onClose };
}

describe("T-387 the citation viewer", () => {
  test("T-387 without a PDF viewer it shows the metadata panel and Download page, and no frame", async () => {
    detail();
    show();
    const viewer = screen.getByRole("complementary", { name: "Source 1" });
    await waitFor(() => {
      expect(screen.getByRole("heading", { name: "Source 1" })).toHaveFocus();
    });
    expect(within(viewer).getByText("Expenses guide")).toBeInTheDocument();
    expect(await within(viewer).findByText("PDF document")).toBeInTheDocument();
    expect(within(viewer).getByText("Expenses guide-page-3.pdf")).toBeInTheDocument();
    expect(within(viewer).getAllByText("page 3")).toHaveLength(2);
    expect(
      within(viewer).getByText("Your browser can't show PDFs here. Download the page to open it."),
    ).toBeInTheDocument();
    expect(within(viewer).getByRole("button", { name: "Download page" })).toBeInTheDocument();
    expect(
      within(viewer).getByRole("button", { name: "Download full document" }),
    ).toBeInTheDocument();
    expect(viewer.querySelector("iframe")).toBeNull();
  });

  test("T-387 with a PDF viewer the page shows from a blob in a frame without sandbox, revoked on close", async () => {
    setPdfViewer(true);
    detail({
      contentType: "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    });
    server.use(http.get(apiUrl(`${base}/content`), () => pdf()));
    const { result, onClose } = show();
    const frame = await waitFor(() => {
      return present(document.querySelector("iframe"));
    });
    expect(frame.getAttribute("src")).toBe("blob:viewer-1");
    expect(frame.hasAttribute("sandbox")).toBe(false);
    expect(frame.getAttribute("referrerpolicy")).toBe("no-referrer");
    expect(frame.getAttribute("title")).toBe("Source 1: Expenses guide, page 3");
    expect(create.mock.calls[0]?.[0].type).toBe("application/pdf");
    fireEvent.click(screen.getByRole("button", { name: "Close source" }));
    expect(onClose).toHaveBeenCalled();
    result.unmount();
    expect(revoke).toHaveBeenCalledWith("blob:viewer-1");
  });

  test("T-387 Escape closes the inline viewer", async () => {
    detail();
    const { onClose } = show();
    await screen.findByText("PDF document");
    fireEvent.keyDown(screen.getByRole("heading", { name: "Source 1" }), { key: "Escape" });
    fireEvent.keyDown(screen.getByRole("heading", { name: "Source 1" }), { key: "a" });
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  test("T-387 the overlay layouts close through the drawer", async () => {
    detail();
    const { onClose } = show({ layout: "full" });
    expect(await screen.findByRole("dialog", { name: "Source 1" })).toBeInTheDocument();
    await act(async () => {
      fireEvent.keyDown(document.activeElement ?? document.body, { key: "Escape" });
      await Promise.resolve();
    });
    expect(onClose).toHaveBeenCalled();
  });

  test("T-387 without a whole-document PDF the footer shows the caption", async () => {
    detail({ documentPdfAvailable: false });
    show();
    expect(
      await screen.findByText(
        "This document is too large to download as one file. Its cited pages still open here.",
      ),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Download full document" })).toBeNull();
  });

  test("T-387 Download page and Download full document save under their safe names", async () => {
    detail({ documentFileName: 'Expenses: "2026".docx' });
    server.use(
      http.get(apiUrl(`${base}/content`), () => pdf()),
      http.get(apiUrl(`${base}/document`), () => pdf()),
    );
    const click = vi
      .spyOn(HTMLAnchorElement.prototype, "click")
      .mockImplementation(() => undefined);
    const { user } = show();
    await user.click(await screen.findByRole("button", { name: "Download page" }));
    await waitFor(() => {
      expect(click).toHaveBeenCalledTimes(1);
    });
    await user.click(screen.getByRole("button", { name: "Download full document" }));
    await waitFor(() => {
      expect(click).toHaveBeenCalledTimes(2);
    });
    const names = click.mock.contexts.map((link) => (link as HTMLAnchorElement).download);
    expect(names).toEqual(["Expenses guide-page-3.pdf", "Expenses_ _2026_.pdf"]);
  });

  test("T-387 409 document_pdf_unavailable replaces Download full document by the caption", async () => {
    detail();
    server.use(
      http.get(apiUrl(`${base}/document`), () =>
        problemResponse(409, "document_pdf_unavailable", {
          title: "Document PDF unavailable",
          detail: "This document has no PDF to download.",
        }),
      ),
    );
    const { user } = show();
    await user.click(await screen.findByRole("button", { name: "Download full document" }));
    expect(await screen.findByText("Document PDF unavailable")).toBeInTheDocument();
    expect(screen.getByText("This document has no PDF to download.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Download full document" })).toBeNull();
    expect(
      screen.getByText(
        "This document is too large to download as one file. Its cited pages still open here.",
      ),
    ).toBeInTheDocument();
  });

  test("T-387 410 document_changed shows its title and detail with no Retry", async () => {
    server.use(
      http.get(apiUrl(base), () =>
        problemResponse(410, "document_changed", {
          title: "Document changed",
          detail: "The cited document changed since this answer.",
        }),
      ),
    );
    show();
    expect(await screen.findByText("Document changed")).toBeInTheDocument();
    expect(screen.getByText("The cited document changed since this answer.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Retry" })).toBeNull();
  });

  test("T-387 404 citation_not_found shows its text with no Retry; another problem offers Retry", async () => {
    let calls = 0;
    server.use(
      http.get(apiUrl(base), () => {
        calls += 1;
        return calls === 1
          ? problemResponse(404, "citation_not_found", { title: "Source not found" })
          : HttpResponse.json(DETAIL);
      }),
    );
    show();
    expect(await screen.findByText("Source not found")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Retry" })).toBeNull();
  });

  test("T-387 a failed getCitation without a problem shows citation.error, whose Retry reads again", async () => {
    let calls = 0;
    server.use(
      http.get(apiUrl(base), () => {
        calls += 1;
        return calls === 1 ? new HttpResponse(null, { status: 502 }) : HttpResponse.json(DETAIL);
      }),
    );
    const { user } = show();
    expect(await screen.findByText("The source didn't load. Select Retry.")).toBeInTheDocument();
    expect(screen.getByText(/Reference ID: [0-9a-f]{32}/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByText("PDF document")).toBeInTheDocument();
  });
});

describe("T-390 cut-short downloads", () => {
  test("T-390 a page body shorter than its Content-Length shows citation.error with Retry and the trace ID, and makes no URL", async () => {
    setPdfViewer(true);
    detail();
    server.use(http.get(apiUrl(`${base}/content`), () => pdf()));
    // MSW answers with the whole body, so the first content answer is built here.
    const intercepted = globalThis.fetch;
    let calls = 0;
    vi.stubGlobal("fetch", (input: RequestInfo | URL, init?: RequestInit) => {
      const url = input instanceof Request ? input.url : String(input);
      if (!url.endsWith("/content")) return intercepted(input, init);
      calls += 1;
      if (calls > 1) return intercepted(input, init);
      return Promise.resolve(
        new Response(new Uint8Array(4), {
          headers: { "Content-Type": "application/pdf", "Content-Length": "100" },
        }),
      );
    });
    const { user } = show();
    expect(await screen.findByText("The source didn't load. Select Retry.")).toBeInTheDocument();
    expect(screen.getByText(/Reference ID: [0-9a-f]{32}/)).toBeInTheDocument();
    expect(create).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "Retry" }));
    await waitFor(() => {
      expect(document.querySelector("iframe")).not.toBeNull();
    });
  });

  test("T-390 a whole-document read that rejects shows citation.downloadFailed and saves nothing", async () => {
    detail();
    server.use(
      http.get(
        apiUrl(`${base}/document`),
        () =>
          new HttpResponse(
            new ReadableStream({
              start(controller) {
                controller.error(new Error("reset"));
              },
            }),
            { headers: { "Content-Type": "application/pdf" } },
          ),
      ),
    );
    const click = vi
      .spyOn(HTMLAnchorElement.prototype, "click")
      .mockImplementation(() => undefined);
    const { user } = show();
    await user.click(await screen.findByRole("button", { name: "Download full document" }));
    expect(
      await screen.findByText("The download didn't finish. Select Retry."),
    ).toBeInTheDocument();
    expect(screen.getByText(/Reference ID: [0-9a-f]{32}/)).toBeInTheDocument();
    expect(click).not.toHaveBeenCalled();
    expect(create).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "Retry" })).toBeEnabled();
  });
});

describe("T-389 download limits", () => {
  test("T-389 429 too_many_downloads on the frame shows the time and a disabled Retry until it passes", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    setPdfViewer(true);
    detail();
    let calls = 0;
    server.use(
      http.get(apiUrl(`${base}/content`), () => {
        calls += 1;
        return calls === 1
          ? problemResponse(429, "too_many_downloads", {
              title: "Too many downloads",
              retryAfterSeconds: 10,
            })
          : pdf();
      }),
    );
    show();
    expect(await screen.findByText("Too many downloads")).toBeInTheDocument();
    expect(screen.getByText(/You can retry at/)).toBeInTheDocument();
    const retry = screen.getByRole("button", { name: "Retry" });
    expect(retry).toBeDisabled();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10_000);
    });
    expect(calls).toBe(1);
    expect(retry).toBeEnabled();
    fireEvent.click(retry);
    await waitFor(() => {
      expect(document.querySelector("iframe")).not.toBeNull();
    });
    expect(calls).toBe(2);
  });

  test("T-389 429 rate_limited on Download full document disables it for the wait, then repeats the call", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    detail();
    let calls = 0;
    server.use(
      http.get(apiUrl(`${base}/document`), () => {
        calls += 1;
        return calls === 1
          ? problemResponse(429, "rate_limited", {
              title: "Too many downloads",
              retryAfterSeconds: 25,
            })
          : pdf();
      }),
    );
    const click = vi
      .spyOn(HTMLAnchorElement.prototype, "click")
      .mockImplementation(() => undefined);
    show();
    fireEvent.click(await screen.findByRole("button", { name: "Download full document" }));
    expect(await screen.findByText("Too many downloads")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Download full document" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Retry" })).toBeDisabled();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(25_000);
    });
    expect(calls).toBe(1);
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    await waitFor(() => {
      expect(click).toHaveBeenCalledTimes(1);
    });
    expect(calls).toBe(2);
  });
});
