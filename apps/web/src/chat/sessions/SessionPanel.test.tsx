import { screen, within } from "@testing-library/react";
import { useInfiniteQuery } from "@tanstack/react-query";
import { HttpResponse, http } from "msw";
import { describe, expect, test, vi } from "vitest";

import { sessionsQuery } from "../../api/queryClient";
import { sessionSummary } from "../../test/chat";
import { IDS, PROJECTS, apiUrl, problemResponse } from "../../test/handlers";
import { configureTestHttp, renderWithProviders } from "../../test/render";
import { server } from "../../test/server";
import { SessionPanel } from "./SessionPanel";

const OTHER = "8f9bae5c-6d7e-4f80-8192-a3b4c5d6e7f8";

function Panel(props: { readonly onNavigate?: () => void; readonly projectId?: string | null }) {
  const sessions = useInfiniteQuery(sessionsQuery());
  return (
    <SessionPanel
      sessions={sessions}
      projects={PROJECTS.map((project) => ({
        ...project,
        exampleQuestions: [...project.exampleQuestions],
      }))}
      currentSessionId={IDS.session}
      answering={new Set([OTHER])}
      projectId={props.projectId ?? null}
      inline
      {...(props.onNavigate === undefined ? {} : { onNavigate: props.onNavigate })}
    />
  );
}

describe("the session panel", () => {
  test("lists sessions with their project and activity, marks the open one and live answers", async () => {
    configureTestHttp();
    server.use(
      http.get(apiUrl("/v1/sessions"), () =>
        HttpResponse.json({
          items: [
            sessionSummary(),
            sessionSummary({ sessionId: OTHER, title: "Other chat", projectId: IDS.projectB }),
          ],
          nextCursor: null,
        }),
      ),
    );
    const onNavigate = vi.fn();
    const { user, router } = renderWithProviders(<Panel onNavigate={onNavigate} />, {
      route: "/chat",
    });
    const panel = screen.getByRole("complementary", { name: "Chats" });
    const open = await within(panel).findByRole("link", { name: /Expense claims/ });
    expect(open).toHaveAttribute("aria-current", "page");
    expect(within(open).getByText("Alpha")).toBeInTheDocument();
    const other = within(panel).getByRole("link", { name: /Other chat/ });
    expect(other).not.toHaveAttribute("aria-current");
    expect(within(other).getByLabelText("Answering…")).toBeInTheDocument();
    await user.click(other);
    expect(router.state.location.pathname).toBe(`/chat/${OTHER}`);
    expect(onNavigate).toHaveBeenCalled();
  });

  test("New chat keeps the chosen project", async () => {
    configureTestHttp();
    const { user, router } = renderWithProviders(<Panel projectId={IDS.projectA} />, {
      route: "/chat/x",
    });
    await user.click(screen.getByRole("button", { name: "New chat" }));
    expect(router.state.location.pathname).toBe("/chat");
    expect(router.state.location.search).toBe(`?project=${IDS.projectA}`);
    await user.click(screen.getByRole("button", { name: "New chat" }));
  });

  test("shows a skeleton while loading, then the empty text", async () => {
    configureTestHttp();
    renderWithProviders(<Panel />, { route: "/chat" });
    expect(
      await screen.findByText("No chats yet. Your conversations will appear here."),
    ).toBeInTheDocument();
  });

  test("Show more chats loads the next page, de-duplicated by sessionId", async () => {
    configureTestHttp();
    server.use(
      http.get(apiUrl("/v1/sessions"), ({ request }) =>
        new URL(request.url).searchParams.get("cursor") === null
          ? HttpResponse.json({ items: [sessionSummary()], nextCursor: "page-2" })
          : HttpResponse.json({
              items: [sessionSummary(), sessionSummary({ sessionId: OTHER, title: "Older" })],
              nextCursor: null,
            }),
      ),
    );
    const { user } = renderWithProviders(<Panel />, { route: "/chat" });
    await user.click(await screen.findByRole("button", { name: "Show more chats" }));
    expect(await screen.findByRole("link", { name: /Older/ })).toBeInTheDocument();
    expect(screen.getAllByRole("link", { name: /Expense claims/ })).toHaveLength(1);
    expect(screen.queryByRole("button", { name: "Show more chats" })).toBeNull();
  });

  test("a failed list shows an error bar with Retry; a failed Show more one under the list", async () => {
    configureTestHttp();
    let calls = 0;
    server.use(
      http.get(apiUrl("/v1/sessions"), () => {
        calls += 1;
        if (calls === 1) return problemResponse(503, "dependency_unavailable");
        if (calls === 2)
          return HttpResponse.json({ items: [sessionSummary()], nextCursor: "page-2" });
        return problemResponse(503, "dependency_unavailable", { title: "Paging failed" });
      }),
    );
    const { user } = renderWithProviders(<Panel />, { route: "/chat" });
    expect(await screen.findByText("Your chats didn't load.")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Retry" }));
    await user.click(await screen.findByRole("button", { name: "Show more chats" }));
    expect(await screen.findByText("Paging failed")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Expense claims/ })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Retry" }));
  });
});
