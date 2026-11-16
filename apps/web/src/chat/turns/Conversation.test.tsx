import { fireEvent, screen } from "@testing-library/react";
import { useState } from "react";
import { describe, expect, test, vi } from "vitest";

import { clientError } from "../../api/problem";
import { liveTurn, present, storedTurn } from "../../test/chat";
import { CURRENT_USER, IDS } from "../../test/handlers";
import { configureTestHttp, renderWithProviders } from "../../test/render";
import { Conversation, type ConversationProps } from "./Conversation";
import { turnModel } from "./turnModel";

function props(overrides: Partial<ConversationProps> = {}): ConversationProps {
  return {
    sessionId: IDS.session,
    turns: [],
    loading: false,
    loadError: null,
    onReload: vi.fn(),
    earlier: { available: false, loading: false, error: null, load: vi.fn() },
    empty: <p>Nothing yet</p>,
    sendingBlocked: false,
    composerHeight: 80,
    onRetry: vi.fn(),
    onNewChat: vi.fn(),
    onCitation: vi.fn(),
    pollSession: vi.fn(() => Promise.resolve(true)),
    ...overrides,
  };
}

function show(value: ConversationProps) {
  configureTestHttp();
  return renderWithProviders(<Conversation {...value} />, {
    currentUser: { ...CURRENT_USER, roles: ["User"] },
  });
}

function geometry(element: HTMLElement, values: { scrollHeight: number; clientHeight: number }) {
  Object.defineProperty(element, "scrollHeight", {
    configurable: true,
    get: () => values.scrollHeight,
  });
  Object.defineProperty(element, "clientHeight", {
    configurable: true,
    value: values.clientHeight,
  });
}

const stored = (seq: number) =>
  turnModel(
    { kind: "stored", key: `k${String(seq)}`, turn: storedTurn({ seq }) },
    IDS.session,
    IDS.projectA,
  );

describe("the conversation", () => {
  test("is a section labelled Conversation, oldest turn first", () => {
    show(props({ turns: [stored(1), stored(2)] }));
    const section = screen.getByRole("region", { name: "Conversation" });
    expect(section).toBeInTheDocument();
    expect(
      screen
        .getAllByRole("article")
        .map((article) => article.getAttribute("aria-labelledby") === null),
    ).toEqual([false, false]);
    expect(screen.getAllByRole("article")[0]).toHaveAccessibleName("Question 1");
  });

  test("shows three skeleton blocks while the session loads", () => {
    const { result } = show(props({ loading: true }));
    expect(result.container.querySelectorAll(".fui-SkeletonItem")).toHaveLength(3);
  });

  test("a failed session read shows an error bar with Retry", () => {
    const onReload = vi.fn();
    show(props({ loadError: clientError("network"), onReload }));
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(onReload).toHaveBeenCalled();
  });

  test("shows the empty content without turns", () => {
    show(props());
    expect(screen.getByText("Nothing yet")).toBeInTheDocument();
  });

  test("Show earlier messages loads the page above and keeps the visible content still", () => {
    const load = vi.fn();
    const before = props({
      turns: [stored(31)],
      earlier: { available: true, loading: false, error: null, load },
    });
    const after = {
      ...before,
      turns: [stored(30), stored(31)],
      earlier: { ...before.earlier, available: false },
    };
    function Switcher() {
      const [value, setValue] = useState(before);
      return (
        <>
          <button
            type="button"
            onClick={() => {
              setValue(after);
            }}
          >
            page arrived
          </button>
          <Conversation {...value} />
        </>
      );
    }
    configureTestHttp();
    renderWithProviders(<Switcher />, { currentUser: { ...CURRENT_USER, roles: ["User"] } });
    const scroller = present(screen.getByRole("region", { name: "Conversation" }).parentElement);
    const sizes = { scrollHeight: 1_000, clientHeight: 400 };
    geometry(scroller, sizes);
    scroller.scrollTop = 100;
    fireEvent.click(screen.getByRole("button", { name: "Show earlier messages" }));
    expect(load).toHaveBeenCalled();
    sizes.scrollHeight = 1_600;
    fireEvent.click(screen.getByRole("button", { name: "page arrived" }));
    expect(scroller.scrollTop).toBe(700);
  });

  test("a failed earlier page shows chat.earlier.error at the top with Retry", () => {
    const load = vi.fn();
    show(
      props({
        turns: [stored(31)],
        earlier: { available: true, loading: false, error: clientError("network"), load },
      }),
    );
    expect(screen.getByText("Earlier messages didn't load.")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(load).toHaveBeenCalled();
  });

  test("Jump to latest appears while an answer streams and the user has scrolled up", () => {
    const streaming = turnModel(
      { kind: "live", key: "live", turn: liveTurn({ phase: "streaming", citations: null }) },
      IDS.session,
      IDS.projectA,
    );
    show(props({ turns: [stored(1), streaming] }));
    const scroller = present(screen.getByRole("region", { name: "Conversation" }).parentElement);
    geometry(scroller, { scrollHeight: 2_000, clientHeight: 400 });
    expect(scroller.style.scrollPaddingBottom).toBe("96px");
    expect(scroller.style.scrollPaddingTop).toBe("48px");
    expect(screen.queryByRole("button", { name: "Jump to latest" })).toBeNull();
    scroller.scrollTop = 500;
    fireEvent.scroll(scroller);
    const jump = screen.getByRole("button", { name: "Jump to latest" });
    const scrollTo = vi.fn();
    scroller.scrollTo = scrollTo;
    fireEvent.click(jump);
    expect(scrollTo).toHaveBeenCalledWith({ top: 2_000, behavior: "smooth" });
    expect(screen.queryByRole("button", { name: "Jump to latest" })).toBeNull();
    scroller.scrollTop = 1_550;
    fireEvent.scroll(scroller);
    expect(screen.queryByRole("button", { name: "Jump to latest" })).toBeNull();
  });
});
