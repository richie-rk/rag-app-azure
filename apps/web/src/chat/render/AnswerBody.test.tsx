import { FluentProvider, webLightTheme } from "@fluentui/react-components";
import { act, fireEvent, render, screen } from "@testing-library/react";
import type { ReactElement } from "react";
import { afterEach, describe, expect, test, vi } from "vitest";

import { PLAIN_TEXT_ANSWER_ABOVE, RENDER_THROTTLE_MS } from "../../platform/constants";
import { AnswerBody, type AnswerBodyProps } from "./AnswerBody";

function show(ui: ReactElement) {
  return render(<FluentProvider theme={webLightTheme}>{ui}</FluentProvider>);
}

const citations = [{ number: 1, title: "Expenses guide" }];

function props(overrides: Partial<AnswerBodyProps> = {}): AnswerBodyProps {
  return { text: "", citations: null, allowedLinks: null, streaming: false, ...overrides };
}

afterEach(() => {
  vi.useRealTimers();
});

describe("AnswerBody", () => {
  test("inserts the sanitised answer, with no aria-live on its container", () => {
    const { container } = show(<AnswerBody {...props({ text: "**Bold** <img src=x>" })} />);
    expect(container.querySelector("strong")?.textContent).toBe("Bold");
    expect(container.querySelector("img")).toBeNull();
    expect(container.querySelector("[aria-live]")).toBeNull();
  });

  test("a citation button opens its source through the one click listener", () => {
    const onCitation = vi.fn();
    show(<AnswerBody {...props({ text: "Claim ⟦1⟧.", citations, onCitation })} />);
    const button = screen.getByRole("button", { name: "Source 1: Expenses guide" });
    fireEvent.click(button);
    expect(onCitation).toHaveBeenCalledWith(1, button);
    fireEvent.click(screen.getByText(/Claim/));
    expect(onCitation).toHaveBeenCalledTimes(1);
  });

  test("a click without a handler does nothing", () => {
    show(<AnswerBody {...props({ text: "Claim ⟦1⟧.", citations })} />);
    const button = screen.getByRole("button", { name: "Source 1: Expenses guide" });
    fireEvent.click(button);
    expect(button).toBeInTheDocument();
  });

  test("T-385 an answer longer than 200,000 characters shows as plain text with no HTML", () => {
    const text = `**a** ⟦1⟧ https://evil.example/x?d=secret ${"b".repeat(PLAIN_TEXT_ANSWER_ABOVE)}`;
    const { container } = show(<AnswerBody {...props({ text })} />);
    expect(container.querySelector("strong")).toBeNull();
    expect(container.textContent.startsWith("**a** [1] [link removed] b")).toBe(true);
  });

  test("while streaming the pipeline runs at most once per 100 ms, and at once when it stops", () => {
    vi.useFakeTimers();
    const { container, rerender } = show(
      <AnswerBody {...props({ text: "One", streaming: true })} />,
    );
    expect(container.textContent).toBe("One");
    const update = (text: string, streaming = true) => {
      rerender(
        <FluentProvider theme={webLightTheme}>
          <AnswerBody {...props({ text, streaming })} />
        </FluentProvider>,
      );
    };
    update("One two");
    act(() => {
      vi.advanceTimersByTime(0);
    });
    expect(container.textContent).toBe("One two");
    update("One two three");
    act(() => {
      vi.advanceTimersByTime(RENDER_THROTTLE_MS / 2);
    });
    expect(container.textContent).toBe("One two");
    update("One two three four");
    act(() => {
      vi.advanceTimersByTime(RENDER_THROTTLE_MS / 2);
    });
    expect(container.textContent).toBe("One two three four");
    update("Done.", false);
    expect(container.textContent).toBe("Done.");
  });
});
