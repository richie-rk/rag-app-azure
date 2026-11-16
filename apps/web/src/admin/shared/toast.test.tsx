import { Button } from "@fluentui/react-components";
import { fireEvent, screen, waitFor } from "@testing-library/react";
import { describe, expect, test } from "vitest";

import { renderRoutes } from "../../test/render";
import { AdminLayout } from "./AdminLayout";
import { useAdminToast } from "./toast";

function Sender() {
  const toast = useAdminToast();
  return (
    <>
      <Button
        onClick={() => {
          toast("Index mapped.");
        }}
      >
        plain
      </Button>
      <Button
        onClick={() => {
          toast("Re-ingest started.", { label: "View progress", to: "/admin/runs/r1" });
        }}
      >
        linked
      </Button>
    </>
  );
}

describe("admin toasts", () => {
  test("a success toast shows in the admin layout's toaster and is announced politely", async () => {
    const { user } = renderRoutes(
      [{ element: <AdminLayout />, children: [{ path: "/admin/x", element: <Sender /> }] }],
      { route: "/admin/x" },
    );
    await user.click(screen.getByRole("button", { name: "plain" }));
    expect(await screen.findByText("Index mapped.")).toBeInTheDocument();
    await waitFor(() => {
      expect(document.querySelector("[role='status']")).toHaveTextContent("Index mapped.");
    });
  });

  test("a toast can carry a link, and focus inside pauses it", async () => {
    const { user } = renderRoutes(
      [
        {
          element: <AdminLayout />,
          children: [
            { path: "/admin/x", element: <Sender /> },
            { path: "/admin/runs/:runId", element: <p>run page</p> },
          ],
        },
      ],
      { route: "/admin/x" },
    );
    await user.click(screen.getByRole("button", { name: "linked" }));
    const link = await screen.findByRole("link", { name: "View progress" });
    expect(link).toHaveAttribute("href", "/admin/runs/r1");
    const toast = link.closest("[role='listitem']");
    if (toast !== null) {
      fireEvent.focusIn(toast);
      fireEvent.focusOut(toast);
    }
    await user.click(link);
    expect(await screen.findByText("run page")).toBeInTheDocument();
    expect(screen.getByText("Re-ingest started.")).toBeInTheDocument();
  });
});
