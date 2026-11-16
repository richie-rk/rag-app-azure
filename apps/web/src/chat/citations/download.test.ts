import { HttpResponse, http } from "msw";
import { afterEach, describe, expect, test, vi } from "vitest";

import { apiErrorOf } from "../../api/problem";
import { IDS, apiUrl, problemResponse } from "../../test/handlers";
import { configureTestHttp } from "../../test/render";
import { server } from "../../test/server";
import { downloadFile, safeFileName, saveBlob } from "./download";

const path = { sessionId: IDS.session, turnId: IDS.turn, citationNumber: 1 };
const contentUrl = apiUrl(`/v1/sessions/${IDS.session}/turns/${IDS.turn}/citations/1/content`);

function pdf(): Response {
  return new HttpResponse(new Uint8Array([37, 80, 68, 70]), {
    headers: { "Content-Type": "application/pdf", "Content-Length": "4" },
  });
}

function failureOf(pending: Promise<unknown>) {
  return pending.then(() => null, apiErrorOf);
}

afterEach(() => {
  vi.useRealTimers();
});

describe("safe file names", () => {
  test.each([
    ["Guide-page-3.pdf", "Guide-page-3.pdf"],
    ['a\\b/c:d*e?f"g<h>i|j\u0007.pdf', "a_b_c_d_e_f_g_h_i_j_.pdf"],
    ["report.docx", "report.pdf"],
    ["no-extension", "no-extension.pdf"],
    [".pdf", "source-2.pdf"],
    ["", "source-2.pdf"],
    [`${"x".repeat(160)}.pdf`, `${"x".repeat(150)}.pdf`],
  ])("%s is saved as %s", (name, expected) => {
    expect(safeFileName(name, 2)).toBe(expected);
  });
});

describe("downloading a file", () => {
  test("429 too_many_requests is repeated after retryAfterSeconds, at most twice", async () => {
    configureTestHttp();
    let calls = 0;
    server.use(
      http.get(contentUrl, () => {
        calls += 1;
        return calls <= 2
          ? problemResponse(429, "too_many_requests", { retryAfterSeconds: 1 })
          : pdf();
      }),
    );
    const file = await downloadFile("getCitationContent", { path });
    expect(calls).toBe(3);
    expect(file.blob.type).toBe("application/pdf");
  });

  test("a third 429 and every other refusal are shown at once", async () => {
    configureTestHttp();
    let calls = 0;
    server.use(
      http.get(contentUrl, () => {
        calls += 1;
        return problemResponse(429, "too_many_requests", { retryAfterSeconds: 1 });
      }),
    );
    const failure = await failureOf(downloadFile("getCitationContent", { path }));
    expect(failure?.code).toBe("too_many_requests");
    expect(calls).toBe(3);
    server.use(
      http.get(contentUrl, () =>
        problemResponse(429, "too_many_downloads", { retryAfterSeconds: 10 }),
      ),
    );
    expect((await failureOf(downloadFile("getCitationContent", { path })))?.code).toBe(
      "too_many_downloads",
    );
  });

  test("an abort during the wait ends the download", async () => {
    configureTestHttp();
    server.use(
      http.get(contentUrl, () =>
        problemResponse(429, "too_many_requests", { retryAfterSeconds: 5 }),
      ),
    );
    const controller = new AbortController();
    const pending = downloadFile("getCitationContent", { path, signal: controller.signal });
    await new Promise((resolve) => setTimeout(resolve, 20));
    controller.abort();
    await expect(pending).rejects.toThrow("Aborted");
  });

  test("saveBlob clicks a temporary link and revokes its URL 30,000 ms later", () => {
    vi.useFakeTimers();
    const create = vi.fn(() => "blob:test");
    const revoke = vi.fn();
    vi.stubGlobal("URL", Object.assign(URL, { createObjectURL: create, revokeObjectURL: revoke }));
    const click = vi
      .spyOn(HTMLAnchorElement.prototype, "click")
      .mockImplementation(() => undefined);
    saveBlob(new Blob(["x"], { type: "application/pdf" }), "page.pdf");
    expect(click).toHaveBeenCalledTimes(1);
    const link = click.mock.contexts[0] as HTMLAnchorElement;
    expect(link.download).toBe("page.pdf");
    expect(link.isConnected).toBe(false);
    expect(revoke).not.toHaveBeenCalled();
    vi.advanceTimersByTime(30_000);
    expect(revoke).toHaveBeenCalledWith("blob:test");
  });
});
