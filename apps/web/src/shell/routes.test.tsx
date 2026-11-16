import { FluentProvider, webLightTheme } from "@fluentui/react-components";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { HttpResponse, http } from "msw";
import { RouterProvider, createMemoryRouter } from "react-router";
import { describe, expect, test, vi } from "vitest";

import { initTokens } from "../auth/tokens";
import {
  adminProject,
  indexDetail,
  indexOptions,
  memoryView,
  page,
  runItem,
  runSummary,
} from "../test/adminData";
import { sessionDetail } from "../test/chat";
import { TEST_ACCOUNT, fakeMsal } from "../test/fakeMsal";
import { ADMIN_USER, IDS, TEST_CONFIG, apiUrl } from "../test/handlers";
import { configureTestHttp, renderApp } from "../test/render";
import { server } from "../test/server";
import { ModuleLoadError, RouteError, isCanonicalUuid, lazyScreen } from "./routes";

/** The first calls of the memory, project, index, document and run screens. */
function memoryAndAdminHandlers() {
  return [
    http.get(apiUrl("/v1/memory"), () => HttpResponse.json(memoryView())),
    http.get(apiUrl("/v1/admin/projects"), () => HttpResponse.json(page([]))),
    http.get(apiUrl("/v1/admin/projects/:projectId"), () => HttpResponse.json(adminProject())),
    http.get(apiUrl("/v1/admin/indexes"), () => HttpResponse.json(page([]))),
    http.get(apiUrl("/v1/admin/indexes/:indexId"), () => HttpResponse.json(indexDetail())),
    http.get(apiUrl("/v1/admin/index-options"), () => HttpResponse.json(indexOptions())),
    http.get(apiUrl("/v1/admin/indexes/:indexId/documents"), () => HttpResponse.json(page([]))),
    http.get(apiUrl("/v1/admin/ingestion-runs"), () => HttpResponse.json(page([]))),
    http.get(apiUrl("/v1/admin/ingestion-runs/:runId"), () =>
      HttpResponse.json({ run: runSummary(1), items: [runItem(1)], nextItemsCursor: null }),
    ),
  ];
}

function signedIn(admin = false) {
  configureTestHttp();
  initTokens({ msal: fakeMsal().msal, account: TEST_ACCOUNT, scope: TEST_CONFIG.apiScope });
  if (admin) server.use(http.get(apiUrl("/v1/me"), () => HttpResponse.json(ADMIN_USER)));
}

describe("the routes", () => {
  test.each([
    ["/", "/chat"],
    ["/no/such/path", "/chat"],
    ["/chat/not-a-uuid", "/chat"],
    [`/chat/${IDS.session.toUpperCase()}`, "/chat"],
    ["/admin", "/chat"],
  ])("%s is replaced by %s", async (route, expected) => {
    signedIn();
    const { router } = renderApp({ route });
    await screen.findByRole("heading", { level: 1, name: "Chat" });
    expect(router.state.location.pathname).toBe(expected);
  });

  test("/chat and /chat/<uuid> show the chat screen with its title", async () => {
    signedIn();
    server.use(
      http.get(apiUrl(`/v1/sessions/${IDS.session}`), () => HttpResponse.json(sessionDetail())),
    );
    const { router } = renderApp({ route: `/chat/${IDS.session}` });
    expect(
      await screen.findByRole("heading", { level: 1, name: "Chat: Expense claims" }),
    ).toBeInTheDocument();
    expect(router.state.location.pathname).toBe(`/chat/${IDS.session}`);
    expect(document.title).toBe("Expense claims · DocChat");
  });

  test("/memory loads its lazy screen", async () => {
    signedIn();
    server.use(...memoryAndAdminHandlers());
    renderApp({ route: "/memory" });
    expect(await screen.findByRole("heading", { level: 1, name: "Memory" })).toBeInTheDocument();
    expect(document.title).toBe("Memory · DocChat");
  });

  test.each([
    ["/admin/projects", "Projects", "Projects · DocChat"],
    ["/admin/projects/new", "New project", "New project · DocChat"],
    [`/admin/projects/${IDS.projectA}`, /^Field notes/, "Field notes · Projects · DocChat"],
    ["/admin/indexes", "Indexes", "Indexes · DocChat"],
    ["/admin/indexes/new", "New index", "New index · DocChat"],
    [`/admin/indexes/${IDS.projectA}`, /^Policy library/, "Policy library · Indexes · DocChat"],
    [
      `/admin/indexes/${IDS.projectA}/documents`,
      "Documents in Policy library",
      "Documents · Policy library · DocChat",
    ],
    ["/admin/runs", "Ingestion runs", "Ingestion runs · DocChat"],
    [`/admin/runs/${IDS.turn}`, "Ingestion run", "Ingestion run · DocChat"],
  ])("T-352 an admin opening %s gets its screen", async (route, heading, title) => {
    signedIn(true);
    server.use(...memoryAndAdminHandlers());
    renderApp({ route });
    expect(await screen.findByRole("heading", { level: 1, name: heading })).toBeInTheDocument();
    expect(document.title).toBe(title);
  });

  test("T-352 a non-admin opening /admin/projects sees Admin only and no admin screen", async () => {
    signedIn();
    renderApp({ route: "/admin/projects" });
    expect(
      await screen.findByRole("heading", { level: 1, name: "This page is for administrators" }),
    ).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Projects" })).toBeNull();
  });

  test("canonical UUIDs are lower case", () => {
    expect(isCanonicalUuid(IDS.session)).toBe(true);
    expect(isCanonicalUuid(IDS.session.toUpperCase())).toBe(false);
    expect(isCanonicalUuid(undefined)).toBe(false);
  });
});

describe("the error element", () => {
  function renderFailing(element: React.ReactElement) {
    const router = createMemoryRouter([{ path: "/", element, errorElement: <RouteError /> }], {
      initialEntries: ["/"],
    });
    render(
      <FluentProvider theme={webLightTheme}>
        <RouterProvider router={router} />
      </FluentProvider>,
    );
  }

  test("a lazy module that fails to load shows DocChat has been updated, with Reload", async () => {
    vi.spyOn(console, "error").mockImplementation(() => undefined);
    vi.spyOn(console, "info").mockImplementation(() => undefined);
    const reload = vi.fn();
    vi.stubGlobal("location", { reload, href: "http://localhost:3000/" });
    const Missing = lazyScreen(
      () => Promise.reject(new TypeError("Failed to fetch dynamically imported module")),
      () => () => null,
    );
    renderFailing(<Missing />);
    expect(
      await screen.findByRole("heading", { name: "DocChat has been updated" }),
    ).toBeInTheDocument();
    expect(screen.getByText("Reload the page to get the latest version.")).toBeInTheDocument();
    expect(screen.queryByText(/Reference ID/)).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "Reload" }));
    expect(reload).toHaveBeenCalled();
    expect(new ModuleLoadError("x").message).toBe("app_updated");
  });

  test("any other rendering error shows Something went wrong on this page", async () => {
    vi.spyOn(console, "error").mockImplementation(() => undefined);
    vi.spyOn(console, "info").mockImplementation(() => undefined);
    function Broken(): never {
      throw new Error("render bug");
    }
    renderFailing(<Broken />);
    expect(
      await screen.findByRole("heading", { name: "Something went wrong on this page" }),
    ).toBeInTheDocument();
    expect(document.title).toBe("Something went wrong on this page · DocChat");
  });
});
