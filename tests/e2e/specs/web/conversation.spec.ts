// The conversation browser tests (T-333; 13-conversations-and-memory.md criterion 51, and
// 10-web-client.md sections 7.3, 8.12 and 8.16) and the feedback browser test (T-393).

import {
  IDS,
  citation,
  defaultMemory,
  session,
  turn,
} from "../../fixtures/web/stub-api/builders.ts";
import { en, expect, expectAccessible, fill, test } from "../../fixtures/web/test.ts";

const QUESTIONS = [
  "How many days of leave do I get?",
  "Where is the leave form?",
  "Does this apply to part-time staff?",
] as const;

test("T-333 after three questions and a reload, the chat shows the turns, feedback and the newest follow-ups, with links only for allowedLinks", async ({
  app,
  page,
  stub,
}) => {
  await stub.stream({ followups: ["What about public holidays?"] });
  await stub.stream({
    answer: "The form is at https://docs.example/a and not at https://docs.example/b ⟦1⟧.",
    citations: [citation(1, "Leave form", 1)],
    allowedLinks: ["https://docs.example/a"],
    followups: ["Who signs the form?"],
  });
  await stub.stream({ followups: ["What about contractors?"] });
  await app.openShell(`/chat?project=${IDS.handbook}`);
  for (const question of QUESTIONS) {
    await app.ask(question);
    await expect(app.sources(app.turn(question))).toBeVisible();
  }
  const helpful = app
    .turn(QUESTIONS[0])
    .getByRole("button", { name: en["chat.turn.helpful"], exact: true });
  await helpful.click();
  await expect(helpful).toHaveAttribute("aria-pressed", "true");

  await page.reload();
  await app.waitForShell();
  for (const question of QUESTIONS) {
    await expect(app.sources(app.turn(question))).toBeVisible();
  }
  await expect(
    app.turn(QUESTIONS[0]).getByRole("button", { name: en["chat.turn.helpful"], exact: true }),
  ).toHaveAttribute("aria-pressed", "true");
  const followups = page.getByRole("group", { name: en["chat.followups.label"] });
  await expect(followups).toHaveCount(1);
  await expect(followups.getByRole("button")).toHaveText(["What about contractors?"]);
  const linked = app.turn(QUESTIONS[1]);
  await expect(linked.locator("a")).toHaveCount(1);
  await expect(linked.locator("a")).toHaveAttribute("href", "https://docs.example/a");
  await expect(linked.getByText(en["chat.turn.linkRemoved"], { exact: false })).toBeVisible();
  expect(await linked.innerText()).not.toContain("docs.example/b");
  await expectAccessible(page, "a reopened session of three turns");
});

const STORED = session({ title: "Leave", turns: [turn({ question: QUESTIONS[0] })] });

test.describe(() => {
  test.use({ world: { sessions: [STORED] } });

  test("T-333 feedback shows success only after 200, and an injected 503 shows the error with its correlation ID", async ({
    app,
    page,
    stub,
  }) => {
    const correlationId = "0123456789abcdef0123456789abcdef";
    await stub.problem("putTurnFeedback", { code: "dependency_unavailable", correlationId });
    await app.openShell(`/chat/${STORED.sessionId}`);
    const answer = app.turn(QUESTIONS[0]);
    const helpful = answer.getByRole("button", { name: en["chat.turn.helpful"], exact: true });
    await helpful.click();
    await expect(answer.getByText("Service unavailable")).toBeVisible();
    await expect(
      answer.getByText(fill(en["error.reference"], { id: correlationId })),
    ).toBeVisible();
    await expect(helpful).toHaveAttribute("aria-pressed", "false");
    await expectAccessible(page, "feedback that failed");
    await helpful.click();
    await expect(helpful).toHaveAttribute("aria-pressed", "true");
    await expect(app.statusRegion()).toHaveText(en["feedback.saved"]);
  });

  test("T-393 after a reload the turn shows Helpful pressed and Edit your comment", async ({
    app,
    page,
    stub,
  }) => {
    await app.openShell(`/chat/${STORED.sessionId}`);
    const answer = app.turn(QUESTIONS[0]);
    await answer.getByRole("button", { name: en["chat.turn.helpful"], exact: true }).click();
    await answer.getByRole("button", { name: en["feedback.addComment"] }).click();
    const dialog = page.getByRole("dialog", { name: en["feedback.titleUp"] });
    await expect(dialog).toBeVisible();
    await expect(dialog.getByRole("radiogroup")).toHaveCount(0);
    await expectAccessible(page, "the feedback dialog in up mode");
    await dialog
      .getByRole("textbox", { name: en["feedback.comment.label"] })
      .fill("  Clear and quick  ");
    await dialog.getByRole("button", { name: en["feedback.submit"] }).click();
    await expect(dialog).toHaveCount(0);
    await expect(answer.getByRole("button", { name: en["feedback.editComment"] })).toBeVisible();
    const sent = await stub.requests("putTurnFeedback");
    expect(sent.at(-1)?.body).toEqual({ rating: "up", category: null, comment: "Clear and quick" });

    await page.reload();
    await app.waitForShell();
    const reopened = app.turn(QUESTIONS[0]);
    await expect(
      reopened.getByRole("button", { name: en["chat.turn.helpful"], exact: true }),
    ).toHaveAttribute("aria-pressed", "true");
    await reopened.getByRole("button", { name: en["feedback.editComment"] }).click();
    await expect(
      page
        .getByRole("dialog", { name: en["feedback.titleUp"] })
        .getByRole("textbox", { name: en["feedback.comment.label"] }),
    ).toHaveValue("Clear and quick");
  });
});

test.describe("the memory screen (T-333)", () => {
  test("T-333 the memory screen edits, deletes, pauses, resumes and clears", async ({
    app,
    page,
    stub,
  }) => {
    await app.openShell("/memory");
    const list = page.getByRole("list", { name: en["memory.list"] });
    await expect(list.getByText("Works on the finance team")).toBeVisible();
    await expect(
      page.getByText(fill(en["memory.usage.facts"].other, { count: 2, max: 30 })),
    ).toBeVisible();
    await expectAccessible(page, "Memory with two facts");

    await page
      .getByRole("button", {
        name: fill(en["memory.editName"], { fact: "Works on the finance team" }),
      })
      .click();
    const editor = page.getByRole("textbox", { name: en["memory.editLabel"] });
    await expect(editor).toBeFocused();
    await editor.fill("Works on the finance and tax team");
    await expectAccessible(page, "a memory being edited");
    await page.getByRole("button", { name: en["memory.save"] }).click();
    await expect(list.getByText("Works on the finance and tax team")).toBeVisible();
    await expect(app.statusRegion()).toHaveText(en["memory.saved"]);
    const [save] = await stub.requests("updateMemoryFact");
    expect(save?.headers["if-match"]).toBe(defaultMemory().etag);
    expect(save?.body).toEqual({ text: "Works on the finance and tax team", category: "team" });

    const second = "Prefers short answers with bullet points";
    await page
      .getByRole("button", { name: fill(en["memory.deleteName"], { fact: second }) })
      .click();
    const confirm = page.getByRole("dialog", { name: en["memory.deleteConfirm.title"] });
    await expect(confirm).toBeVisible();
    await confirm.getByRole("button", { name: en["memory.deleteConfirm.action"] }).click();
    await expect(list.getByText(second)).toHaveCount(0);
    await expect(app.statusRegion()).toHaveText(en["memory.deleted"]);

    const toggle = page.getByRole("switch", { name: en["memory.toggle"] });
    await toggle.click();
    await expect(page.getByText(en["memory.pausedBar"])).toBeVisible();
    await expect(app.statusRegion()).toHaveText(en["memory.pausedDone"]);
    await expectAccessible(page, "memory paused");
    await toggle.click();
    await expect(page.getByText(en["memory.pausedBar"])).toHaveCount(0);
    await expect(app.statusRegion()).toHaveText(en["memory.resumedDone"]);

    await page.getByRole("button", { name: en["memory.clearAll"] }).click();
    const clear = page.getByRole("dialog", { name: en["memory.clearConfirm.title"] });
    await clear.getByRole("button", { name: en["memory.clearConfirm.action"] }).click();
    await expect(page.getByText(en["memory.empty"])).toBeVisible();
    await expect(page.getByRole("button", { name: en["memory.clearAll"] })).toBeDisabled();
    await expectAccessible(page, "memory cleared");
    const writes = await stub.requests();
    for (const call of writes.filter(
      (item) => item.method !== "GET" && item.path.startsWith("/v1/memory"),
    )) {
      expect(call.headers["if-match"], `${call.method} ${call.path}`).toMatch(/^"\d+"$/);
    }
  });

  test("T-333 a stale edit shows the refresh prompt and keeps the typed text", async ({
    app,
    page,
    stub,
  }) => {
    await stub.problem("updateMemoryFact", { code: "precondition_failed" });
    await app.openShell("/memory");
    await page
      .getByRole("button", {
        name: fill(en["memory.editName"], { fact: "Works on the finance team" }),
      })
      .click();
    const editor = page.getByRole("textbox", { name: en["memory.editLabel"] });
    await editor.fill("Leads the finance team");
    await page.getByRole("button", { name: en["memory.save"] }).click();
    await expect(page.getByText(en["memory.conflict"])).toBeVisible();
    await expect(page.getByText("Changed by someone else")).toBeVisible();
    await expect(editor).toHaveValue("Leads the finance team");
    expect((await stub.requests("getMemory")).length).toBeGreaterThan(1);
    await expectAccessible(page, "a stale memory edit");
  });
});
