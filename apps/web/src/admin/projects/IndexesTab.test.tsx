import { screen, waitFor, within } from "@testing-library/react";
import { HttpResponse, http } from "msw";
import { describe, expect, test } from "vitest";

import { adminProject, indexSummary, mapping, page, rowVersion, uuid } from "../../test/adminData";
import { findToast, nth, renderAdmin } from "../../test/adminRender";
import { apiUrl, problemResponse } from "../../test/handlers";
import { server } from "../../test/server";
import { MAPPABLE_STATUSES, mappableIndexes } from "./MapIndexDialog";
import { ProjectScreen } from "./ProjectScreen";

const PROJECT = uuid(1);
const ROUTES = [
  { path: "/admin/projects/:projectId", element: <ProjectScreen /> },
  { path: "/admin/indexes/:indexId", element: <p>index page</p> },
];

interface Write {
  readonly method: string;
  readonly indexId: string;
  readonly ifMatch: string | null;
  readonly body: unknown;
}

function indexesWorld(answer: (write: Write, index: number) => Response | null = () => null) {
  const world = {
    version: 1,
    mappings: [mapping(1, { isDefault: true }), mapping(2, { indexStatus: "rebuilding" })],
    writes: [] as Write[],
    log: [] as string[],
  };
  const record = async (request: Request, indexId: string) => {
    const text = await request.text();
    const write: Write = {
      method: request.method,
      indexId,
      ifMatch: request.headers.get("If-Match"),
      body: text === "" ? null : (JSON.parse(text) as unknown),
    };
    world.writes.push(write);
    return write;
  };
  server.use(
    http.get(apiUrl(`/v1/admin/projects/${PROJECT}`), () => {
      world.log.push("project");
      return HttpResponse.json(adminProject({ rowVersion: rowVersion(world.version) }));
    }),
    http.get(apiUrl(`/v1/admin/projects/${PROJECT}/indexes`), ({ request }) => {
      world.log.push("indexes");
      expect(new URL(request.url).searchParams.get("limit")).toBe("50");
      return HttpResponse.json(page(world.mappings));
    }),
    http.get(apiUrl("/v1/admin/indexes"), () =>
      HttpResponse.json(
        page([
          indexSummary(1),
          indexSummary(3, { displayName: "Ready one" }),
          indexSummary(4, { displayName: "Failed one", status: "failed" }),
          indexSummary(5, { displayName: "Deleting one", status: "deleting" }),
          indexSummary(6, { displayName: "New one", status: "provisioning" }),
        ]),
      ),
    ),
    http.put(
      apiUrl(`/v1/admin/projects/${PROJECT}/indexes/:indexId`),
      async ({ request, params }) => {
        const write = await record(request, String(params["indexId"]));
        const scripted = answer(write, world.writes.length - 1);
        if (scripted !== null) return scripted;
        world.version += 1;
        const isDefault = (write.body as { isDefault: boolean }).isDefault;
        const existing = world.mappings.find((each) => each.indexId === write.indexId);
        const changed = mapping(Number(write.indexId.slice(-3)) - 500, {
          ...existing,
          isDefault,
        });
        world.mappings = [
          ...world.mappings
            .filter((each) => each.indexId !== write.indexId)
            .map((each) => (isDefault ? { ...each, isDefault: false } : each)),
          changed,
        ];
        return HttpResponse.json(
          { mapping: changed, projectRowVersion: rowVersion(world.version) },
          { status: existing === undefined ? 201 : 200 },
        );
      },
    ),
    http.delete(
      apiUrl(`/v1/admin/projects/${PROJECT}/indexes/:indexId`),
      async ({ request, params }) => {
        const write = await record(request, String(params["indexId"]));
        const scripted = answer(write, world.writes.length - 1);
        if (scripted !== null) return scripted;
        world.version += 1;
        world.mappings = world.mappings.filter((each) => each.indexId !== write.indexId);
        return HttpResponse.json({ removed: true, projectRowVersion: rowVersion(world.version) });
      },
    ),
  );
  return world;
}

function open() {
  return renderAdmin(ROUTES, `/admin/projects/${PROJECT}?tab=indexes`);
}

/** Map an index, once the tab's rows have loaded and the button is enabled. */
async function mapButton(): Promise<HTMLElement> {
  const button = await screen.findByRole("button", { name: "Map an index" });
  await waitFor(() => {
    expect(button).toBeEnabled();
  });
  return button;
}

describe("the project's Indexes tab", () => {
  test("T-399 reads the project first, then fills the table with link, default badge and status", async () => {
    const world = indexesWorld();
    open();
    const table = await screen.findByRole("table", { name: "Indexes" });
    await within(table).findByRole("link", { name: "Index 1" });
    expect(world.log.slice(-2)).toEqual(["project", "indexes"]);
    const rows = within(table).getAllByRole("row").slice(1);
    expect(rows.map((row) => row.textContent)).toEqual([
      "Index 1DefaultReadyUnmap",
      "Index 2RebuildingMake defaultUnmap",
    ]);
    expect(within(table).getByRole("link", { name: "Index 1" })).toHaveAttribute(
      "href",
      `/admin/indexes/${uuid(501)}`,
    );
  });

  test("T-399 Map an index lists only unmapped indexes with a mappable status, and maps one", async () => {
    const world = indexesWorld();
    const { user } = open();
    await user.click(await mapButton());
    const dialog = await screen.findByRole("dialog", { name: "Map an index" });
    expect(within(dialog).getByRole("switch", { name: "Make this the default" })).not.toBeChecked();
    await user.click(within(dialog).getByRole("combobox", { name: "Index" }));
    const options = await screen.findAllByRole("option");
    expect(options.map((option) => option.textContent)).toEqual([
      "Ready oneReady",
      "New oneSetting up",
    ]);
    await user.click(screen.getByRole("option", { name: /Ready one/ }));
    await user.click(within(dialog).getByRole("button", { name: "Map" }));
    expect(await findToast("Index mapped.")).toBeInTheDocument();
    expect(world.writes).toEqual([
      {
        method: "PUT",
        indexId: uuid(503),
        ifMatch: `"${rowVersion(1)}"`,
        body: { isDefault: false },
      },
    ]);
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).toBeNull();
    });
    expect(await screen.findByRole("link", { name: "Index 3" })).toBeInTheDocument();
  });

  test("T-399 with nothing to offer, the dialog shows noneAvailable and Map is disabled; a first mapping is the default", async () => {
    const world = indexesWorld();
    world.mappings = [];
    server.use(
      http.get(apiUrl("/v1/admin/indexes"), () =>
        HttpResponse.json(page([indexSummary(4, { status: "failed" })])),
      ),
    );
    const { user } = open();
    expect(
      await screen.findByText(
        "No indexes mapped. Map an index so people can chat with its documents.",
      ),
    ).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Map an index" }));
    const dialog = await screen.findByRole("dialog");
    expect(await within(dialog).findByText(/No index can be mapped right now/)).toBeInTheDocument();
    expect(within(dialog).getByRole("switch", { name: "Make this the default" })).toBeChecked();
    expect(within(dialog).getByRole("button", { name: "Map" })).toBeDisabled();
    await user.click(within(dialog).getByRole("button", { name: "Cancel" }));
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).toBeNull();
    });
  });

  test("a 412 on Map keeps the dialog open with its choices, reloads the tab and shows conflict.action", async () => {
    const world = indexesWorld((write) =>
      write.ifMatch === `"${rowVersion(1)}"`
        ? problemResponse(412, "precondition_failed", { title: "Changed by someone else" })
        : null,
    );
    const { user } = open();
    await user.click(await mapButton());
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("combobox", { name: "Index" }));
    await user.click(await screen.findByRole("option", { name: /Ready one/ }));
    await user.click(within(dialog).getByRole("switch", { name: "Make this the default" }));
    world.version = 7;
    world.log = [];
    await user.click(within(dialog).getByRole("button", { name: "Map" }));
    expect(
      await within(dialog).findByText(
        "The latest version is shown now. Check it, then repeat the action if you still need it.",
      ),
    ).toBeInTheDocument();
    await waitFor(() => {
      expect(world.log).toEqual(["project", "indexes"]);
    });
    expect(within(dialog).getByRole("combobox", { name: "Index" })).toHaveTextContent("Ready one");
    expect(within(dialog).getByRole("switch", { name: "Make this the default" })).toBeChecked();
    await user.click(within(dialog).getByRole("button", { name: "Map" }));
    expect(await findToast("Index mapped.")).toBeInTheDocument();
    expect(world.writes[1]).toMatchObject({
      ifMatch: `"${rowVersion(7)}"`,
      body: { isDefault: true },
    });
  });

  test("Make default sends isDefault true; a 409 shows the problem's text above the table", async () => {
    const world = indexesWorld((write, index) =>
      index === 1
        ? problemResponse(409, "index_not_mappable", { title: "Index unavailable" })
        : null,
    );
    const { user } = open();
    await user.click(await screen.findByRole("button", { name: "Make default" }));
    expect(await findToast("Default index changed.")).toBeInTheDocument();
    expect(world.writes[0]).toMatchObject({ indexId: uuid(502), body: { isDefault: true } });
    await waitFor(() => {
      expect(screen.getAllByRole("button", { name: "Make default" })).toHaveLength(1);
    });
    await user.click(screen.getByRole("button", { name: "Make default" }));
    expect(await screen.findByText("Index unavailable")).toBeInTheDocument();
  });

  test("Unmap confirms, sends the project's version and toasts whatever removed says", async () => {
    const world = indexesWorld();
    const { user } = open();
    const table = await screen.findByRole("table", { name: "Indexes" });
    const unmaps = await within(table).findAllByRole("button", { name: "Unmap" });
    await user.click(nth(unmaps, 1));
    const dialog = await screen.findByRole("dialog", { name: "Unmap Index 2 from Field notes?" });
    await user.click(within(dialog).getByRole("button", { name: "Unmap" }));
    expect(await findToast("Index unmapped.")).toBeInTheDocument();
    expect(world.writes[0]).toMatchObject({
      method: "DELETE",
      indexId: uuid(502),
      ifMatch: `"${rowVersion(1)}"`,
    });
    await waitFor(() => {
      expect(screen.queryByRole("link", { name: "Index 2" })).toBeNull();
    });
  });

  test("the mappable statuses of section 4.8", () => {
    expect([...MAPPABLE_STATUSES]).toEqual([
      "provisioning",
      "ready",
      "reingest_required",
      "reingesting",
      "rebuilding",
    ]);
    expect(
      mappableIndexes([indexSummary(1), indexSummary(2, { status: "deleting" })], new Set()),
    ).toHaveLength(1);
  });
});
