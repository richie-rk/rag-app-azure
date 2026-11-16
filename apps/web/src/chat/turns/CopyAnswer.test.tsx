import { FluentProvider, webLightTheme } from "@fluentui/react-components";
import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, test, vi } from "vitest";

import { LiveRegions } from "../../a11y/LiveRegions";
import { PLAIN_TEXT_ANSWER_ABOVE } from "../../platform/constants";
import { CopyAnswer } from "./CopyAnswer";

function show(text: string, writeText: (value: string) => Promise<void>) {
  Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText } });
  render(
    <FluentProvider theme={webLightTheme}>
      <CopyAnswer
        text={text}
        citations={[{ number: 1, title: "Guide" }]}
        allowedLinks={["https://docs.example/a"]}
      />
      <LiveRegions />
    </FluentProvider>,
  );
  return screen.getByRole("button", { name: "Copy answer" });
}

afterEach(() => {
  vi.useRealTimers();
});

describe("T-386 Copy answer", () => {
  test("T-386 writes exactly the plain text, announces it and confirms for 2,000 ms", async () => {
    const user = userEvent.setup({ advanceTimers: (ms) => vi.advanceTimersByTime(ms) });
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const writeText = vi.fn(() => Promise.resolve());
    const button = show(
      "See [docs](https://docs.example/a)⟦1⟧ and https://evil.example/c?d=secret.",
      writeText,
    );
    await user.click(button);
    expect(writeText).toHaveBeenCalledWith(
      "See docs (https://docs.example/a) [1] and [link removed].\n",
    );
    act(() => {
      vi.advanceTimersByTime(60);
    });
    expect(screen.getByRole("status")).toHaveTextContent("Answer copied");
    act(() => {
      vi.advanceTimersByTime(2_000);
    });
    expect(
      screen.queryByText("Couldn't copy the answer. Select the text and copy it instead."),
    ).toBeNull();
  });

  test("T-386 a rejected write shows the inline failure (clipboard_failed)", async () => {
    const user = userEvent.setup();
    const button = show("Answer.", () => Promise.reject(new Error("denied")));
    await user.click(button);
    expect(
      await screen.findByText("Couldn't copy the answer. Select the text and copy it instead."),
    ).toBeInTheDocument();
  });

  test("T-386 an answer longer than 200,000 characters writes nothing and fails", async () => {
    const user = userEvent.setup();
    const writeText = vi.fn(() => Promise.resolve());
    const button = show("a".repeat(PLAIN_TEXT_ANSWER_ABOVE + 1), writeText);
    await user.click(button);
    expect(writeText).not.toHaveBeenCalled();
    expect(
      screen.getByText("Couldn't copy the answer. Select the text and copy it instead."),
    ).toBeInTheDocument();
  });
});
