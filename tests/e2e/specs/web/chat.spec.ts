// The chat against the stub API's scripted streams (10-web-client.md sections 8.2 to 8.14):
// the chat end-to-end test (T-280), Retry (T-370), the turn-in-progress resend (T-371), a new
// chat's URL (T-373) and the broadened search (T-374).

import {
  DEFAULT_FOLLOWUPS,
  IDS,
  citation,
  citationFile,
  session,
  turn,
} from "../../fixtures/web/stub-api/builders.ts";
import { en, expect, expectAccessible, fill, test } from "../../fixtures/web/test.ts";

const NEW_CHAT = `/chat?project=${IDS.handbook}`;
const QUESTION = "How many days of leave do I get?";

test.describe("the chat end to end (T-280)", () => {
  test("T-280 a question streams deltas, citations and follow-ups; Stop leaves a cancelled turn", async ({
    app,
    page,
    stub,
  }) => {
    await app.openShell(NEW_CHAT);
    await expect(page.getByRole("heading", { name: en["chat.empty.title"] })).toBeVisible();
    await expectAccessible(page, "a new chat's empty state");

    await stub.stream({ hold: "beforeCitations" });
    await app.ask(QUESTION);
    const first = app.turn(QUESTION);
    await expect(first.getByText(/25 days of annual leave each year/)).toBeVisible();
    // Before `citations`, a token is plain `[n]` text and sending is blocked.
    await expect(first.getByText("[1]")).toBeVisible();
    await expect(app.stopButton()).toBeVisible();
    await expectAccessible(page, "an answer streaming");
    await stub.release();
    await expect(first.getByRole("button", { name: "Source 1: Leave policy" })).toBeVisible();
    await expect(
      app.sources(first).getByRole("button", { name: "Leave policy, page 4" }),
    ).toBeVisible();
    await expect(app.statusRegion()).toHaveText("Answer complete. 2 sources.");
    const followups = page.getByRole("group", { name: en["chat.followups.label"] });
    for (const text of DEFAULT_FOLLOWUPS) {
      await expect(followups.getByRole("button", { name: text })).toBeVisible();
    }
    await expect(app.sendButton()).toBeVisible();
    await expectAccessible(page, "a completed answer with sources and follow-ups");

    // A follow-up sends its text as the next question; Stop ends that answer early.
    await stub.stream({ hold: "beforeCitations" });
    await followups.getByRole("button", { name: DEFAULT_FOLLOWUPS[0] }).click();
    const second = app.turn(DEFAULT_FOLLOWUPS[0]);
    await expect(second.getByText(/25 days of annual leave/)).toBeVisible();
    await app.stopButton().click();
    // The live caption, or the stored one once the refetched session replaces the live turn.
    await expect(
      second.getByText(en["chat.turn.stopped"]).or(second.getByText(en["chat.turn.stoppedStored"])),
    ).toBeVisible();
    await expect(app.statusRegion()).toHaveText(en["chat.announce.stopped"]);
    await expect(second.getByRole("button", { name: en["chat.turn.retry"] })).toBeVisible();
    await expect
      .poll(async () => {
        const snapshot = await stub.snapshot();
        return snapshot.sessions[0]?.turns.map((item) => item.status);
      })
      .toEqual(["completed", "cancelled"]);
    await expectAccessible(page, "a stopped answer");
  });

  test.describe(() => {
    test.use({
      world: {
        sessions: [
          session({
            title: "Leave and travel",
            turns: [
              turn({
                question: QUESTION,
                citations: [
                  citation(1, "Leave policy", 4, "pdf"),
                  citation(2, "Travel guide", 2, "docx"),
                ],
                files: {
                  "1": citationFile("Leave policy, page 4.pdf", "Leave policy.pdf"),
                  "2": citationFile("Travel guide, page 2.pdf", "Travel guide.pdf"),
                },
              }),
            ],
          }),
        ],
      },
    });

    test("T-280 PDF and DOCX citations show their cited page, and Download full document saves <stem>.pdf", async ({
      app,
      page,
      world,
    }) => {
      const sessionId = world.sessions?.[0]?.sessionId ?? "";
      await app.openShell(`/chat/${sessionId}`);
      const stored = app.turn(QUESTION);
      const viewerEnabled = await page.evaluate(() => navigator.pdfViewerEnabled);
      for (const [number, title, page_, type] of [
        [1, "Leave policy", 4, en["citation.type.pdf"]],
        [2, "Travel guide", 2, en["citation.type.docx"]],
      ] as const) {
        await app
          .sources(stored)
          .getByRole("button", { name: `${title}, page ${String(page_)}` })
          .click();
        const viewer = page.getByRole("complementary", {
          name: fill(en["citation.title"], { n: number }),
        });
        await expect(viewer).toBeVisible();
        if (viewerEnabled) {
          const frame = viewer.locator("iframe");
          await expect(frame).toHaveAttribute(
            "title",
            fill(en["citation.frame"], { n: number, title, locator: `page ${String(page_)}` }),
          );
          await expect(frame).toHaveAttribute("src", /^blob:/);
        } else {
          await expect(viewer.getByText(type)).toBeVisible();
          await expect(viewer.getByText(en["citation.noPdfViewer"])).toBeVisible();
        }
        await expectAccessible(page, `citation ${String(number)} open`);
      }
      const download = page.waitForEvent("download");
      await page.getByRole("button", { name: en["citation.downloadDocument"] }).click();
      expect((await download).suggestedFilename()).toBe("Travel guide.pdf");
    });
  });

  test("T-280 a stream cut at every byte renders the same answer", async ({ app, stub }) => {
    const answer = "Café ☕ costs 3 € — enjoy 😀 ⟦1⟧. Señor Müller agrees ⟦2⟧.";
    await stub.stream({ mode: "byteSplit", answer });
    await app.openShell(NEW_CHAT);
    await app.ask(QUESTION);
    const done = app.turn(QUESTION);
    await expect(done.getByText(/Café ☕ costs 3 € — enjoy 😀/)).toBeVisible();
    await expect(done.getByText(/Señor Müller agrees/)).toBeVisible();
    await expect(app.sources(done)).toBeVisible();
  });

  test("T-280 an event of an unknown type is skipped", async ({ app, stub }) => {
    await stub.stream({ mode: "unknownType" });
    await app.openShell(NEW_CHAT);
    await app.ask(QUESTION);
    const done = app.turn(QUESTION);
    await expect(app.sources(done)).toBeVisible();
    await expect(done.getByText(en["chat.error.unreadable"])).toHaveCount(0);
  });
});

test("T-370 Retry sends a new clientTurnId with the same question and session", async ({
  app,
  page,
  stub,
}) => {
  await stub.stream({ mode: "error", error: { code: "model_unavailable", retryable: true } });
  await app.openShell(NEW_CHAT);
  await app.ask(QUESTION);
  const failed = app.turn(QUESTION);
  await expect(failed.getByText("Answer service unavailable")).toBeVisible();
  await expect(app.statusRegion()).toHaveText(en["chat.announce.failed"]);
  await expectAccessible(page, "a failed answer");
  await failed.getByRole("button", { name: en["chat.turn.retry"] }).click();
  await expect(app.turn(QUESTION)).toHaveCount(2);
  await expect(app.sources(app.turn(QUESTION).last())).toBeVisible();
  // The failed turn stays above the new one.
  await expect(app.turn(QUESTION).first().getByText("Answer service unavailable")).toBeVisible();
  const sends = await stub.requests("createChatTurn");
  expect(sends).toHaveLength(2);
  const [original, retried] = sends.map(
    (item) => item.body as { clientTurnId: string; question: string; sessionId?: string },
  );
  const snapshot = await stub.snapshot();
  expect(retried?.question).toBe(original?.question);
  expect(retried?.clientTurnId).not.toBe(original?.clientTurnId);
  expect(retried?.sessionId).toBe(snapshot.sessions[0]?.sessionId);
});

test("T-371 409 turn_in_progress soon after cited is resent after 1,000 ms with the same clientTurnId", async ({
  app,
  stub,
}) => {
  await app.openShell(NEW_CHAT);
  await app.ask(QUESTION);
  await expect(app.sources(app.turn(QUESTION))).toBeVisible();
  await stub.problem("createChatTurn", { code: "turn_in_progress", retryAfterSeconds: 5 });
  const next = "Who approves travel?";
  await app.ask(next);
  await expect(app.sources(app.turn(next))).toBeVisible();
  await expect(app.turn(next).getByText(en["chat.error.title"])).toHaveCount(0);
  const sends = (await stub.requests("createChatTurn")).slice(1);
  expect(sends).toHaveLength(2);
  const ids = sends.map((item) => (item.body as { clientTurnId: string }).clientTurnId);
  expect(ids[0]).toBe(ids[1]);
  expect((sends[1]?.at ?? 0) - (sends[0]?.at ?? 0)).toBeGreaterThanOrEqual(950);
});

test.describe(() => {
  test.use({
    world: {
      sessions: [
        session({
          title: "An older chat",
          turns: [turn({ question: "What is the travel policy?" })],
          ageSeconds: 86400,
        }),
      ],
    },
  });

  test("T-373 a new chat takes /chat/<sessionId> after meta and tops the session list", async ({
    app,
    page,
    stub,
  }) => {
    await app.openShell(NEW_CHAT);
    await app.ask(QUESTION);
    await expect(page).toHaveURL(/\/chat\/[0-9a-f-]{36}$/);
    // Taking the session's URL isn't an arrival: focus stays in the composer (section 11.4).
    await expect(app.composer()).toBeFocused();
    const snapshot = await stub.snapshot();
    const created = snapshot.sessions[0];
    expect(new URL(page.url()).pathname).toBe(`/chat/${created?.sessionId ?? ""}`);
    const links = app.sessionPanel().getByRole("link");
    await expect(links.first()).toContainText(QUESTION);
    await expect(links.first()).toHaveAttribute("aria-current", "page");
    await expect(links.nth(1)).toContainText("An older chat");
  });
});

test("T-374 search_broadened shows and announces its text and never names the technique", async ({
  app,
  page,
  stub,
}) => {
  await stub.stream({ mode: "broadened", hold: "afterStatus" });
  await app.openShell(NEW_CHAT);
  await app.ask(QUESTION);
  const live = app.turn(QUESTION);
  await expect(live.getByText(en["chat.turn.broadened"], { exact: true })).toBeVisible();
  await expect(app.statusRegion()).toHaveText(en["chat.turn.broadened"]);
  expect(await page.locator("body").innerText()).not.toMatch(
    /passage|hypothetical|embedding|vector|rerank|rewrit|semantic/i,
  );
  await expectAccessible(page, "the broadened search line");
  await stub.release();
  await expect(app.sources(live)).toBeVisible();
  await expect(live.getByText(en["chat.turn.broadened"], { exact: true })).toHaveCount(0);
});
