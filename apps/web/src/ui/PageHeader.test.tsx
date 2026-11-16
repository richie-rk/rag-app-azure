import { screen } from "@testing-library/react";
import { useNavigate } from "react-router";
import { describe, expect, test } from "vitest";

import { renderWithProviders } from "../test/render";
import { PageHeader, ScreenHeading, documentTitle, useDocumentTitle } from "./PageHeader";
import { RouterLink, isModifiedClick } from "./RouterLink";

function TitledScreen({ parts }: { readonly parts: readonly string[] }) {
  useDocumentTitle(...parts);
  return <ScreenHeading>{parts[0] ?? "Home"}</ScreenHeading>;
}

function Navigator() {
  const navigate = useNavigate();
  return (
    <>
      <ScreenHeading visuallyHidden>Chat</ScreenHeading>
      <button type="button" onClick={() => void navigate("/chat/other")}>
        go
      </button>
    </>
  );
}

describe("headings and titles", () => {
  test("the document title is <parts> · DocChat, or DocChat alone", () => {
    expect(documentTitle()).toBe("DocChat");
    expect(documentTitle("Projects")).toBe("Projects · DocChat");
    expect(documentTitle("Alpha", "Projects")).toBe("Alpha · Projects · DocChat");
    renderWithProviders(<TitledScreen parts={["Documents", "Manuals"]} />);
    expect(document.title).toBe("Documents · Manuals · DocChat");
  });

  test("the screen's h1 takes focus on arrival and on every route change", async () => {
    const { user } = renderWithProviders(<Navigator />, { route: "/chat/one" });
    const heading = screen.getByRole("heading", { level: 1, name: "Chat" });
    expect(heading).toHaveAttribute("tabindex", "-1");
    expect(heading).toHaveFocus();
    await user.click(screen.getByRole("button", { name: "go" }));
    expect(heading).toHaveFocus();
  });

  test("the page header shows a breadcrumb that navigates, the h1 and its actions", async () => {
    const { user, router } = renderWithProviders(
      <PageHeader
        title="Alpha"
        breadcrumbs={[{ label: "Projects", to: "/admin/projects" }, { label: "Alpha" }]}
        actions={<span>Archived</span>}
      />,
      { route: "/admin/projects/1" },
    );
    expect(screen.getByRole("heading", { level: 1, name: "Alpha" })).toBeInTheDocument();
    expect(screen.getByText("Archived")).toBeInTheDocument();
    const crumb = screen.getByRole("link", { name: "Projects" });
    expect(crumb).toHaveAttribute("href", "/admin/projects");
    await user.click(crumb);
    expect(router.state.location.pathname).toBe("/admin/projects");
  });

  test("a page header without a breadcrumb or actions shows the h1 alone", () => {
    renderWithProviders(<PageHeader title="Projects" breadcrumbs={[]} />);
    expect(screen.queryByRole("navigation")).toBeNull();
    expect(screen.getByRole("heading", { level: 1, name: "Projects" })).toBeInTheDocument();
  });

  test("a modified click on a breadcrumb or a link is left to the browser", async () => {
    const { user, router } = renderWithProviders(
      <>
        <PageHeader title="Alpha" breadcrumbs={[{ label: "Projects", to: "/admin/projects" }]} />
        <RouterLink to="/memory">Memory</RouterLink>
      </>,
      { route: "/start" },
    );
    await user.keyboard("{Control>}");
    await user.click(screen.getByRole("link", { name: "Projects" }));
    await user.click(screen.getByRole("link", { name: "Memory" }));
    await user.keyboard("{/Control}");
    expect(router.state.location.pathname).toBe("/start");
    await user.click(screen.getByRole("link", { name: "Memory" }));
    expect(router.state.location.pathname).toBe("/memory");
    expect(screen.getByRole("link", { name: "Memory" })).toHaveAttribute("href", "/memory");
  });

  test("a click is modified by any modifier key or another button", () => {
    const base = { button: 0, metaKey: false, altKey: false, ctrlKey: false, shiftKey: false };
    expect(isModifiedClick(base as never)).toBe(false);
    for (const change of [
      { button: 1 },
      { metaKey: true },
      { altKey: true },
      { ctrlKey: true },
      { shiftKey: true },
    ]) {
      expect(isModifiedClick({ ...base, ...change } as never)).toBe(true);
    }
  });
});
