import { screen } from "@testing-library/react";
import { describe, expect, test, vi } from "vitest";

import { IDS } from "../test/handlers";
import { renderWithProviders } from "../test/render";
import { NavPanel, selectedNavValue, type RecentChatsState } from "./NavPanel";

const recent = (count: number): RecentChatsState => ({
  status: "success",
  items: Array.from({ length: count }, (_, index) => ({
    sessionId: `00000000-0000-4000-8000-${String(index).padStart(12, "0")}`,
    title: `Chat ${String(index + 1)}`,
  })),
});

function renderPanel(options: {
  isAdmin?: boolean;
  recentChats?: RecentChatsState;
  route?: string;
  type?: "inline" | "overlay";
}) {
  const onOpenChange = vi.fn();
  const harness = renderWithProviders(
    <NavPanel
      open
      type={options.type ?? "inline"}
      isAdmin={options.isAdmin ?? false}
      recentChats={options.recentChats ?? recent(0)}
      onOpenChange={onOpenChange}
    />,
    { route: options.route ?? "/chat" },
  );
  return { ...harness, onOpenChange };
}

describe("the navigation drawer", () => {
  test("is the main navigation landmark with Chat, Memory, Recent chats and New chat", () => {
    renderPanel({});
    expect(screen.getByRole("navigation", { name: "Main navigation" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Chat" })).toHaveAttribute("href", "/chat");
    expect(screen.getByRole("link", { name: "Memory" })).toHaveAttribute("href", "/memory");
    expect(screen.getByText("Recent chats")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "New chat" })).toBeInTheDocument();
    expect(screen.queryByText("Administration")).toBeNull();
    expect(screen.queryByRole("link", { name: "Projects" })).toBeNull();
  });

  test("T-352 admins see the Administration section first", () => {
    renderPanel({ isAdmin: true });
    const links = screen.getAllByRole("link").map((link) => link.textContent);
    expect(links.slice(0, 5)).toEqual(["Projects", "Indexes", "Ingestion runs", "Chat", "Memory"]);
    expect(screen.getByText("Administration")).toBeInTheDocument();
  });

  test("Recent chats shows the six newest sessions, the open one selected", () => {
    const state = recent(8);
    const open = state.status === "success" ? state.items[1]?.sessionId : undefined;
    renderPanel({ recentChats: state, route: `/chat/${open ?? ""}` });
    const chats = screen.getAllByRole("link", { name: /^Chat \d$/ });
    expect(chats.map((link) => link.textContent)).toEqual([
      "Chat 1",
      "Chat 2",
      "Chat 3",
      "Chat 4",
      "Chat 5",
      "Chat 6",
    ]);
    expect(screen.getByRole("link", { name: "Chat 2" })).toHaveAttribute("aria-current", "page");
  });

  test("Recent chats: a skeleton while loading, a note when empty or failed", () => {
    const { result } = renderPanel({ recentChats: { status: "pending" } });
    expect(screen.queryByText("No chats yet.")).toBeNull();
    result.unmount();
    renderPanel({ recentChats: recent(0) }).result.unmount();
    renderPanel({ recentChats: { status: "error" } });
    expect(screen.getByText("Recent chats didn't load.")).toBeInTheDocument();
  });

  test("an empty Recent chats says so", () => {
    renderPanel({ recentChats: recent(0) });
    expect(screen.getByText("No chats yet.")).toBeInTheDocument();
  });

  test("a link navigates in the app and closes an overlay", async () => {
    const { user, router, onOpenChange } = renderPanel({
      type: "overlay",
      route: "/chat?project=p1",
    });
    expect(screen.getByRole("link", { name: "New chat" })).toHaveAttribute(
      "href",
      "/chat?project=p1",
    );
    await user.click(screen.getByRole("link", { name: "Memory" }));
    expect(router.state.location.pathname).toBe("/memory");
    expect(onOpenChange).toHaveBeenCalledWith(false);
  });

  test("the overlay is the main navigation landmark without aria-modal, which the role disallows", () => {
    renderPanel({ type: "overlay" });
    const nav = screen.getByRole("navigation", { name: "Main navigation" });
    expect(nav).not.toHaveAttribute("aria-modal");
    expect(nav).toHaveAttribute("role", "navigation");
  });

  test("an inline drawer stays open after navigating, and a modified click is left to the browser", async () => {
    const { user, router, onOpenChange } = renderPanel({ type: "inline" });
    await user.keyboard("{Shift>}");
    await user.click(screen.getByRole("link", { name: "Memory" }));
    await user.keyboard("{/Shift}");
    expect(router.state.location.pathname).toBe("/chat");
    await user.click(screen.getByRole("link", { name: "Memory" }));
    expect(router.state.location.pathname).toBe("/memory");
    expect(onOpenChange).not.toHaveBeenCalled();
  });

  test("the route selects its item", () => {
    const items = [{ sessionId: IDS.session, title: "Open" }];
    expect(selectedNavValue("/admin/projects/1", items)).toBe("projects");
    expect(selectedNavValue("/admin/indexes", items)).toBe("indexes");
    expect(selectedNavValue("/admin/runs/2", items)).toBe("runs");
    expect(selectedNavValue("/memory", items)).toBe("memory");
    expect(selectedNavValue(`/chat/${IDS.session}`, items)).toBe(`session:${IDS.session}`);
    expect(selectedNavValue(`/chat/${IDS.turn}`, items)).toBe("chat");
    expect(selectedNavValue("/chat", items)).toBe("chat");
    expect(selectedNavValue("/elsewhere", items)).toBeUndefined();
  });
});
