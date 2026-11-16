// The memory concurrency browser test (T-395; 10-web-client.md section 7.3, acceptance criterion
// 45): two tabs edit one fact; the second save meets `412`, shows `memory.conflict` and keeps its
// text.

import { en, expect, expectAccessible, fill, test } from "../../fixtures/web/test.ts";

const FACT = "Works on the finance team";

test("T-395 two tabs editing one fact: the second save gets 412 and keeps its text", async ({
  app,
  page,
  context,
  stub,
}) => {
  await app.openShell("/memory");
  const other = await context.newPage();
  await other.goto("/memory");
  await expect(other.getByRole("heading", { level: 1 })).toHaveText(en["memory.title"]);

  const editName = fill(en["memory.editName"], { fact: FACT });
  await other.getByRole("button", { name: editName }).click();
  const otherEditor = other.getByRole("textbox", { name: en["memory.editLabel"] });
  await otherEditor.fill("Works on the tax team");

  await page.getByRole("button", { name: editName }).click();
  await page.getByRole("textbox", { name: en["memory.editLabel"] }).fill("Leads the finance team");
  await page.getByRole("button", { name: en["memory.save"] }).click();
  await expect(page.getByText("Leads the finance team")).toBeVisible();

  await other.getByRole("button", { name: en["memory.save"] }).click();
  await expect(other.getByText(en["memory.conflict"])).toBeVisible();
  await expect(otherEditor).toHaveValue("Works on the tax team");
  const saves = await stub.requests("updateMemoryFact");
  expect(saves).toHaveLength(2);
  expect(saves[0]?.headers["if-match"]).toBe(saves[1]?.headers["if-match"]);
  await expectAccessible(other, "the second tab after its stale save");
});
