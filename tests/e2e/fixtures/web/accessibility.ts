// The accessibility gate of every screen state a browser test reaches (10-web-client.md sections
// 11.6 and 13.2): an axe scan with the WCAG 2.0, 2.1 and 2.2 A and AA tags, in the theme on screen
// and then in the other one, and no violation of any impact.

import AxeBuilder from "@axe-core/playwright";
import { expect, type Page } from "@playwright/test";

export const AXE_TAGS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"] as const;

type Scheme = "light" | "dark";

/** The theme's main background variable on the Fluent provider: it changes with the theme. */
function themeToken(page: Page): Promise<string> {
  return page.evaluate(() => {
    const provider = document.querySelector(".fui-FluentProvider");
    return provider === null
      ? ""
      : getComputedStyle(provider).getPropertyValue("--colorNeutralBackground1").trim();
  });
}

/** Waits until no finite CSS transition or animation is running (a theme change fades colours),
 * so colours are read at rest; spinners and skeletons animate without end and don't count, and a
 * finished animation that holds its last frame isn't running. */
async function settle(page: Page): Promise<void> {
  await expect
    .poll(
      () =>
        page.evaluate(
          () =>
            document
              .getAnimations()
              .filter(
                (animation) =>
                  animation.playState === "running" &&
                  animation.effect?.getComputedTiming().iterations !== Infinity,
              ).length,
        ),
      { timeout: 5000 },
    )
    .toBe(0);
}

async function scan(page: Page, label: string): Promise<void> {
  await settle(page);
  // Tabster's focus guards (`data-tabster-dummy`, Fluent's focus management) are focusable and
  // aria-hidden by design: focus that lands on one moves on at once, and they hold no content.
  const results = await new AxeBuilder({ page })
    .withTags([...AXE_TAGS])
    .exclude("[data-tabster-dummy]")
    .analyze();
  const found = results.violations.map(
    (violation) =>
      `${violation.id} (${violation.impact ?? "unrated"}): ${violation.help} at ` +
      violation.nodes
        .map(
          (node) =>
            `${node.target.join(" ")} [${(node.failureSummary ?? "").replace(/\s+/g, " ").trim()}]`,
        )
        .join(", "),
  );
  expect(found, `axe violations in ${label}`).toEqual([]);
}

/**
 * Scans the current screen state in both themes. The theme follows `prefers-color-scheme` unless
 * the page remembers a choice, so the second scan emulates the other scheme and waits for the
 * provider's tokens to change; the page's own scheme is restored afterwards.
 */
export async function expectAccessible(page: Page, state: string): Promise<void> {
  const prefersDark = await page.evaluate(
    () => window.matchMedia("(prefers-color-scheme: dark)").matches,
  );
  const current: Scheme = prefersDark ? "dark" : "light";
  const other: Scheme = prefersDark ? "light" : "dark";
  const before = await themeToken(page);
  if (before === "") {
    // A page without the Fluent provider is the static 404 page, which may carry no style at all
    // (section 3.6), so in the dark scheme the browser paints its canvas dark with light text.
    // axe reads a transparent page background as white and would compare light text with it, so
    // the page has only its light rendering to scan.
    await page.emulateMedia({ colorScheme: "light" });
    try {
      await scan(page, `${state}, light scheme`);
    } finally {
      await page.emulateMedia({ colorScheme: current });
    }
    return;
  }
  await scan(page, `${state}, ${current} scheme`);
  await page.emulateMedia({ colorScheme: other });
  try {
    await expect
      .poll(() => themeToken(page), { message: `${state}: the theme follows the ${other} scheme` })
      .not.toBe(before);
    await scan(page, `${state}, ${other} scheme`);
  } finally {
    await page.emulateMedia({ colorScheme: current });
  }
  await expect.poll(() => themeToken(page)).toBe(before);
}
