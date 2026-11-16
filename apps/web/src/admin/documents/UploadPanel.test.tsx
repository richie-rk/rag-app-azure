import { act, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { HttpResponse, http } from "msw";
import { describe, expect, test, vi } from "vitest";

import type {
  IndexDetail,
  UploadPreflightRequest,
  UploadPreflightResult,
  UploadReservationRequest,
  UploadSubmitRequest,
} from "../../api/generated/types.gen";
import { hasActiveWork } from "../../platform/activeWork";
import {
  documentSummary,
  indexDetail,
  indexOptions,
  page,
  uuid,
  versionSummary,
} from "../../test/adminData";
import { renderAdmin } from "../../test/adminRender";
import { installFakeXhr, type FakeUpload } from "../../test/fakeXhr";
import { API_BASE, apiUrl, problemResponse } from "../../test/handlers";
import { server } from "../../test/server";
import { DocumentsScreen } from "./DocumentsScreen";

const INDEX = uuid(500);
const PROJECT_A = { projectId: uuid(1), name: "Alpha" };
const ROUTES = [
  { path: "/admin/indexes/:indexId/documents", element: <DocumentsScreen /> },
  { path: "/admin/runs/:runId", element: <p>run page</p> },
];

function preflightResult(name: string): UploadPreflightResult {
  const status = name.startsWith("update")
    ? "update"
    : name.startsWith("dup")
      ? "duplicate"
      : "new";
  return {
    name,
    status,
    reason: null,
    documentId: status === "new" ? null : uuid(210),
    existingDocument: status === "duplicate" ? { documentId: uuid(211), name: "other.pdf" } : null,
    currentVersionNumber: status === "new" ? null : 1,
  };
}

interface ApiOptions {
  /** The index each read answers, in turn; the last one repeats. */
  readonly indexes?: readonly (IndexDetail | Response)[];
  readonly options?: ReturnType<typeof indexOptions>;
  readonly preflight?: (body: UploadPreflightRequest) => Response | null;
  readonly reserve?: (body: UploadReservationRequest) => Response | null;
  readonly submit?: (body: UploadSubmitRequest) => Response | null;
}

/** The Documents screen's reads and the upload operations, recording what the panel sends. */
function uploadApi(options: ApiOptions = {}) {
  const sent = {
    preflight: [] as UploadPreflightRequest[],
    reserve: [] as UploadReservationRequest[],
    submit: [] as UploadSubmitRequest[],
    lists: 0,
    indexReads: 0,
  };
  const indexes = options.indexes ?? [indexDetail({ mappedProjects: [PROJECT_A] })];
  server.use(
    http.get(apiUrl(`/v1/admin/indexes/${INDEX}`), () => {
      const answer = indexes[Math.min(sent.indexReads, indexes.length - 1)];
      sent.indexReads += 1;
      return answer instanceof Response ? answer : HttpResponse.json(answer);
    }),
    http.get(apiUrl(`/v1/admin/indexes/${INDEX}/documents`), () => {
      sent.lists += 1;
      return HttpResponse.json(page([documentSummary(1)]));
    }),
    http.get(apiUrl("/v1/admin/index-options"), () =>
      HttpResponse.json(options.options ?? indexOptions()),
    ),
    http.post(apiUrl(`/v1/admin/indexes/${INDEX}/uploads/preflight`), async ({ request }) => {
      const body = (await request.json()) as UploadPreflightRequest;
      sent.preflight.push(body);
      return (
        options.preflight?.(body) ??
        HttpResponse.json({ results: body.files.map((file) => preflightResult(file.name)) })
      );
    }),
    http.post(apiUrl(`/v1/admin/indexes/${INDEX}/uploads`), async ({ request }) => {
      const body = (await request.json()) as UploadReservationRequest;
      sent.reserve.push(body);
      const n = sent.reserve.length;
      return (
        options.reserve?.(body) ??
        HttpResponse.json(
          { documentId: uuid(220 + n), versionId: uuid(320 + n), versionNumber: 1, reused: false },
          { status: 201 },
        )
      );
    }),
    http.post(apiUrl(`/v1/admin/indexes/${INDEX}/uploads/submit`), async ({ request }) => {
      const body = (await request.json()) as UploadSubmitRequest;
      sent.submit.push(body);
      return (
        options.submit?.(body) ??
        HttpResponse.json(
          {
            runId: uuid(450),
            results: body.versionIds.map((versionId) => ({ versionId, outcome: "queued" })),
          },
          { status: 202 },
        )
      );
    }),
  );
  return sent;
}

function uploaded(xhr: FakeUpload, n: number) {
  xhr.answer(200, { version: versionSummary(n, { state: "pending", documentPdfStatus: null }) });
}

async function openPanel(ready = true, project?: string) {
  const harness = renderAdmin(ROUTES, `/admin/indexes/${INDEX}/documents`);
  const button = await screen.findByRole("button", { name: "Upload documents" });
  if (project !== undefined) {
    const picker = await screen.findByRole("combobox", { name: /Upload through project/ });
    await waitFor(() => {
      expect(picker).toBeEnabled();
    });
    await harness.user.click(picker);
    await harness.user.click(await screen.findByRole("option", { name: project }));
  }
  await waitFor(() => {
    expect(button).toBeEnabled();
  });
  await harness.user.click(button);
  const panel = await screen.findByRole("dialog", { name: "Upload documents" });
  if (ready) await within(panel).findByRole("button", { name: "Choose files" });
  return { ...harness, panel };
}

function choose(panel: HTMLElement, names: readonly string[]) {
  const input = panel.querySelector<HTMLInputElement>('input[type="file"]');
  if (input === null) throw new Error("no file input");
  const files = names.map((name) => new File(["%PDF-1.7"], name, { type: "application/pdf" }));
  fireEvent.change(input, { target: { files } });
  return input;
}

describe("the upload panel (section 10)", () => {
  test("T-404 new, update and duplicate: only the new file and the confirmed update upload, one at a time, then one submit", async () => {
    const sent = uploadApi();
    const xhrs = installFakeXhr();
    const { panel, user } = await openPanel();
    const input = choose(panel, ["new.pdf", "update.pdf", "dup.pdf"]);
    expect(input).toHaveAttribute("accept", ".pdf,.docx,.pptx");
    expect(input).toHaveAttribute("multiple");
    expect(await within(panel).findByText("New")).toBeInTheDocument();
    expect(
      within(panel).getByText("This document is already ingested. Update it?"),
    ).toBeInTheDocument();
    expect(within(panel).getByText("Same file as other.pdf. Skipped.")).toBeInTheDocument();
    expect(sent.preflight).toHaveLength(1);
    expect(sent.preflight[0]?.files.map((file) => [file.name, file.sizeBytes])).toEqual([
      ["new.pdf", 8],
      ["update.pdf", 8],
      ["dup.pdf", 8],
    ]);
    expect(sent.preflight[0]?.files[0]?.sha256).toMatch(/^[0-9a-f]{64}$/);
    const upload = within(panel).getByRole("button", { name: "Upload 1 file" });
    expect(upload).toBeEnabled();
    await user.click(within(panel).getByRole("checkbox", { name: "Update update.pdf" }));
    await user.click(within(panel).getByRole("button", { name: "Upload 2 files" }));

    await waitFor(() => {
      expect(xhrs).toHaveLength(1);
    });
    expect(hasActiveWork()).toBe(true);
    const first = xhrs[0];
    if (first === undefined) throw new Error("no upload");
    expect(first.method).toBe("PUT");
    expect(first.url).toBe(`${API_BASE}/v1/admin/indexes/${INDEX}/uploads/${uuid(321)}/content`);
    expect(first.headers["Content-Type"]).toBe("application/octet-stream");
    expect((first.body as File).name).toBe("new.pdf");
    expect(within(panel).getByRole("button", { name: "Stop uploading" })).toBeInTheDocument();
    act(() => {
      first.progress(4, 8);
    });
    expect(await within(panel).findByText("Uploading new.pdf: 50%")).toBeInTheDocument();
    // One file at a time: the second reservation waits for the first file's bytes.
    expect(sent.reserve).toHaveLength(1);
    act(() => {
      uploaded(first, 1);
    });
    await waitFor(() => {
      expect(xhrs).toHaveLength(2);
    });
    act(() => {
      if (xhrs[1] !== undefined) uploaded(xhrs[1], 2);
    });

    expect(await within(panel).findByText("Upload complete")).toBeInTheDocument();
    expect(within(panel).getByText("Ingestion has started for 2 files.")).toBeInTheDocument();
    expect(within(panel).getByRole("link", { name: "View progress" })).toHaveAttribute(
      "href",
      `/admin/runs/${uuid(450)}`,
    );
    expect(sent.reserve.map((body) => [body.name, body.confirmUpdate, body.projectId])).toEqual([
      ["new.pdf", false, PROJECT_A.projectId],
      ["update.pdf", true, PROJECT_A.projectId],
    ]);
    expect(sent.submit).toEqual([{ versionIds: [uuid(321), uuid(322)] }]);
    expect(within(panel).getAllByText("Uploaded")).toHaveLength(2);
    expect(within(panel).queryByRole("button", { name: /^Upload \d/ })).toBeNull();
    expect(hasActiveWork()).toBe(false);
    // The Documents list is read again.
    await waitFor(() => {
      expect(sent.lists).toBeGreaterThan(1);
    });
    // The summary's Close, beside the header's.
    const closes = within(panel).getAllByRole("button", { name: "Close" });
    expect(closes).toHaveLength(2);
    await user.click(closes[1] ?? panel);
    await waitFor(() => {
      expect(screen.queryByRole("dialog", { name: "Upload documents" })).toBeNull();
    });
  });

  test("the drop zone takes files too, and states the limits", async () => {
    const sent = uploadApi();
    const { panel } = await openPanel();
    expect(
      within(panel).getByText(
        "Or drop files here. PDF, Word (.docx) and PowerPoint (.pptx) files, up to 50.0 MB each.",
      ),
    ).toBeInTheDocument();
    const zone = within(panel).getByRole("button", { name: "Choose files" }).parentElement;
    if (zone === null) throw new Error("no drop zone");
    fireEvent.dragOver(zone);
    fireEvent.dragLeave(zone);
    fireEvent.drop(zone, {
      dataTransfer: { files: [new File(["%PDF"], "dropped.pdf")] },
    });
    expect(await within(panel).findByText("dropped.pdf")).toBeInTheDocument();
    await waitFor(() => {
      expect(sent.preflight).toHaveLength(1);
    });
  });

  test("T-403 a selection that would pass maxFilesPerBatch shows upload.tooMany and adds nothing", async () => {
    const sent = uploadApi({
      options: indexOptions({
        uploadLimits: {
          maxFileBytes: 52_428_800,
          maxFilesPerBatch: 2,
          allowedExtensions: [".pdf", ".docx", ".pptx"],
        },
      }),
    });
    const { panel } = await openPanel();
    choose(panel, ["a.pdf", "b.pdf", "c.pdf"]);
    expect(await within(panel).findByText("Choose up to 2 files at a time.")).toBeInTheDocument();
    expect(within(panel).queryByText("a.pdf")).toBeNull();
    expect(sent.preflight).toEqual([]);
    choose(panel, ["sheet.xlsx"]);
    expect(
      await within(panel).findByText("Not a PDF, Word or PowerPoint file. Skipped."),
    ).toBeInTheDocument();
    expect(within(panel).queryByText("Choose up to 2 files at a time.")).toBeNull();
    expect(
      within(panel).getByText("Nothing to upload. Every file was skipped."),
    ).toBeInTheDocument();
    expect(within(panel).getByRole("button", { name: "Upload 0 files" })).toBeDisabled();
  });

  test("T-418 the warning above Upload names every mapped project, read again when the panel opens", async () => {
    const three = [
      PROJECT_A,
      { projectId: uuid(2), name: "Beta" },
      { projectId: uuid(3), name: "Gamma" },
    ];
    // The screen loaded with one mapping; the panel's own read finds three.
    uploadApi({
      indexes: [
        indexDetail({ mappedProjects: [PROJECT_A] }),
        indexDetail({ mappedProjects: three }),
      ],
    });
    const { panel } = await openPanel();
    expect(
      await within(panel).findByText(
        "Everyone in the 3 projects that use this index can find these documents in chat: Alpha, Beta, Gamma.",
      ),
    ).toBeInTheDocument();
  });

  test("T-418 with 12 mapped projects it names 10 and the count of the rest", async () => {
    const twelve = Array.from({ length: 12 }, (_, index) => ({
      projectId: uuid(index + 1),
      name: `Project ${String(index + 1).padStart(2, "0")}`,
    }));
    uploadApi({ indexes: [indexDetail({ mappedProjects: twelve })] });
    const { panel } = await openPanel(true, "Project 03");
    expect(
      await within(panel).findByText(/Project 01, .*Project 10 and 2 more\.$/),
    ).toBeInTheDocument();
    expect(within(panel).queryByText(/Project 11/)).toBeNull();
  });

  test("T-418 while the read fails the warning names no project, and Upload still works", async () => {
    const sent = uploadApi({
      indexes: [
        indexDetail({ mappedProjects: [PROJECT_A] }),
        problemResponse(503, "dependency_unavailable"),
      ],
    });
    installFakeXhr((xhr) => {
      uploaded(xhr, 1);
    });
    const { panel, user } = await openPanel();
    await waitFor(() => {
      expect(sent.indexReads).toBe(2);
    });
    expect(
      within(panel).getByText(
        "Everyone in every project that uses this index can find these documents in chat.",
      ),
    ).toBeInTheDocument();
    choose(panel, ["a.pdf"]);
    await user.click(await within(panel).findByRole("button", { name: "Upload 1 file" }));
    expect(await within(panel).findByText("Upload complete")).toBeInTheDocument();
  });

  test("T-407 503 malware_scanning_not_ready from the preflight shows the problem's text and the retry time, never error.server", async () => {
    uploadApi({
      preflight: () =>
        problemResponse(503, "malware_scanning_not_ready", {
          title: "Uploads are paused",
          detail: "Malware scanning isn't confirmed yet.",
          retryAfterSeconds: 3600,
        }),
    });
    const { panel } = await openPanel();
    choose(panel, ["a.pdf"]);
    expect(await within(panel).findByText("Uploads are paused")).toBeInTheDocument();
    expect(within(panel).getByText("Malware scanning isn't confirmed yet.")).toBeInTheDocument();
    expect(within(panel).getByText(/^You can retry at /)).toBeInTheDocument();
    expect(within(panel).queryByText("Something went wrong")).toBeNull();
    expect(within(panel).getByRole("button", { name: "Retry" })).toBeDisabled();
    expect(within(panel).getByRole("button", { name: "Upload 0 files" })).toBeDisabled();
    expect(within(panel).getByRole("button", { name: "Choose files" })).toBeDisabled();
  });

  test("T-407 the same answer mid-batch isn't retried, pauses the current and remaining files and sends nothing more", async () => {
    const sent = uploadApi();
    const xhrs = installFakeXhr((xhr) => {
      if (xhr.url.includes(uuid(321))) {
        uploaded(xhr, 1);
        return;
      }
      xhr.answer(
        503,
        {
          type: "urn:docchat:problem:malware_scanning_not_ready",
          title: "Uploads are paused",
          status: 503,
          detail: "Malware scanning isn't confirmed yet.",
          code: "malware_scanning_not_ready",
          correlationId: "0123456789abcdef0123456789abcdef",
          retryAfterSeconds: 3600,
        },
        "application/problem+json",
      );
    });
    const { panel, user } = await openPanel();
    choose(panel, ["a.pdf", "b.pdf", "c.pdf"]);
    await user.click(await within(panel).findByRole("button", { name: "Upload 3 files" }));
    expect(await within(panel).findByText("Uploads are paused")).toBeInTheDocument();
    expect(within(panel).getAllByText("Not uploaded. Uploads are paused.")).toHaveLength(2);
    expect(within(panel).getByText("Uploaded")).toBeInTheDocument();
    expect(xhrs).toHaveLength(2);
    expect(sent.reserve).toHaveLength(2);
    expect(sent.submit).toEqual([]);
    expect(within(panel).getByRole("button", { name: "Retry" })).toBeDisabled();
    // Upload stays, disabled, with the shared-index warning above it (sections 10.4 and 10.7).
    expect(within(panel).getByRole("button", { name: "Upload 3 files" })).toBeDisabled();
    expect(within(panel).getByText(/can find these documents in chat/)).toBeInTheDocument();
    expect(within(panel).queryByRole("button", { name: "Stop uploading" })).toBeNull();
    expect(hasActiveWork()).toBe(false);
  });

  test("T-406 429 upload_busy waits retryAfterSeconds before the next attempt (fake timers)", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      const sent = uploadApi();
      const xhrs = installFakeXhr((xhr) => {
        if (xhrs.length === 1) {
          xhr.answer(
            429,
            {
              type: "urn:docchat:problem:upload_busy",
              title: "Too many uploads",
              status: 429,
              detail: "Try again shortly.",
              code: "upload_busy",
              correlationId: "0123456789abcdef0123456789abcdef",
              retryAfterSeconds: 5,
            },
            "application/problem+json",
          );
          return;
        }
        uploaded(xhr, 1);
      });
      const { panel, user } = await openPanel();
      choose(panel, ["a.pdf"]);
      await user.click(await within(panel).findByRole("button", { name: "Upload 1 file" }));
      await waitFor(() => {
        expect(xhrs).toHaveLength(1);
      });
      await act(() => vi.advanceTimersByTimeAsync(4_000));
      expect(xhrs).toHaveLength(1);
      await act(() => vi.advanceTimersByTimeAsync(1_100));
      await waitFor(() => {
        expect(xhrs).toHaveLength(2);
      });
      expect(await within(panel).findByText("Upload complete")).toBeInTheDocument();
      expect(sent.reserve).toHaveLength(1);
    } finally {
      vi.useRealTimers();
    }
  });

  test("Stop uploading aborts the current file, skips the rest and still submits the uploaded ones", async () => {
    const sent = uploadApi();
    const xhrs = installFakeXhr((xhr) => {
      if (xhr.url.includes(uuid(321))) uploaded(xhr, 1);
    });
    const { panel, user } = await openPanel();
    choose(panel, ["a.pdf", "b.pdf", "c.pdf"]);
    await user.click(await within(panel).findByRole("button", { name: "Upload 3 files" }));
    await waitFor(() => {
      expect(xhrs).toHaveLength(2);
    });
    await user.click(within(panel).getByRole("button", { name: "Stop uploading" }));
    expect(
      await within(panel).findByText(
        "1 of 3 files uploaded. Ingestion has started for those. Choose the others in a new batch to upload them.",
      ),
    ).toBeInTheDocument();
    expect(xhrs[1]?.aborted).toBe(true);
    expect(within(panel).getAllByText("Not uploaded. You stopped the upload.")).toHaveLength(2);
    expect(sent.submit).toEqual([{ versionIds: [uuid(321)] }]);
  });

  test("a failed submit shows the batch's error bar, whose Retry sends the same IDs", async () => {
    let fail = true;
    const sent = uploadApi({
      submit: () =>
        fail ? problemResponse(500, "internal_error", { title: "Server error" }) : null,
    });
    installFakeXhr((xhr) => {
      uploaded(xhr, 1);
    });
    const { panel, user } = await openPanel();
    choose(panel, ["a.pdf"]);
    await user.click(await within(panel).findByRole("button", { name: "Upload 1 file" }));
    expect(await within(panel).findByText("Server error")).toBeInTheDocument();
    fail = false;
    await user.click(within(panel).getByRole("button", { name: "Retry" }));
    expect(await within(panel).findByText("Upload complete")).toBeInTheDocument();
    expect(sent.submit).toEqual([{ versionIds: [uuid(321)] }, { versionIds: [uuid(321)] }]);
  });

  test("a file that fails after its retries shows why; the summary is partial without a run when all were queued before", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      uploadApi({
        submit: (body) =>
          HttpResponse.json({
            runId: null,
            results: body.versionIds.map((versionId) => ({ versionId, outcome: "already_queued" })),
          }),
      });
      const xhrs = installFakeXhr((xhr) => {
        if (xhr.url.includes(uuid(321))) uploaded(xhr, 1);
        else xhr.fail();
      });
      const { panel, user } = await openPanel();
      choose(panel, ["a.pdf", "b.pdf"]);
      await user.click(await within(panel).findByRole("button", { name: "Upload 2 files" }));
      await act(() => vi.advanceTimersByTimeAsync(2_100));
      await act(() => vi.advanceTimersByTimeAsync(6_100));
      expect(
        await within(panel).findByText("Not uploaded. Check your connection, then select Retry."),
      ).toBeInTheDocument();
      expect(xhrs).toHaveLength(4);
      expect(await within(panel).findByText(/^1 of 2 files uploaded/)).toBeInTheDocument();
      expect(within(panel).queryByRole("link", { name: "View progress" })).toBeNull();
    } finally {
      vi.useRealTimers();
    }
  });

  test("the index options didn't load: index.optionsError with Retry", async () => {
    let options = 0;
    uploadApi();
    server.use(
      http.get(apiUrl("/v1/admin/index-options"), () => {
        options += 1;
        return options === 1
          ? problemResponse(500, "internal_error")
          : HttpResponse.json(indexOptions());
      }),
    );
    const { panel, user } = await openPanel(false);
    expect(await within(panel).findByText("The index options didn't load.")).toBeInTheDocument();
    await user.click(within(panel).getByRole("button", { name: "Retry" }));
    expect(await within(panel).findByRole("button", { name: "Choose files" })).toBeInTheDocument();
  });
});
