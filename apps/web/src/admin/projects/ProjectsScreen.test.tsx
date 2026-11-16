import { screen, waitFor, within } from "@testing-library/react";
import { HttpResponse, http } from "msw";
import { describe, expect, test } from "vitest";

import { adminProject, page, projectSummary, uuid } from "../../test/adminData";
import { renderAdmin } from "../../test/adminRender";
import { apiUrl, problemResponse } from "../../test/handlers";
import { server } from "../../test/server";
import { ProjectsScreen } from "./ProjectsScreen";

const ROUTES = [
  { path: "/admin/projects", element: <ProjectsScreen /> },
  { path: "/admin/projects/new", element: <p>new project</p> },
];

function listHandler(respond: (url: URL) => Response) {
  const seen: URL[] = [];
  server.use(
    http.get(apiUrl("/v1/admin/projects"), ({ request }) => {
      const url = new URL(request.url);
      seen.push(url);
      return respond(url);
    }),
  );
  return seen;
}

describe("Projects", () => {
  test("lists projects in the API's order with every column, Active by default", async () => {
    const seen = listHandler(() =>
      HttpResponse.json(
        page([
          projectSummary({ name: "Beta", isDefault: true, memberCount: 7 }),
          projectSummary({
            projectId: uuid(2),
            name: "Alpha",
            defaultIndex: null,
            status: "archived",
          }),
        ]),
      ),
    );
    renderAdmin(ROUTES, "/admin/projects");
    expect(await screen.findByRole("heading", { level: 1, name: "Projects" })).toBeInTheDocument();
    const table = await screen.findByRole("table");
    const rows = within(table).getAllByRole("row");
    expect(rows.map((row) => row.textContent)).toEqual([
      "NameDefault indexMembersNew users joinStatus",
      "BetaPolicy library7YesActive",
      "AlphaNone2NoArchived",
    ]);
    expect(within(table).getByRole("link", { name: "Beta" })).toHaveAttribute(
      "href",
      `/admin/projects/${uuid(1)}`,
    );
    expect(document.title).toBe("Projects · DocChat");
    expect(seen[0]?.searchParams.get("status")).toBe("active");
    expect(seen[0]?.searchParams.get("limit")).toBe("50");
    expect(seen[0]?.searchParams.has("q")).toBe(false);
    expect(screen.getByRole("link", { name: "New project" })).toHaveAttribute(
      "href",
      "/admin/projects/new",
    );
  });

  test("the filters are mirrored to the URL and start from the first page", async () => {
    const seen = listHandler((url) => {
      const matches =
        url.searchParams.get("q") === "Fi" && url.searchParams.get("status") === "active";
      return HttpResponse.json(page(matches ? [projectSummary()] : []));
    });
    const { user, router } = renderAdmin(ROUTES, "/admin/projects");
    expect(
      await screen.findByText("No projects yet. Create one to get started."),
    ).toBeInTheDocument();
    await user.type(screen.getByRole("searchbox", { name: "Filter by name" }), "Fi");
    await waitFor(() => {
      expect(router.state.location.search).toBe("?q=Fi");
    });
    expect(await screen.findByRole("link", { name: "Field notes" })).toBeInTheDocument();
    await user.click(screen.getByRole("combobox", { name: "Status" }));
    await user.click(await screen.findByRole("option", { name: "Archived" }));
    await waitFor(() => {
      expect(router.state.location.search).toBe("?q=Fi&status=archived");
    });
    expect(await screen.findByText("No projects match these filters.")).toBeInTheDocument();
    expect(seen.at(-1)?.searchParams.get("status")).toBe("archived");
    expect(seen.at(-1)?.searchParams.get("q")).toBe("Fi");
  });

  test("a status outside its values is dropped with replace navigation", async () => {
    listHandler(() => HttpResponse.json(page([])));
    const { router } = renderAdmin(ROUTES, "/admin/projects?status=deleted&q=x");
    await waitFor(() => {
      expect(router.state.location.search).toBe("?q=x");
    });
    expect(router.state.historyAction).toBe("REPLACE");
  });

  test("Show more appends the next page; a failed page shows its error bar under the table", async () => {
    let calls = 0;
    listHandler((url) => {
      calls += 1;
      if (url.searchParams.get("cursor") === null) {
        return HttpResponse.json(page([projectSummary()], "next1"));
      }
      if (calls === 2) return problemResponse(503, "dependency_unavailable", { title: "Down" });
      return HttpResponse.json(page([projectSummary({ projectId: uuid(3), name: "Third" })]));
    });
    const { user } = renderAdmin(ROUTES, "/admin/projects");
    await user.click(await screen.findByRole("button", { name: "Show more" }));
    expect(await screen.findByText("Down")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Field notes" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByRole("link", { name: "Third" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Show more" })).toBeNull();
  });

  test("a failed load shows projects.loadError with Retry", async () => {
    let fail = true;
    listHandler(() =>
      fail
        ? problemResponse(500, "internal_error", { title: "Unexpected error", detail: "Oops." })
        : HttpResponse.json(page([projectSummary()])),
    );
    const { user } = renderAdmin(ROUTES, "/admin/projects");
    expect(await screen.findByText("Projects didn't load.")).toBeInTheDocument();
    expect(screen.getByText("Unexpected error")).toBeInTheDocument();
    expect(screen.getByText(/Reference ID/)).toBeInTheDocument();
    fail = false;
    await user.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByRole("link", { name: "Field notes" })).toBeInTheDocument();
    expect(adminProject().name).toBe("Field notes");
  });
});
