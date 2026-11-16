// The keyboard browser test (T-409; 10-web-client.md sections 11.3 and 11.4, acceptance criterion
// 57): with the keyboard only, skip to main content, open a session, send with Enter, stop with
// Escape, open a citation with Enter, close it with Escape with focus returned, give feedback.

import type { Locator, Page } from "@playwright/test";

import { citation, session, turn } from "../../fixtures/web/stub-api/builders.ts";
import { en, expect, expectAccessible, fill, test } from "../../fixtures/web/test.ts";

const STORED_QUESTION = "How much leave do I get?";
const STORED = session({
  title: "Leave questions",
  turns: [turn({ question: STORED_QUESTION, citations: [citation(1, "Leave policy", 4)] })],
});

/** Presses Tab (or Shift+Tab) until `target` has focus. */
async function tabTo(page: Page, target: Locator, backwards = false): Promise<void> {
  for (let presses = 0; presses < 80; presses += 1) {
    if (await target.evaluate((element) => element === document.activeElement)) return;
    await page.keyboard.press(backwards ? "Shift+Tab" : "Tab");
  }
  throw new Error(`Tab never reached ${String(target)}`);
}

test.use({ world: { sessions: [STORED] } });

test("T-409 the keyboard alone skips to main, opens a session, sends, stops, opens a citation and gives feedback", async ({
  app,
  page,
  stub,
}) => {
  await app.openShell("/chat");

  // The skip link is the first focusable element: backwards from the screen's h1 it is the last
  // stop before focus leaves the page, and Tab from there comes back to it.
  const skip = page.getByRole("link", { name: en["shell.skipLink"] });
  await tabTo(page, skip, true);
  await page.keyboard.press("Shift+Tab");
  expect(await page.evaluate(() => document.activeElement === document.body)).toBe(true);
  await page.keyboard.press("Tab");
  await expect(skip).toBeFocused();
  await expectAccessible(page, "the skip link on focus");
  await page.keyboard.press("Enter");
  await expect(page.locator("main#main")).toBeFocused();

  // Open the session from the session panel.
  await tabTo(page, app.sessionPanel().getByRole("link", { name: /Leave questions/ }));
  await page.keyboard.press("Enter");
  await expect(page).toHaveURL(new RegExp(`/chat/${STORED.sessionId}$`));
  await expect(app.heading()).toBeFocused();

  // Send with Enter, then stop with Escape.
  await stub.stream({ hold: "beforeCitations" });
  await tabTo(page, app.composer());
  const question = "Does unused leave carry over?";
  await page.keyboard.type(question);
  await page.keyboard.press("Enter");
  await expect(app.turn(question).getByText(/25 days of annual leave/)).toBeVisible();
  await expect(app.composer()).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(
    app
      .turn(question)
      .getByText(en["chat.turn.stopped"])
      .or(app.turn(question).getByText(en["chat.turn.stoppedStored"])),
  ).toBeVisible();

  // Open a citation with Enter; Escape closes it and focus returns to its button.
  const source = app.sources(app.turn(STORED_QUESTION)).getByRole("button", {
    name: "Leave policy, page 4",
  });
  await tabTo(page, source, true);
  await page.keyboard.press("Enter");
  const viewer = page.getByRole("complementary", { name: fill(en["citation.title"], { n: 1 }) });
  await expect(
    viewer.getByRole("heading", { name: fill(en["citation.title"], { n: 1 }) }),
  ).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(viewer).toHaveCount(0);
  await expect(source).toBeFocused();

  // Give feedback with the keyboard.
  const helpful = app
    .turn(STORED_QUESTION)
    .getByRole("button", { name: en["chat.turn.helpful"], exact: true });
  await tabTo(page, helpful);
  await page.keyboard.press("Space");
  await expect(helpful).toHaveAttribute("aria-pressed", "true");
  const notHelpful = app
    .turn(STORED_QUESTION)
    .getByRole("button", { name: en["chat.turn.notHelpful"], exact: true });
  await tabTo(page, notHelpful);
  await page.keyboard.press("Enter");
  const dialog = page.getByRole("dialog", { name: en["feedback.title"] });
  await expect(dialog).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(dialog).toHaveCount(0);
  await expect(notHelpful).toBeFocused();
});
