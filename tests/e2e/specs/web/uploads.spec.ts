// The upload browser tests (10-web-client.md section 10; acceptance criteria 53 to 55 and 65):
// T-404, T-405, T-407 and T-418, on the Documents screen's upload panel.

import type { Page } from "@playwright/test";

import { IDS, adminUser, indexDetail } from "../../fixtures/web/stub-api/builders.ts";
import type { WorldInit } from "../../fixtures/web/stub-api/protocol.ts";
import { en, expect, expectAccessible, fill, test } from "../../fixtures/web/test.ts";

const DOCUMENTS = `/admin/indexes/${IDS.handbookIndex}/documents`;

function pdf(name: string): { name: string; mimeType: string; buffer: Buffer } {
  return { name, mimeType: "application/pdf", buffer: Buffer.from(`%PDF-1.4\n% ${name}\n%%EOF\n`) };
}

async function openPanel(page: Page): Promise<void> {
  await page.getByRole("button", { name: en["documents.upload"] }).click();
  await expect(page.getByRole("dialog", { name: en["upload.title"] })).toBeVisible();
}

async function choose(page: Page, names: readonly string[]): Promise<void> {
  await page
    .getByRole("dialog", { name: en["upload.title"] })
    .locator("input[type=file]")
    .setInputFiles(names.map(pdf));
}

/** Chooses Upload through project, which nothing preselects while several projects are mapped
 * (section 7.9); Upload documents waits for it. */
async function uploadThrough(page: Page, project: string): Promise<void> {
  await page.getByRole("combobox", { name: en["documents.project"] }).click();
  await page.getByRole("option", { name: project }).click();
}

/** The index mapped to `count` projects, named Project 1 to Project n. */
function mappedTo(count: number): NonNullable<WorldInit["admin"]> {
  const projects = Array.from({ length: count }, (_, index) => ({
    projectId:
      index === 0 ? IDS.handbook : `0f3e5c2a-9b1d-4e7f-8a6c-${String(index).padStart(12, "0")}`,
    name: index === 0 ? "Employee handbook" : `Project ${String(index + 1)}`,
  }));
  return {
    indexes: [indexDetail(IDS.handbookIndex, "Handbook index", 3001, { mappedProjects: projects })],
  };
}

test.use({
  world: {
    user: adminUser(),
    admin: {
      preflight: {
        "Leave policy.pdf": { status: "update" },
        "Copy of guide.pdf": { status: "duplicate", existingDocumentName: "Guide.pdf" },
      },
    },
  },
});

test("T-404 new, update and duplicate files upload only the new file and the confirmed update", async ({
  app,
  page,
  stub,
}) => {
  await app.openShell(DOCUMENTS);
  await openPanel(page);
  await choose(page, ["New guide.pdf", "Leave policy.pdf", "Copy of guide.pdf"]);
  await expect(page.getByText(en["upload.result.new"], { exact: true })).toBeVisible();
  await expect(page.getByText(en["upload.result.update"])).toBeVisible();
  await expect(
    page.getByText(fill(en["upload.result.duplicate"], { existingName: "Guide.pdf" })),
  ).toBeVisible();
  await expectAccessible(page, "the upload panel's preflight results");
  await page
    .getByRole("checkbox", {
      name: fill(en["upload.result.updateLabel"], { name: "Leave policy.pdf" }),
    })
    .check();
  await page.getByRole("button", { name: fill(en["upload.start"].other, { count: 2 }) }).click();
  await expect(page.getByText(en["upload.complete.title"])).toBeVisible();

  const reserved = await stub.requests("adminReserveUpload");
  expect(reserved.map((call) => (call.body as { name: string }).name)).toEqual([
    "New guide.pdf",
    "Leave policy.pdf",
  ]);
  expect((reserved[1]?.body as { confirmUpdate: boolean }).confirmUpdate).toBe(true);
  const contents = await stub.requests("adminUploadContent");
  expect(contents.map((call) => call.contentType)).toEqual([
    "application/octet-stream",
    "application/octet-stream",
  ]);
  // One file at a time: the second reservation follows the first file's bytes.
  expect(reserved[1]?.at ?? 0).toBeGreaterThanOrEqual(contents[0]?.at ?? 0);
  const [submit] = await stub.requests("adminSubmitUploads");
  expect((submit?.body as { versionIds: string[] }).versionIds).toHaveLength(2);
  expect(contents.map((call) => call.path.split("/")[6])).toEqual(
    (submit?.body as { versionIds: string[] }).versionIds,
  );
});

test("T-405 a 503 is retried after 2,000 and 6,000 ms, and 429 too_many_requests waits its retryAfterSeconds", async ({
  app,
  page,
  stub,
}) => {
  await stub.problem("adminReserveUpload", { code: "too_many_requests", retryAfterSeconds: 1 });
  await stub.problem("adminUploadContent", { code: "dependency_unavailable" }, { times: 2 });
  await app.openShell(DOCUMENTS);
  await openPanel(page);
  await choose(page, ["New guide.pdf"]);
  await page.getByRole("button", { name: fill(en["upload.start"].one, {}) }).click();
  await expect(page.getByText(en["upload.complete.title"])).toBeVisible({ timeout: 20_000 });
  const reserves = await stub.requests("adminReserveUpload");
  expect(reserves).toHaveLength(2);
  expect((reserves[1]?.at ?? 0) - (reserves[0]?.at ?? 0)).toBeGreaterThanOrEqual(950);
  const contents = await stub.requests("adminUploadContent");
  expect(contents).toHaveLength(3);
  expect((contents[1]?.at ?? 0) - (contents[0]?.at ?? 0)).toBeGreaterThanOrEqual(1950);
  expect((contents[2]?.at ?? 0) - (contents[1]?.at ?? 0)).toBeGreaterThanOrEqual(5950);
});

test("T-405 a fourth consecutive 429 from adminReserveUpload fails the file", async ({
  app,
  page,
  stub,
}) => {
  await stub.problem(
    "adminReserveUpload",
    { code: "too_many_requests", retryAfterSeconds: 1 },
    { times: 4 },
  );
  await app.openShell(DOCUMENTS);
  await openPanel(page);
  await choose(page, ["New guide.pdf"]);
  await page.getByRole("button", { name: fill(en["upload.start"].one, {}) }).click();
  await expect(page.getByText(/^Not uploaded\./)).toBeVisible({ timeout: 20_000 });
  expect(await stub.requests("adminReserveUpload")).toHaveLength(4);
  expect(await stub.requests("adminUploadContent")).toHaveLength(0);
});

test("T-407 503 malware_scanning_not_ready pauses the batch with its time and sends nothing more", async ({
  app,
  page,
  stub,
}) => {
  await stub.problem("adminPreflightUploads", {
    code: "malware_scanning_not_ready",
    retryAfterSeconds: 3600,
  });
  await app.openShell(DOCUMENTS);
  await openPanel(page);
  await choose(page, ["New guide.pdf"]);
  await expect(page.getByText("Uploads paused")).toBeVisible();
  await expect(
    page.getByText("Uploads are paused until malware scanning confirms it's working. Retry later."),
  ).toBeVisible();
  await expect(page.getByText(/^You can retry at /)).toBeVisible();
  await expect(page.getByText(en["error.server.title"])).toHaveCount(0);
  await expectAccessible(page, "uploads paused at preflight");
});

test("T-407 malware_scanning_not_ready mid-batch marks the current and remaining files paused", async ({
  app,
  page,
  stub,
}) => {
  await stub.problem("adminUploadContent", {
    code: "malware_scanning_not_ready",
    retryAfterSeconds: 3600,
  });
  await app.openShell(DOCUMENTS);
  await openPanel(page);
  await choose(page, ["First.pdf", "Second.pdf"]);
  await page.getByRole("button", { name: fill(en["upload.start"].other, { count: 2 }) }).click();
  await expect(page.getByText(en["upload.filePaused"])).toHaveCount(2);
  await expect(
    page.getByRole("button", { name: fill(en["upload.start"].other, { count: 2 }) }),
  ).toBeDisabled();
  expect(await stub.requests("adminUploadContent")).toHaveLength(1);
  expect(await stub.requests("adminSubmitUploads")).toHaveLength(0);
});

test.describe(() => {
  test.use({ world: { user: adminUser(), admin: mappedTo(3) } });

  test("T-418 the panel names every project mapped to the index above Upload", async ({
    app,
    page,
    stub,
  }) => {
    await app.openShell(DOCUMENTS);
    await uploadThrough(page, "Employee handbook");
    // A mapping added after the Documents screen loaded is named when the panel opens.
    await stub.update({ admin: mappedTo(4) });
    await openPanel(page);
    await choose(page, ["New guide.pdf"]);
    await expect(
      page.getByText(
        fill(en["upload.sharedIndex"].other, {
          count: 4,
          projects: "Employee handbook, Project 2, Project 3, Project 4",
        }),
      ),
    ).toBeVisible();
  });

  test("T-418 with the index refetch failing the bar names no project and Upload still works", async ({
    app,
    page,
    stub,
  }) => {
    await app.openShell(DOCUMENTS);
    await uploadThrough(page, "Employee handbook");
    await stub.problem("adminGetIndex", { code: "dependency_unavailable" }, { times: 3 });
    await openPanel(page);
    await choose(page, ["New guide.pdf"]);
    await expect(page.getByText(en["upload.sharedIndex.unknown"])).toBeVisible();
    await page.getByRole("button", { name: fill(en["upload.start"].one, {}) }).click();
    await expect(page.getByText(en["upload.complete.title"])).toBeVisible();
  });
});

test.describe(() => {
  test.use({ world: { user: adminUser(), admin: mappedTo(12) } });

  test("T-418 twelve mapped projects show the first ten and how many more", async ({
    app,
    page,
  }) => {
    await app.openShell(DOCUMENTS);
    await uploadThrough(page, "Employee handbook");
    await openPanel(page);
    await choose(page, ["New guide.pdf"]);
    const names = [
      "Employee handbook",
      ...Array.from({ length: 9 }, (_, index) => `Project ${String(index + 2)}`),
    ].join(", ");
    await expect(
      page.getByText(
        fill(en["upload.sharedIndex"].other, {
          count: 12,
          projects: `${names} ${fill(en["upload.sharedIndex.more"], { count: 2 })}`,
        }),
      ),
    ).toBeVisible();
  });
});
