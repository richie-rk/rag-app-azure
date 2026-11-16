import { screen, waitFor } from "@testing-library/react";
import { HttpResponse, http } from "msw";
import { describe, expect, test } from "vitest";

import { adminProject, page, userSummary, uuid } from "../../test/adminData";
import { findToast, renderAdmin } from "../../test/adminRender";
import { apiUrl, problemResponse } from "../../test/handlers";
import { server } from "../../test/server";
import { NewProjectScreen } from "./NewProjectScreen";

const ROUTES = [
  { path: "/admin/projects", element: <p>projects list</p> },
  { path: "/admin/projects/new", element: <NewProjectScreen /> },
  { path: "/admin/projects/:projectId", element: <p>project page</p> },
];

function api(create: (body: Record<string, unknown>) => Response) {
  const bodies: Record<string, unknown>[] = [];
  server.use(
    http.get(apiUrl("/v1/admin/users"), () =>
      HttpResponse.json(page([userSummary(1), userSummary(2), userSummary(3)])),
    ),
    http.post(apiUrl("/v1/admin/projects"), async ({ request }) => {
      const body = (await request.json()) as Record<string, unknown>;
      bodies.push(body);
      return create(body);
    }),
  );
  return bodies;
}

async function choose(user: ReturnType<typeof renderAdmin>["user"], name: string) {
  const input = screen.getByRole("combobox", { name: "Members" });
  await user.click(input);
  await user.type(input, "pe");
  await user.click(await screen.findByRole("option", { name: new RegExp(name) }));
}

describe("New project", () => {
  test("Create project sends the form and the chosen people, opens the project and toasts", async () => {
    const bodies = api(() =>
      HttpResponse.json(adminProject({ projectId: uuid(9), name: "Field notes" }), { status: 201 }),
    );
    const { user, router } = renderAdmin(ROUTES, "/admin/projects/new");
    expect(screen.getByRole("heading", { level: 1, name: "New project" })).toBeInTheDocument();
    expect(document.title).toBe("New project · DocChat");
    await choose(user, "Person 2");
    await choose(user, "Person 1");
    await user.type(screen.getByRole("textbox", { name: /^Name/ }), "Field notes");
    await user.type(screen.getByRole("textbox", { name: "Description" }), "Notes");
    await user.click(screen.getByRole("button", { name: "Add example question" }));
    await user.type(screen.getByRole("textbox", { name: "Example question 1" }), "What changed?");
    await user.click(screen.getByRole("button", { name: "Add example question" }));
    await user.type(screen.getByRole("textbox", { name: "Instructions for answers" }), "Be brief.");
    await user.click(screen.getByRole("switch", { name: "Add new users automatically" }));
    await user.click(screen.getByRole("button", { name: "Create project" }));
    await waitFor(() => {
      expect(router.state.location.pathname).toBe(`/admin/projects/${uuid(9)}`);
    });
    expect(bodies[0]).toEqual({
      name: "Field notes",
      description: "Notes",
      instructions: "Be brief.",
      exampleQuestions: ["What changed?"],
      isDefault: true,
      initialMemberIds: [uuid(102), uuid(101)],
    });
    expect(await findToast("Project created.")).toBeInTheDocument();
  });

  test("a missing name is caught before sending, with focus on the field", async () => {
    const bodies = api(() => HttpResponse.json(adminProject(), { status: 201 }));
    const { user } = renderAdmin(ROUTES, "/admin/projects/new");
    await user.click(screen.getByRole("button", { name: "Create project" }));
    expect(await screen.findByText("Enter a name.")).toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: /^Name/ })).toHaveFocus();
    expect(bodies).toHaveLength(0);
  });

  test("T-400 a 422 at body.initialMemberIds.1 names the second person and keeps every value", async () => {
    let call = 0;
    const bodies = api(() => {
      call += 1;
      return call === 1
        ? problemResponse(422, "validation_failed", {
            title: "Invalid input",
            errors: [{ path: "body.initialMemberIds.1", reason: "not_found" }],
          })
        : HttpResponse.json(adminProject({ projectId: uuid(9) }), { status: 201 });
    });
    const { user, router } = renderAdmin(ROUTES, "/admin/projects/new");
    await choose(user, "Person 1");
    await choose(user, "Person 3");
    await user.type(screen.getByRole("textbox", { name: /^Name/ }), "Field notes");
    await user.type(screen.getByRole("textbox", { name: "Description" }), "Kept");
    await user.click(screen.getByRole("button", { name: "Create project" }));
    expect(
      await screen.findByText(
        "Person 3 doesn't have a DocChat account, so they can't be added. Remove them, then create the project.",
      ),
    ).toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: /^Name/ })).toHaveValue("Field notes");
    expect(screen.getByRole("textbox", { name: "Description" })).toHaveValue("Kept");
    const chosen = screen.getByRole("listbox", { name: "Members" });
    await user.click(screen.getByRole("option", { name: /Person 3/ }));
    expect(chosen).not.toHaveTextContent("Person 3");
    await user.click(screen.getByRole("button", { name: "Create project" }));
    await waitFor(() => {
      expect(router.state.location.pathname).toBe(`/admin/projects/${uuid(9)}`);
    });
    expect(bodies[1]?.["initialMemberIds"]).toEqual([uuid(101)]);
  });

  test("409 project_name_taken shows the problem's detail under Name", async () => {
    api(() =>
      problemResponse(409, "project_name_taken", {
        title: "Name in use",
        detail: "Another project already uses this name.",
      }),
    );
    const { user } = renderAdmin(ROUTES, "/admin/projects/new");
    await user.type(screen.getByRole("textbox", { name: /^Name/ }), "Taken");
    await user.click(screen.getByRole("button", { name: "Create project" }));
    expect(await screen.findByText("Another project already uses this name.")).toBeInTheDocument();
    expect(screen.queryByText("Name in use")).toBeNull();
    expect(screen.getByRole("textbox", { name: /^Name/ })).toHaveAttribute("aria-invalid", "true");
  });

  test("a 422 entry that matches no field and a server failure show the error bar", async () => {
    let call = 0;
    api(() => {
      call += 1;
      return call === 1
        ? problemResponse(422, "validation_failed", {
            title: "Invalid input",
            errors: [{ path: "body.unknown", reason: "unknown_field" }],
          })
        : new HttpResponse(null, { status: 502 });
    });
    const { user } = renderAdmin(ROUTES, "/admin/projects/new");
    await user.type(screen.getByRole("textbox", { name: /^Name/ }), "N");
    await user.click(screen.getByRole("button", { name: "Create project" }));
    expect(await screen.findByText("body.unknown: unknown_field")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Create project" }));
    expect(await screen.findByText("Something went wrong")).toBeInTheDocument();
  });

  test("Cancel returns to Projects", async () => {
    api(() => HttpResponse.json(adminProject(), { status: 201 }));
    const { user } = renderAdmin(ROUTES, "/admin/projects/new");
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    expect(await screen.findByText("projects list")).toBeInTheDocument();
  });
});
