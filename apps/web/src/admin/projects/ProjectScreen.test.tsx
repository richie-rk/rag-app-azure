import { act, screen, waitFor, within } from "@testing-library/react";
import { HttpResponse, http } from "msw";
import { describe, expect, test, vi } from "vitest";

import type { AdminProject } from "../shared/queries";
import { adminProject, member, page, rowVersion, userSummary, uuid } from "../../test/adminData";
import { findToast, nth, renderAdmin } from "../../test/adminRender";
import { apiUrl, problemResponse } from "../../test/handlers";
import { server } from "../../test/server";
import { formatRetryTime } from "../../ui/format";
import { ProjectScreen } from "./ProjectScreen";

const PROJECT = uuid(1);
const ROUTES = [
  { path: "/admin/projects", element: <p>projects list</p> },
  { path: "/admin/projects/:projectId", element: <ProjectScreen /> },
];

interface Patch {
  readonly body: Record<string, unknown>;
  readonly ifMatch: string | null;
}

function projectApi(
  read: () => AdminProject | Response,
  patch: (sent: Patch) => AdminProject | Response = () => adminProject(),
) {
  const patches: Patch[] = [];
  const answer = (value: AdminProject | Response) =>
    value instanceof Response ? value : HttpResponse.json(value);
  server.use(
    http.get(apiUrl(`/v1/admin/projects/${PROJECT}`), () => answer(read())),
    http.patch(apiUrl(`/v1/admin/projects/${PROJECT}`), async ({ request }) => {
      const sent = {
        body: (await request.json()) as Record<string, unknown>,
        ifMatch: request.headers.get("If-Match"),
      };
      patches.push(sent);
      return answer(patch(sent));
    }),
    http.get(apiUrl(`/v1/admin/projects/${PROJECT}/members`), () =>
      HttpResponse.json(page([member(1)])),
    ),
  );
  return patches;
}

describe("Project", () => {
  test("shows the breadcrumb, the name with its badge, the tabs and the prefilled Details form", async () => {
    projectApi(() => adminProject());
    renderAdmin(ROUTES, `/admin/projects/${PROJECT}`);
    expect(
      await screen.findByRole("heading", { level: 1, name: /Field notes/ }),
    ).toBeInTheDocument();
    expect(screen.getByText("Active")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Projects" })).toHaveAttribute(
      "href",
      "/admin/projects",
    );
    expect(document.title).toBe("Field notes · Projects · DocChat");
    expect(screen.getByRole("tab", { name: "Details", selected: true })).toBeInTheDocument();
    expect(await screen.findByRole("textbox", { name: /^Name/ })).toHaveValue("Field notes");
    expect(screen.getByRole("textbox", { name: "Example question 1" })).toHaveValue(
      "What changed in the policy?",
    );
    expect(screen.getByRole("button", { name: "Save changes" })).toBeDisabled();
  });

  test("the tab is mirrored to ?tab=, and an unknown tab is dropped", async () => {
    projectApi(() => adminProject());
    const { user, router } = renderAdmin(ROUTES, `/admin/projects/${PROJECT}?tab=nope`);
    await waitFor(() => {
      expect(router.state.location.search).toBe("");
    });
    await user.click(await screen.findByRole("tab", { name: "Members" }));
    expect(router.state.location.search).toBe("?tab=members");
    expect(await screen.findByRole("table", { name: "Members" })).toBeInTheDocument();
  });

  test("a 404 shows the problem's text with a link back to Projects", async () => {
    projectApi(() =>
      problemResponse(404, "project_not_found", {
        title: "Project not found",
        detail: "This project doesn't exist, or you don't have access to it.",
      }),
    );
    renderAdmin(ROUTES, `/admin/projects/${PROJECT}`);
    expect(await screen.findByText("Project not found")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Back to Projects" })).toBeInTheDocument();
  });

  test("any other load failure shows the error bar with Retry", async () => {
    let fail = true;
    projectApi(() => (fail ? new HttpResponse(null, { status: 503 }) : adminProject()));
    const { user } = renderAdmin(ROUTES, `/admin/projects/${PROJECT}`);
    expect(await screen.findByText("Something went wrong")).toBeInTheDocument();
    fail = false;
    await user.click(screen.getByRole("button", { name: "Retry" }));
    expect(
      await screen.findByRole("heading", { level: 1, name: /Field notes/ }),
    ).toBeInTheDocument();
  });

  test("Save changes sends only the changed fields with If-Match, then toasts", async () => {
    const patches = projectApi(
      () => adminProject(),
      ({ body }) => adminProject({ ...body, rowVersion: rowVersion(2) }),
    );
    const { user } = renderAdmin(ROUTES, `/admin/projects/${PROJECT}`);
    const description = await screen.findByRole("textbox", { name: "Description" });
    await user.clear(description);
    await user.type(description, "Updated");
    await user.click(screen.getByRole("button", { name: "Save changes" }));
    expect(await findToast("Changes saved.")).toBeInTheDocument();
    expect(patches).toEqual([{ body: { description: "Updated" }, ifMatch: `"${rowVersion(1)}"` }]);
    expect(screen.getByRole("button", { name: "Save changes" })).toBeDisabled();
  });

  test("removing an example question sends the shorter list", async () => {
    const patches = projectApi(
      () => adminProject({ exampleQuestions: ["One", "Two"] }),
      ({ body }) => adminProject({ ...body, rowVersion: rowVersion(2) }),
    );
    const { user } = renderAdmin(ROUTES, `/admin/projects/${PROJECT}`);
    await user.click(await screen.findByRole("button", { name: "Remove example question 1" }));
    expect(screen.getByRole("textbox", { name: "Example question 1" })).toHaveValue("Two");
    await user.click(screen.getByRole("button", { name: "Save changes" }));
    expect(await findToast("Changes saved.")).toBeInTheDocument();
    expect(patches[0]?.body).toEqual({ exampleQuestions: ["Two"] });
  });

  test("section 5.6 step 4: a member added on another tab moves the draft's version, so Save meets no 412", async () => {
    const patches = projectApi(
      () => adminProject(),
      ({ ifMatch, body }) =>
        ifMatch === `"${rowVersion(2)}"`
          ? adminProject({ ...body, rowVersion: rowVersion(3) })
          : problemResponse(412, "precondition_failed"),
    );
    server.use(
      http.get(apiUrl("/v1/admin/users"), () => HttpResponse.json(page([userSummary(7)]))),
      http.put(apiUrl(`/v1/admin/projects/${PROJECT}/members/:userId`), () =>
        HttpResponse.json({ member: member(7), projectRowVersion: rowVersion(2) }, { status: 201 }),
      ),
    );
    const { user } = renderAdmin(ROUTES, `/admin/projects/${PROJECT}`);
    const description = await screen.findByRole("textbox", { name: "Description" });
    await user.type(description, " (draft)");
    await user.click(screen.getByRole("tab", { name: "Members" }));
    await user.click(await screen.findByRole("button", { name: "Add people" }));
    const picker = screen.getByRole("combobox", { name: "People to add" });
    await user.click(picker);
    await user.type(picker, "pe");
    await user.click(await screen.findByRole("option", { name: /Person 7/ }));
    await user.click(screen.getByRole("button", { name: "Add" }));
    expect(await findToast("1 person added.")).toBeInTheDocument();
    await user.click(screen.getByRole("tab", { name: "Details" }));
    expect(await screen.findByRole("textbox", { name: "Description" })).toHaveValue(
      "Shared notes for the field team (draft)",
    );
    await user.click(screen.getByRole("button", { name: "Save changes" }));
    expect(await findToast("Changes saved.")).toBeInTheDocument();
    expect(patches).toEqual([
      {
        body: { description: "Shared notes for the field team (draft)" },
        ifMatch: `"${rowVersion(2)}"`,
      },
    ]);
  });

  test("T-397 a 412 keeps the typed values, shows each changed field's latest value, and the next save sends the new version", async () => {
    let reads = 0;
    const patches = projectApi(
      () => {
        reads += 1;
        return reads === 1
          ? adminProject()
          : adminProject({ name: "Renamed", instructions: "New rules", rowVersion: rowVersion(5) });
      },
      ({ ifMatch, body }) =>
        ifMatch === `"${rowVersion(1)}"`
          ? problemResponse(412, "precondition_failed", {
              title: "Changed by someone else",
              detail: "Someone else changed this item.",
            })
          : adminProject({ ...body, rowVersion: rowVersion(6) }),
    );
    const { user } = renderAdmin(ROUTES, `/admin/projects/${PROJECT}`);
    const description = await screen.findByRole("textbox", { name: "Description" });
    await user.clear(description);
    await user.type(description, "Mine");
    await user.click(screen.getByRole("button", { name: "Save changes" }));
    expect(await screen.findByText("Changed by someone else")).toBeInTheDocument();
    expect(
      screen.getByText(
        "Your edits are still here. Fields that changed show the latest saved value. Review them, then save to replace them.",
      ),
    ).toBeInTheDocument();
    expect(await screen.findByText("Latest saved value: Renamed")).toBeInTheDocument();
    expect(screen.getByText("Latest saved value: New rules")).toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: "Description" })).toHaveValue("Mine");
    expect(screen.getByRole("textbox", { name: /^Name/ })).toHaveValue("Field notes");
    const useLatest = screen.getAllByRole("button", { name: "Use latest value" });
    await user.click(nth(useLatest, 0));
    expect(screen.getByRole("textbox", { name: /^Name/ })).toHaveValue("Renamed");
    expect(screen.queryByText("Latest saved value: Renamed")).toBeNull();
    await user.click(screen.getByRole("button", { name: "Save changes" }));
    expect(await findToast("Changes saved.")).toBeInTheDocument();
    expect(patches[1]).toEqual({
      body: { description: "Mine", instructions: "Explain acronyms." },
      ifMatch: `"${rowVersion(5)}"`,
    });
  });

  test("T-364 a 429 on Save changes shows the time beside Save, keeps the values and Save disabled for 20 s", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      projectApi(
        () => adminProject(),
        () =>
          problemResponse(429, "write_rate_limited", {
            title: "Too many changes",
            detail: "You're making changes too quickly. Wait a moment, then retry.",
            retryAfterSeconds: 20,
          }),
      );
      const { user } = renderAdmin(ROUTES, `/admin/projects/${PROJECT}`);
      const name = await screen.findByRole("textbox", { name: /^Name/ });
      await user.type(name, " 2");
      const sentAt = Date.now();
      await user.click(screen.getByRole("button", { name: "Save changes" }));
      expect(await screen.findByText("Too many changes")).toBeInTheDocument();
      expect(
        screen.getByText(`You can retry at ${formatRetryTime(sentAt + 20_000, sentAt)}.`, {
          exact: false,
        }),
      ).toBeInTheDocument();
      expect(name).toHaveValue("Field notes 2");
      expect(screen.getByRole("button", { name: "Save changes" })).toBeDisabled();
      await act(() => vi.advanceTimersByTimeAsync(19_000));
      expect(screen.getByRole("button", { name: "Save changes" })).toBeDisabled();
      await act(() => vi.advanceTimersByTimeAsync(2_000));
      expect(screen.getByRole("button", { name: "Save changes" })).toBeEnabled();
    } finally {
      vi.useRealTimers();
    }
  });

  test("T-361 a 422 at body.exampleQuestions.2 marks the third example question", async () => {
    projectApi(
      () => adminProject({ exampleQuestions: ["One", "Two", "Three"] }),
      () =>
        problemResponse(422, "validation_failed", {
          errors: [
            { path: "body.exampleQuestions.2", reason: "too_long" },
            { path: "header.If-Match", reason: "invalid_format" },
          ],
        }),
    );
    const { user } = renderAdmin(ROUTES, `/admin/projects/${PROJECT}`);
    const third = await screen.findByRole("textbox", { name: "Example question 3" });
    await user.type(third, "!");
    await user.click(screen.getByRole("button", { name: "Save changes" }));
    expect(await screen.findByText("This can be up to 160 characters.")).toBeInTheDocument();
    expect(third).toHaveAttribute("aria-invalid", "true");
    expect(third).toHaveFocus();
    expect(screen.getByText("header.If-Match: invalid_format")).toBeInTheDocument();
    // Typing in the field clears its error.
    await user.type(third, "?");
    expect(third).not.toHaveAttribute("aria-invalid", "true");
  });

  test("409 project_name_taken goes under Name; 404 offers the way back; 428 adds the defect line", async () => {
    let answer = 0;
    projectApi(
      () => adminProject(),
      () => {
        answer += 1;
        if (answer === 1) {
          return problemResponse(409, "project_name_taken", {
            detail: "Another project already uses this name.",
          });
        }
        if (answer === 2) return problemResponse(404, "project_not_found", { title: "Gone" });
        return problemResponse(428, "precondition_required", { title: "Version needed" });
      },
    );
    const { user } = renderAdmin(ROUTES, `/admin/projects/${PROJECT}`);
    await user.type(await screen.findByRole("textbox", { name: /^Name/ }), "!");
    await user.click(screen.getByRole("button", { name: "Save changes" }));
    expect(await screen.findByText("Another project already uses this name.")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Save changes" }));
    expect(await screen.findByText("Gone")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Back to Projects" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Save changes" }));
    expect(await screen.findByText("Version needed")).toBeInTheDocument();
    expect(screen.getByText(/Reload the page\. If this keeps happening/)).toBeInTheDocument();
  });

  test("Archive project confirms and sends the status alone; Restore project sends it back", async () => {
    let status: "active" | "archived" = "active";
    const patches = projectApi(
      () => adminProject({ status }),
      ({ body }) => {
        status = body["status"] as typeof status;
        return adminProject({ status, rowVersion: rowVersion(status === "archived" ? 2 : 3) });
      },
    );
    const { user } = renderAdmin(ROUTES, `/admin/projects/${PROJECT}`);
    await user.click(await screen.findByRole("button", { name: "Archive project" }));
    const dialog = await screen.findByRole("dialog", { name: "Archive Field notes?" });
    await user.click(within(dialog).getByRole("button", { name: "Archive" }));
    expect(await findToast("Project archived.")).toBeInTheDocument();
    expect(await screen.findByRole("button", { name: "Restore project" })).toBeInTheDocument();
    expect(screen.getByText("Archived")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Restore project" }));
    expect(await findToast("Project restored.")).toBeInTheDocument();
    expect(patches).toEqual([
      { body: { status: "archived" }, ifMatch: `"${rowVersion(1)}"` },
      { body: { status: "active" }, ifMatch: `"${rowVersion(2)}"` },
    ]);
  });

  test("a 412 on Archive shows the problem's text with admin.conflict.action and reloads", async () => {
    let reads = 0;
    projectApi(
      () => {
        reads += 1;
        return adminProject({ rowVersion: rowVersion(reads) });
      },
      () => problemResponse(412, "precondition_failed", { title: "Changed by someone else" }),
    );
    const { user } = renderAdmin(ROUTES, `/admin/projects/${PROJECT}`);
    await user.click(await screen.findByRole("button", { name: "Archive project" }));
    await user.click(
      within(await screen.findByRole("dialog")).getByRole("button", { name: "Archive" }),
    );
    expect(
      await screen.findByText(
        "The latest version is shown now. Check it, then repeat the action if you still need it.",
      ),
    ).toBeInTheDocument();
    await waitFor(() => {
      expect(reads).toBe(2);
    });
  });
});
