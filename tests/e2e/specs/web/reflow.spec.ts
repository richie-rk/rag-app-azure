// The reflow browser test (T-411; 10-web-client.md sections 8.1 and 11.1, acceptance criterion
// 59): at 320 CSS px wide no screen scrolls horizontally (tables scroll inside their own regions),
// and at 200% zoom, a 1,280 px window showing 640 CSS px, no text is clipped.

import type { Page } from "@playwright/test";

import { adminUser, citation, session, turn } from "../../fixtures/web/stub-api/builders.ts";
import { en, expect, expectAccessible, test, type App } from "../../fixtures/web/test.ts";

const WIDE_TABLE = [
  "The allowances differ by grade ⟦1⟧:",
  "",
  "| Grade | Annual leave | Carry-over | Sick leave | Parental leave | Study leave | Notes |",
  "|---|---|---|---|---|---|---|",
  "| Junior | 25 days | 5 days | 10 days | 26 weeks | 5 days | Pro rata for part-time staff |",
  "| Senior | 28 days | 8 days | 12 days | 26 weeks | 8 days | Includes one wellbeing day |",
].join("\n");

const STORED = session({
  title: "A very long conversation title that has to be cut with an ellipsis in the panel",
  turns: [
    turn({
      question: "How do the leave allowances differ between grades and what carries over?",
      answer: WIDE_TABLE,
      citations: [citation(1, "Leave policy", 4)],
    }),
  ],
});

/** Text an element cuts off: it hides its overflow, holds text of its own, isn't visually hidden
 * (1 px) and isn't an intended one-line ellipsis, yet its content is larger than its box. */
function clippedText(page: Page): Promise<string[]> {
  return page.evaluate(() => {
    const found: string[] = [];
    for (const element of document.querySelectorAll("body *")) {
      const style = getComputedStyle(element);
      if (style.display === "none" || style.visibility === "hidden") continue;
      const box = element.getBoundingClientRect();
      if (box.width <= 1 || box.height <= 1) continue;
      const hidesX = style.overflowX === "hidden" || style.overflowX === "clip";
      const hidesY = style.overflowY === "hidden" || style.overflowY === "clip";
      if (!hidesX && !hidesY) continue;
      if (style.textOverflow === "ellipsis") continue;
      const ownText = [...element.childNodes].some(
        (node) => node.nodeType === Node.TEXT_NODE && (node.textContent ?? "").trim() !== "",
      );
      if (!ownText) continue;
      if (
        (hidesY && element.scrollHeight > element.clientHeight + 1) ||
        (hidesX && element.scrollWidth > element.clientWidth + 1)
      ) {
        found.push(`${element.tagName.toLowerCase()} "${element.textContent.slice(0, 40)}"`);
      }
    }
    return found;
  });
}

function scrollsSideways(page: Page): Promise<boolean> {
  return page.evaluate(
    () => document.documentElement.scrollWidth > document.documentElement.clientWidth,
  );
}

async function screens(app: App): Promise<void> {
  const page = app.page;
  const checks: readonly (readonly [string, () => Promise<void>])[] = [
    [
      "a session with a wide table",
      async () => {
        await page.goto(`/chat/${STORED.sessionId}`);
        await app.waitForShell();
        await expect(app.sources(app.turn(STORED.turns[0]?.question ?? ""))).toBeVisible();
        await expect(page.getByRole("region", { name: "Table 1" })).toBeVisible();
      },
    ],
    [
      "a new chat",
      async () => {
        await page.goto("/chat");
        await app.waitForShell();
        await expect(app.composer()).toBeVisible();
      },
    ],
    [
      "Memory",
      async () => {
        await page.goto("/memory");
        await app.waitForShell();
        await expect(page.getByRole("list", { name: en["memory.list"] })).toBeVisible();
      },
    ],
    [
      "Projects",
      async () => {
        await page.goto("/admin/projects");
        await app.waitForShell();
        await expect(page.getByRole("table")).toBeVisible();
      },
    ],
  ];
  const width = page.viewportSize()?.width ?? 0;
  for (const [name, open] of checks) {
    await open();
    expect(await scrollsSideways(page), `${name} at ${String(width)} px scrolls sideways`).toBe(
      false,
    );
    if (width >= 640) expect(await clippedText(page), `${name}: clipped text`).toEqual([]);
    await expectAccessible(page, `${name} at ${String(width)} px`);
  }
}

test.use({ world: { sessions: [STORED], user: adminUser() } });

test("T-411 at 320 px wide no screen scrolls horizontally", async ({ app, page }) => {
  await page.setViewportSize({ width: 320, height: 640 });
  await app.openShell("/chat");
  await screens(app);
});

test("T-411 at 200% zoom no text is clipped and nothing scrolls sideways", async ({
  app,
  page,
}) => {
  // A 1,280 x 720 window at 200% zoom lays the page out in 640 x 360 CSS px.
  await page.setViewportSize({ width: 640, height: 360 });
  await app.openShell("/chat");
  await screens(app);
});
