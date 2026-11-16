// The citation viewer and its downloads (10-web-client.md section 8.14; acceptance criteria 39 to
// 41): T-387 and T-388.

import type { Page } from "@playwright/test";

import {
  IDS,
  adminUser,
  citation,
  citationFile,
  documentDetail,
  session,
  turn,
  versionSummary,
} from "../../fixtures/web/stub-api/builders.ts";
import type { OperationId } from "../../fixtures/web/stub-api/protocol.ts";
import { en, expect, expectAccessible, fill, test } from "../../fixtures/web/test.ts";

const QUESTION = "What do the onboarding documents say?";

const CITED = session({
  title: "Onboarding",
  turns: [
    turn({
      question: QUESTION,
      answer: "Leave is 25 days ⟦1⟧, travel needs approval ⟦2⟧ and onboarding takes a week ⟦3⟧.",
      citations: [
        citation(1, "Leave policy", 4, "pdf"),
        citation(2, "Travel guide", 2, "docx"),
        citation(3, "Onboarding deck", 7, "pptx"),
      ],
      files: {
        "1": citationFile('Budget: Q3/Q4 "draft"?.pdf', "Annual <report>|2026.docx"),
        "2": citationFile("Travel guide, page 2.pdf", "Travel guide.pdf", false),
        "3": citationFile("Onboarding deck, page 7.pdf", "Onboarding deck.pdf"),
      },
    }),
  ],
});

/** Records every object URL the page creates and revokes. */
const TRACK_OBJECT_URLS = `(() => {
  const created = [];
  const revoked = [];
  const create = URL.createObjectURL.bind(URL);
  const revoke = URL.revokeObjectURL.bind(URL);
  URL.createObjectURL = (value) => { const url = create(value); created.push(url); return url; };
  URL.revokeObjectURL = (url) => { revoked.push(url); revoke(url); };
  window.__docchatObjectUrls = { created, revoked };
})();`;

const NO_PDF_VIEWER = `Object.defineProperty(Navigator.prototype, "pdfViewerEnabled", {
  configurable: true,
  get: () => false,
});`;

async function objectUrls(page: Page): Promise<{ created: string[]; revoked: string[] }> {
  return page.evaluate(
    () =>
      (window as unknown as { __docchatObjectUrls: { created: string[]; revoked: string[] } })
        .__docchatObjectUrls,
  );
}

const VIEWER_OPERATIONS = new Set<OperationId>([
  "getCurrentUser",
  "listMyProjects",
  "listSessions",
  "getSession",
  "getCitation",
  "getCitationContent",
  "getCitationDocument",
]);

test.use({ world: { sessions: [CITED] } });

test.describe("the citation viewer (T-387)", () => {
  test("T-387 a PDF, DOCX and PPTX citation each show their cited page by the browser's PDF support", async ({
    app,
    page,
  }, testInfo) => {
    await page.addInitScript(TRACK_OBJECT_URLS);
    await app.openShell(`/chat/${CITED.sessionId}`);
    const viewerEnabled = await page.evaluate(() => navigator.pdfViewerEnabled);
    if (testInfo.project.name === "msedge") expect(viewerEnabled).toBe(true);
    const answer = app.turn(QUESTION);
    for (const [number, title, pageNumber, type] of [
      [1, "Leave policy", 4, en["citation.type.pdf"]],
      [2, "Travel guide", 2, en["citation.type.docx"]],
      [3, "Onboarding deck", 7, en["citation.type.pptx"]],
    ] as const) {
      const locator = fill(en["chat.locator.page"], { page: pageNumber });
      await app
        .sources(answer)
        .getByRole("button", { name: `${title}, ${locator}` })
        .click();
      const viewer = page.getByRole("complementary", {
        name: fill(en["citation.title"], { n: number }),
      });
      await expect(viewer).toBeVisible();
      await expect(
        viewer.getByRole("heading", { name: fill(en["citation.title"], { n: number }) }),
      ).toBeFocused();
      if (viewerEnabled) {
        const frame = viewer.locator("iframe");
        await expect(frame).toHaveAttribute("src", /^blob:/);
        await expect(frame).toHaveAttribute(
          "title",
          fill(en["citation.frame"], { n: number, title, locator }),
        );
        expect(await frame.getAttribute("sandbox")).toBeNull();
        await expect(viewer.getByText(en["citation.noPdfViewer"])).toHaveCount(0);
      } else {
        await expect(viewer.locator("iframe")).toHaveCount(0);
        await expect(viewer.getByText(type)).toBeVisible();
        await expect(viewer.getByText(en["citation.noPdfViewer"])).toBeVisible();
        await expect(
          viewer.getByRole("button", { name: en["citation.downloadPage"] }),
        ).toBeVisible();
      }
      await expectAccessible(page, `citation ${String(number)} in the viewer`);
      await viewer.getByRole("button", { name: en["citation.close"] }).click();
      await expect(viewer).toHaveCount(0);
      const urls = await objectUrls(page);
      expect(urls.created.filter((url) => !urls.revoked.includes(url))).toEqual([]);
    }
  });

  test("T-387 410 document_changed shows the problem's title and detail", async ({
    app,
    page,
    stub,
  }) => {
    await stub.problem("getCitation", { code: "document_changed" });
    await app.openShell(`/chat/${CITED.sessionId}`);
    await app
      .sources(app.turn(QUESTION))
      .getByRole("button", { name: "Leave policy, page 4" })
      .click();
    const viewer = page.getByRole("complementary", { name: fill(en["citation.title"], { n: 1 }) });
    await expect(viewer.getByText("Document changed")).toBeVisible();
    await expect(
      viewer.getByText(
        "This document has changed since the answer was written. Ask the question now for an answer from the current version.",
      ),
    ).toBeVisible();
    await expect(viewer.getByRole("button", { name: en["error.retry"] })).toHaveCount(0);
    await expectAccessible(page, "a changed document in the viewer");
  });

  test("T-387 Escape closes the viewer and returns focus to the citation button", async ({
    app,
    page,
  }) => {
    await app.openShell(`/chat/${CITED.sessionId}`);
    const button = app.turn(QUESTION).getByRole("button", { name: "Source 2: Travel guide" });
    await button.click();
    const viewer = page.getByRole("complementary", { name: fill(en["citation.title"], { n: 2 }) });
    await expect(viewer).toBeVisible();
    await page.keyboard.press("Escape");
    await expect(viewer).toHaveCount(0);
    await expect(button).toBeFocused();
  });
});

test.describe("the citation downloads (T-388)", () => {
  test("T-388 Download page and Download full document save safe names ending .pdf", async ({
    app,
    page,
    stub,
  }) => {
    // Without a PDF viewer the metadata panel offers Download page (Edge and Chrome have one).
    await page.addInitScript(NO_PDF_VIEWER);
    await app.openShell(`/chat/${CITED.sessionId}`);
    await app
      .sources(app.turn(QUESTION))
      .getByRole("button", { name: "Leave policy, page 4" })
      .click();
    const viewer = page.getByRole("complementary", { name: fill(en["citation.title"], { n: 1 }) });
    const pageDownload = page.waitForEvent("download");
    await viewer.getByRole("button", { name: en["citation.downloadPage"] }).click();
    expect((await pageDownload).suggestedFilename()).toBe("Budget_ Q3_Q4 _draft__.pdf");
    const documentDownload = page.waitForEvent("download");
    await viewer.getByRole("button", { name: en["citation.downloadDocument"] }).click();
    expect((await documentDownload).suggestedFilename()).toBe("Annual _report__2026.pdf");
    const operations = (await stub.requests()).map((call) => call.operation);
    expect(
      operations.filter((operation) => operation !== null && !VIEWER_OPERATIONS.has(operation)),
    ).toEqual([]);
  });

  test("T-388 documentPdfAvailable false shows the caption and no Download full document", async ({
    app,
    page,
  }) => {
    await app.openShell(`/chat/${CITED.sessionId}`);
    await app
      .sources(app.turn(QUESTION))
      .getByRole("button", { name: "Travel guide, page 2" })
      .click();
    const viewer = page.getByRole("complementary", { name: fill(en["citation.title"], { n: 2 }) });
    await expect(viewer.getByText(en["citation.documentTooLarge"])).toBeVisible();
    await expect(viewer.getByRole("button", { name: en["citation.downloadDocument"] })).toHaveCount(
      0,
    );
    for (const frame of await page.locator("iframe").all()) {
      expect(await frame.getAttribute("src")).toMatch(/^blob:/);
    }
  });

  test("T-388 409 document_pdf_unavailable replaces Download full document by the caption", async ({
    app,
    page,
    stub,
  }) => {
    await stub.problem("getCitationDocument", { code: "document_pdf_unavailable" });
    await app.openShell(`/chat/${CITED.sessionId}`);
    await app
      .sources(app.turn(QUESTION))
      .getByRole("button", { name: "Onboarding deck, page 7" })
      .click();
    const viewer = page.getByRole("complementary", { name: fill(en["citation.title"], { n: 3 }) });
    await viewer.getByRole("button", { name: en["citation.downloadDocument"] }).click();
    await expect(viewer.getByText("Full document unavailable")).toBeVisible();
    await expect(viewer.getByText(en["citation.documentTooLarge"])).toBeVisible();
    await expect(viewer.getByRole("button", { name: en["citation.downloadDocument"] })).toHaveCount(
      0,
    );
    await expectAccessible(page, "a whole-document PDF that isn't available");
  });

  test.describe(() => {
    test.use({
      world: {
        sessions: [CITED],
        user: adminUser(),
        admin: {
          documents: {
            [IDS.handbookIndex]: [
              documentDetail("Leave policy.pdf", 4101),
              documentDetail("Huge manual.pdf", 4102, {
                documentId: "5d8e0b1f-4a6c-4dc4-8fbb-a2a18f7e6d5c",
                currentVersion: versionSummary({ documentPdfStatus: "too_large" }),
              }),
            ],
          },
        },
      },
    });

    test("T-388 the document panel's Download PDF saves the current version's PDF, and a too-large version shows the caption", async ({
      app,
      page,
    }) => {
      await app.openShell(`/admin/indexes/${IDS.handbookIndex}/documents`);
      await page.getByRole("button", { name: "Leave policy.pdf" }).click();
      const panel = page.getByRole("dialog", { name: "Leave policy.pdf" });
      const download = page.waitForEvent("download");
      await panel.getByRole("button", { name: en["documentPanel.downloadPdf"] }).click();
      expect((await download).suggestedFilename()).toBe("Leave policy.pdf");
      await panel.getByRole("button", { name: en["documentPanel.close"] }).click();
      await page.getByRole("button", { name: "Huge manual.pdf" }).click();
      await expect(page.getByText(en["documentPanel.pdfTooLarge"])).toBeVisible();
      await expect(page.getByRole("button", { name: en["documentPanel.downloadPdf"] })).toHaveCount(
        0,
      );
      await expectAccessible(page, "the document panel");
    });
  });
});
