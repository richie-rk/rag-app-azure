// The edit conflict browser test (T-397; 10-web-client.md section 7.4, acceptance criterion 47):
// a `412` on Save changes keeps the typed values, shows the latest saved value under each changed
// field, and the next save succeeds with the new `rowVersion`.

import { IDS, adminUser, rowVersion } from "../../fixtures/web/stub-api/builders.ts";
import { en, expect, expectAccessible, fill, test } from "../../fixtures/web/test.ts";

test.use({ world: { user: adminUser() } });

test("T-397 a 412 on Save changes keeps the typed values and the next save succeeds", async ({
  app,
  page,
  stub,
}) => {
  await app.openShell(`/admin/projects/${IDS.handbook}`);
  await expect(app.heading()).toContainText("Employee handbook");
  const description = page.getByRole("textbox", { name: en["project.form.description"] });
  await expect(description).toHaveValue("Policies for leave, travel and expenses.");
  await expectAccessible(page, "the project's Details tab");
  await description.fill("Policies for leave and travel");

  // Another administrator saves the project meanwhile.
  const snapshot = await stub.snapshot();
  const latestVersion = rowVersion(7777);
  await stub.update({
    admin: {
      projects: snapshot.admin.projects.map((project) =>
        project.projectId === IDS.handbook
          ? { ...project, description: "Changed by Sam", rowVersion: latestVersion }
          : project,
      ),
    },
  });

  const save = page.getByRole("button", { name: en["project.save"] });
  await save.click();
  await expect(page.getByText("Changed by someone else")).toBeVisible();
  await expect(page.getByText(en["admin.conflict.body"])).toBeVisible();
  await expect(
    page.getByText(fill(en["admin.conflict.latestValue"], { value: "Changed by Sam" })),
  ).toBeVisible();
  await expect(page.getByRole("button", { name: en["admin.conflict.useLatest"] })).toHaveCount(1);
  await expect(description).toHaveValue("Policies for leave and travel");
  await expectAccessible(page, "an edit conflict on the Details tab");

  await save.click();
  await expect(page.getByText(en["project.savedToast"])).toBeVisible();
  const patches = await stub.requests("adminUpdateProject");
  expect(patches).toHaveLength(2);
  expect(patches[1]?.headers["if-match"]).toBe(`"${latestVersion}"`);
  expect(patches[1]?.body).toEqual({ description: "Policies for leave and travel" });
  const saved = await stub.snapshot();
  expect(
    saved.admin.projects.find((project) => project.projectId === IDS.handbook)?.description,
  ).toBe("Policies for leave and travel");
});
