import { Input } from "@fluentui/react-components";
import { screen } from "@testing-library/react";
import { describe, expect, test, vi } from "vitest";

import { renderWithProviders } from "../../test/render";
import { AdminField } from "./AdminField";
import { LinkButton } from "./LinkButton";

describe("admin fields", () => {
  test("a field has its label, hint, error and, after a 412, the latest saved value", async () => {
    const onUseLatest = vi.fn();
    const { user } = renderWithProviders(
      <AdminField
        label="Name"
        hint="Shown to people."
        required
        error="Enter a name."
        conflict={{ value: "Theirs", onUseLatest }}
      >
        <Input />
      </AdminField>,
    );
    const input = screen.getByRole("textbox", { name: /Name/ });
    expect(input).toBeRequired();
    expect(input).toHaveAttribute("aria-invalid", "true");
    expect(screen.getByText("Shown to people.")).toBeInTheDocument();
    expect(screen.getByText("Enter a name.")).toBeInTheDocument();
    expect(screen.getByText("Latest saved value: Theirs")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Use latest value" }));
    expect(onUseLatest).toHaveBeenCalled();
  });

  test("a field without error or conflict shows neither", () => {
    renderWithProviders(
      <AdminField label="Description">
        <Input />
      </AdminField>,
    );
    expect(screen.getByRole("textbox", { name: "Description" })).not.toHaveAttribute(
      "aria-invalid",
      "true",
    );
    expect(screen.queryByText(/Latest saved value/)).toBeNull();
  });

  test("a link button navigates in the app and keeps a real href", async () => {
    const { user, router } = renderWithProviders(
      <LinkButton to="/admin/projects/new">New project</LinkButton>,
      { route: "/admin/projects" },
    );
    const link = screen.getByRole("link", { name: "New project" });
    expect(link).toHaveAttribute("href", "/admin/projects/new");
    await user.click(link);
    expect(router.state.location.pathname).toBe("/admin/projects/new");
  });
});
