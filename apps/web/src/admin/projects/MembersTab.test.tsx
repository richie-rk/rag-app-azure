import { act, screen, waitFor, within } from "@testing-library/react";
import { HttpResponse, http } from "msw";
import { describe, expect, test, vi } from "vitest";

import {
  ADMIN_REF,
  adminProject,
  member,
  page,
  rowVersion,
  userSummary,
  uuid,
} from "../../test/adminData";
import { findToast, renderAdmin } from "../../test/adminRender";
import { apiUrl, problemResponse } from "../../test/handlers";
import { server } from "../../test/server";
import { formatDate } from "../../ui/format";
import { addedByText, rowAfterRemoval } from "./MembersTab";
import { ProjectScreen } from "./ProjectScreen";

const PROJECT = uuid(1);
const ROUTES = [{ path: "/admin/projects/:projectId", element: <ProjectScreen /> }];

interface World {
  version: number;
  members: ReturnType<typeof member>[];
  log: string[];
  adds: { userId: string; ifMatch: string | null }[];
}

/** The project's API: reads log their order; adds answer from `add` (default: added). */
function membersWorld(
  add: (world: World, userId: string, index: number) => Response | null = () => null,
  remove: (world: World, userId: string) => Response | null = () => null,
) {
  const world: World = { version: 1, members: [member(1), member(2)], log: [], adds: [] };
  server.use(
    http.get(apiUrl(`/v1/admin/projects/${PROJECT}`), () => {
      world.log.push("project");
      return HttpResponse.json(adminProject({ rowVersion: rowVersion(world.version) }));
    }),
    http.get(apiUrl(`/v1/admin/projects/${PROJECT}/members`), () => {
      world.log.push("members");
      return HttpResponse.json(page(world.members));
    }),
    http.get(apiUrl("/v1/admin/users"), () =>
      HttpResponse.json(
        page([
          userSummary(1, { isMember: true }),
          userSummary(3, { isMember: false }),
          userSummary(4, { isMember: false }),
          userSummary(5, { isMember: false }),
        ]),
      ),
    ),
    http.put(apiUrl(`/v1/admin/projects/${PROJECT}/members/:userId`), ({ params, request }) => {
      const userId = String(params["userId"]);
      world.adds.push({ userId, ifMatch: request.headers.get("If-Match") });
      const scripted = add(world, userId, world.adds.length - 1);
      if (scripted !== null) return scripted;
      world.version += 1;
      const added = member(Number(userId.slice(-3)) - 100, { userId });
      world.members = [...world.members, added];
      return HttpResponse.json(
        { member: added, projectRowVersion: rowVersion(world.version) },
        { status: 201 },
      );
    }),
    http.delete(apiUrl(`/v1/admin/projects/${PROJECT}/members/:userId`), ({ params }) => {
      const userId = String(params["userId"]);
      const scripted = remove(world, userId);
      if (scripted !== null) return scripted;
      world.version += 1;
      world.members = world.members.filter((each) => each.userId !== userId);
      return HttpResponse.json({ removed: true, projectRowVersion: rowVersion(world.version) });
    }),
  );
  return world;
}

async function pick(user: ReturnType<typeof renderAdmin>["user"], ...names: string[]) {
  const input = screen.getByRole("combobox", { name: "People to add" });
  for (const name of names) {
    await user.click(input);
    await user.type(input, "pe");
    await user.click(await screen.findByRole("option", { name: new RegExp(name) }));
  }
}

describe("the Members tab", () => {
  test("T-399 reads the project, then the members, and fills the table", async () => {
    const world = membersWorld();
    world.members = [
      member(1),
      member(2, { grantSource: "default", grantedBy: null }),
      member(3, { grantedBy: { userId: uuid(901), displayName: "" } }),
    ];
    const { user } = renderAdmin(ROUTES, `/admin/projects/${PROJECT}?tab=members`);
    const table = await screen.findByRole("table", { name: "Members" });
    await within(table).findByText("Person 1");
    // The header's own read, then the tab's: the project, then its members.
    expect(world.log).toEqual(["project", "project", "members"]);
    const rows = within(table).getAllByRole("row").slice(1);
    expect(rows.map((row) => row.textContent)).toEqual([
      `Person 1person1@example.testGrace Hopper${formatDate(member(1).grantedAt)}Remove`,
      `Person 2person2@example.testJoined automatically at first sign-in${formatDate(member(1).grantedAt)}Remove`,
      `Person 3person3@example.testAn administrator${formatDate(member(1).grantedAt)}Remove`,
    ]);
    expect(within(table).getByRole("button", { name: "Remove Person 2" })).toBeInTheDocument();
    // Every opening of the tab reads the project first again, whatever the cache holds.
    await user.click(screen.getByRole("tab", { name: "Details" }));
    world.log = [];
    await user.click(screen.getByRole("tab", { name: "Members" }));
    await waitFor(() => {
      expect(world.log).toEqual(["project", "members"]);
    });
  });

  test("Empty: members.empty", async () => {
    const world = membersWorld();
    world.members = [];
    renderAdmin(ROUTES, `/admin/projects/${PROJECT}?tab=members`);
    expect(
      await screen.findByText("No members yet. Add people so they can chat in this project."),
    ).toBeInTheDocument();
  });

  test("T-398 Add people sends one add per person in order, each with the previous projectRowVersion", async () => {
    const world = membersWorld();
    const { user } = renderAdmin(ROUTES, `/admin/projects/${PROJECT}?tab=members`);
    await screen.findByRole("table", { name: "Members" });
    await user.click(screen.getByRole("button", { name: "Add people" }));
    await user.click(screen.getByRole("combobox", { name: "People to add" }));
    await user.type(screen.getByRole("combobox", { name: "People to add" }), "pe");
    expect(await screen.findByRole("option", { name: /Person 1/ })).toHaveAttribute(
      "aria-disabled",
      "true",
    );
    await user.keyboard("{Escape}");
    await pick(user, "Person 3", "Person 4", "Person 5");
    await user.click(screen.getByRole("button", { name: "Add" }));
    expect(await findToast("3 people added.")).toBeInTheDocument();
    expect(world.adds.map((each) => each.ifMatch)).toEqual([
      `"${rowVersion(1)}"`,
      `"${rowVersion(2)}"`,
      `"${rowVersion(3)}"`,
    ]);
    expect(world.adds.map((each) => each.userId)).toEqual([uuid(103), uuid(104), uuid(105)]);
    await waitFor(() => {
      expect(
        within(screen.getByRole("table", { name: "Members" })).getByText("Person 5"),
      ).toBeInTheDocument();
    });
  });

  test("T-398 a 412 on the second add sends no third, reloads project then members, and keeps the rest in the picker", async () => {
    const world = membersWorld((_, __, index) =>
      index === 1
        ? problemResponse(412, "precondition_failed", { title: "Changed by someone else" })
        : null,
    );
    const { user } = renderAdmin(ROUTES, `/admin/projects/${PROJECT}?tab=members`);
    await screen.findByRole("table", { name: "Members" });
    await user.click(screen.getByRole("button", { name: "Add people" }));
    await pick(user, "Person 3", "Person 4", "Person 5");
    world.log = [];
    await user.click(screen.getByRole("button", { name: "Add" }));
    expect(
      await screen.findByText(
        "The latest version is shown now. Check it, then repeat the action if you still need it.",
      ),
    ).toBeInTheDocument();
    expect(await findToast("1 person added.")).toBeInTheDocument();
    expect(world.adds).toHaveLength(2);
    await waitFor(() => {
      expect(world.log).toEqual(["project", "members"]);
    });
    const chosen = screen.getByRole("listbox", { name: "People to add" });
    expect(chosen).not.toHaveTextContent("Person 3");
    expect(chosen).toHaveTextContent("Person 4");
    expect(chosen).toHaveTextContent("Person 5");
  });

  test("T-398 a 429 waits, shows members.waiting, sends the same person; a fourth 429 fails that person and the next is sent", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      const world = membersWorld((_, userId) =>
        userId === uuid(103)
          ? problemResponse(429, "write_rate_limited", { retryAfterSeconds: 2, title: "Too many" })
          : null,
      );
      const { user } = renderAdmin(ROUTES, `/admin/projects/${PROJECT}?tab=members`);
      await screen.findByRole("table", { name: "Members" });
      await user.click(screen.getByRole("button", { name: "Add people" }));
      await pick(user, "Person 3", "Person 4");
      await user.click(screen.getByRole("button", { name: "Add" }));
      expect(
        await screen.findByText(/Many changes in a short time\. Adding the rest at/),
      ).toBeInTheDocument();
      expect(screen.getByRole("combobox", { name: "People to add" })).toBeDisabled();
      for (let wait = 0; wait < 3; wait += 1) {
        await act(() => vi.advanceTimersByTimeAsync(2_100));
      }
      expect(
        await screen.findByText(
          "These people weren't added: Person 3. They stay in the picker, so you can select Add to retry.",
        ),
      ).toBeInTheDocument();
      expect(world.adds.map((each) => each.userId)).toEqual([
        uuid(103),
        uuid(103),
        uuid(103),
        uuid(103),
        uuid(104),
      ]);
      expect(world.adds.slice(0, 4).every((each) => each.ifMatch === `"${rowVersion(1)}"`)).toBe(
        true,
      );
      expect(await findToast("1 person added.")).toBeInTheDocument();
      expect(screen.getByRole("listbox", { name: "People to add" })).toHaveTextContent("Person 3");
    } finally {
      vi.useRealTimers();
    }
  });

  test("Remove confirms, sends the project's version, toasts and moves focus to the next row", async () => {
    const world = membersWorld();
    world.members = [member(1), member(2), member(3)];
    const { user } = renderAdmin(ROUTES, `/admin/projects/${PROJECT}?tab=members`);
    const table = await screen.findByRole("table", { name: "Members" });
    await user.click(await within(table).findByRole("button", { name: "Remove Person 2" }));
    const dialog = await screen.findByRole("dialog", { name: "Remove Person 2 from Field notes?" });
    await user.click(within(dialog).getByRole("button", { name: "Remove" }));
    expect(await findToast("Person 2 removed.")).toBeInTheDocument();
    await waitFor(() => {
      expect(within(table).queryByText("Person 2")).toBeNull();
    });
    await waitFor(() => {
      expect(within(table).getByText("Person 3")).toHaveFocus();
    });
  });

  test("removing the last row moves focus to the caption", async () => {
    const world = membersWorld();
    world.members = [member(1)];
    const { user } = renderAdmin(ROUTES, `/admin/projects/${PROJECT}?tab=members`);
    const table = await screen.findByRole("table", { name: "Members" });
    await user.click(await within(table).findByRole("button", { name: "Remove Person 1" }));
    await user.click(
      within(await screen.findByRole("dialog")).getByRole("button", { name: "Remove" }),
    );
    await waitFor(() => {
      expect(table.querySelector("caption")).toHaveFocus();
    });
    expect(
      screen.getByText("No members yet. Add people so they can chat in this project."),
    ).toBeInTheDocument();
  });

  test("a refused remove shows the problem's text above the table and keeps the row", async () => {
    membersWorld(undefined, () =>
      problemResponse(409, "project_archived", { title: "Project archived" }),
    );
    const { user } = renderAdmin(ROUTES, `/admin/projects/${PROJECT}?tab=members`);
    await user.click(await screen.findByRole("button", { name: "Remove Person 2" }));
    await user.click(
      within(await screen.findByRole("dialog")).getByRole("button", { name: "Remove" }),
    );
    expect(await screen.findByText("Project archived")).toBeInTheDocument();
    expect(await screen.findByRole("button", { name: "Remove Person 2" })).toBeInTheDocument();
  });

  test("helpers: who added a member, and the row after a removal", () => {
    expect(addedByText(member(1))).toBe(ADMIN_REF.displayName);
    expect(rowAfterRemoval(["a", "b", "c"], "b")).toBe("c");
    expect(rowAfterRemoval(["a", "b", "c"], "c")).toBe("b");
    expect(rowAfterRemoval(["a"], "a")).toBeNull();
    expect(rowAfterRemoval(["a"], "z")).toBeNull();
  });
});
