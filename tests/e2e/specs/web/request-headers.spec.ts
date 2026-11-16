// The request headers browser test (T-358; 10-web-client.md sections 5.3 and 5.4, acceptance
// criteria 16 and 63): every API request carries a valid `traceparent` and the bearer token, the
// stream and the downloads included; `config.json` and Entra ID requests carry neither; the page
// talks only to the site, the API and Entra ID.

import { IDS, adminUser, citation, session, turn } from "../../fixtures/web/stub-api/builders.ts";
import { API_ORIGIN, APP_ORIGIN } from "../../fixtures/web/stub-api/protocol.ts";
import { LOGIN_ORIGIN } from "../../fixtures/web/identity.ts";
import { en, expect, fill, test, type App } from "../../fixtures/web/test.ts";
import type { StubApi } from "../../fixtures/web/stubApi.ts";

const TRACEPARENT = /^00-([0-9a-f]{32})-([0-9a-f]{16})-01$/;

const STORED = session({
  title: "Leave",
  turns: [turn({ question: "How much leave?", citations: [citation(1, "Leave policy", 4)] })],
});

/** Every API request the stub received carried a valid `traceparent` and this world's token,
 * and no two shared a trace ID. */
async function expectSignedAndTraced(stub: StubApi): Promise<string[]> {
  const calls = await stub.requests();
  const traceIds = calls.map((call) => {
    const match = TRACEPARENT.exec(call.headers["traceparent"] ?? "");
    expect(match, `${call.method} ${call.path}`).not.toBeNull();
    expect(match?.[1]).not.toMatch(/^0+$/);
    expect(match?.[2]).not.toMatch(/^0+$/);
    expect(call.headers["authorization"], `${call.method} ${call.path}`).toMatch(
      new RegExp(`^Bearer stub-at\\.${stub.worldId}\\.\\d+$`),
    );
    return match?.[1] ?? "";
  });
  expect(new Set(traceIds).size).toBe(traceIds.length);
  return calls.map((call) => call.operation ?? "");
}

async function chatAndDownloads(app: App): Promise<void> {
  const page = app.page;
  await app.openShell(`/chat/${STORED.sessionId}`);
  const question = "And how much carries over?";
  await app.ask(question);
  await expect(app.sources(app.turn(question))).toBeVisible();
  await app
    .sources(app.turn("How much leave?"))
    .getByRole("button", { name: "Leave policy, page 4" })
    .click();
  const viewer = page.getByRole("complementary", { name: fill(en["citation.title"], { n: 1 }) });
  if (await page.evaluate(() => navigator.pdfViewerEnabled)) {
    await expect(viewer.locator("iframe")).toHaveAttribute("src", /^blob:/);
  } else {
    const saved = page.waitForEvent("download");
    await viewer.getByRole("button", { name: en["citation.downloadPage"] }).click();
    await saved;
  }
  const document = page.waitForEvent("download");
  await viewer.getByRole("button", { name: en["citation.downloadDocument"] }).click();
  await document;
}

test.use({ world: { sessions: [STORED] } });

test("T-358 API requests, the stream and the downloads carry traceparent and the token; config.json and Entra ID don't", async ({
  app,
  context,
  stub,
  gates,
}) => {
  const outbound: { url: string; headers: Record<string, string> }[] = [];
  context.on("request", (request) => {
    outbound.push({ url: request.url(), headers: request.headers() });
  });
  await chatAndDownloads(app);
  const operations = await expectSignedAndTraced(stub);
  expect(operations).toEqual(
    expect.arrayContaining([
      "getCurrentUser",
      "createChatTurn",
      "getCitation",
      "getCitationContent",
      "getCitationDocument",
    ]),
  );
  const unsigned = outbound.filter(
    (request) =>
      request.url === `${APP_ORIGIN}/config.json` || request.url.startsWith(LOGIN_ORIGIN),
  );
  expect(unsigned.some((request) => request.url.endsWith("/config.json"))).toBe(true);
  expect(unsigned.some((request) => request.url.startsWith(LOGIN_ORIGIN))).toBe(true);
  for (const request of unsigned) {
    expect(request.headers["traceparent"], request.url).toBeUndefined();
    expect(request.headers["authorization"], request.url).toBeUndefined();
  }
  const origins = new Set(
    gates.requests.map((request) => new URL(request.split(" ")[1] ?? "").origin),
  );
  expect([...origins].sort()).toEqual([API_ORIGIN, APP_ORIGIN, LOGIN_ORIGIN].sort());
});

test.describe(() => {
  test.use({ world: { sessions: [STORED], user: adminUser() } });

  test("T-358 every upload request carries traceparent and the token", async ({
    app,
    page,
    stub,
  }) => {
    await app.openShell(`/admin/indexes/${IDS.handbookIndex}/documents`);
    await page.getByRole("button", { name: en["documents.upload"] }).click();
    await page.locator("input[type=file]").setInputFiles({
      name: "Handbook.pdf",
      mimeType: "application/pdf",
      buffer: Buffer.from("%PDF-1.4\n%stub\n"),
    });
    await page.getByRole("button", { name: /^Upload 1 file$/ }).click();
    await expect(page.getByText(en["upload.complete.title"])).toBeVisible();
    const operations = await expectSignedAndTraced(stub);
    expect(operations).toEqual(
      expect.arrayContaining([
        "adminPreflightUploads",
        "adminReserveUpload",
        "adminUploadContent",
        "adminSubmitUploads",
      ]),
    );
  });
});
