import { screen } from "@testing-library/react";
import { describe, expect, test, vi } from "vitest";

import { renderWithProviders } from "../test/render";
import { TopBar, initials } from "./TopBar";

function renderBar(navOpen = true) {
  const onToggleNav = vi.fn();
  const onSignOut = vi.fn();
  const harness = renderWithProviders(
    <TopBar
      navOpen={navOpen}
      onToggleNav={onToggleNav}
      displayName="Ada Lovelace"
      email="ada@example.test"
      onSignOut={onSignOut}
    />,
    { route: "/memory" },
  );
  return { ...harness, onToggleNav, onSignOut };
}

describe("the top bar", () => {
  test("is a banner with the navigation toggle, the product mark linking to chat and the account", async () => {
    const { user, router, onToggleNav } = renderBar();
    expect(screen.getByRole("banner")).toBeInTheDocument();
    const toggle = screen.getByRole("button", { name: "Hide navigation" });
    await user.click(toggle);
    expect(onToggleNav).toHaveBeenCalled();
    await user.click(screen.getByRole("link", { name: "DocChat" }));
    expect(router.state.location.pathname).toBe("/chat");
  });

  test("the toggle offers Show navigation while the drawer is closed", () => {
    renderBar(false);
    expect(screen.getByRole("button", { name: "Show navigation" })).toHaveAttribute(
      "aria-expanded",
      "false",
    );
  });

  test("the theme toggle is pressed in the dark theme and remembers the choice", async () => {
    const { user } = renderBar();
    const toggle = screen.getByRole("button", { name: "Dark theme" });
    expect(toggle).toHaveAttribute("aria-pressed", "false");
    await user.click(toggle);
    expect(screen.getByRole("button", { name: "Dark theme" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    expect(window.localStorage.getItem("docchat.theme")).toBe("dark");
    await user.click(screen.getByRole("button", { name: "Dark theme" }));
    expect(window.localStorage.getItem("docchat.theme")).toBe("light");
  });

  test("the account menu shows the person and Sign out", async () => {
    const { user, onSignOut } = renderBar();
    await user.click(screen.getByRole("button", { name: "Account: Ada Lovelace" }));
    expect(await screen.findByText("ada@example.test")).toBeInTheDocument();
    await user.click(screen.getByRole("menuitem", { name: "Sign out" }));
    expect(onSignOut).toHaveBeenCalled();
  });

  test("the avatar shows up to two initials", () => {
    expect(initials("Ada Lovelace")).toBe("AL");
    expect(initials("  grace   brewster murray hopper ")).toBe("GB");
    expect(initials("Émile")).toBe("É");
    expect(initials("")).toBe("");
  });
});
