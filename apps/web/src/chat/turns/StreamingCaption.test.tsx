import { FluentProvider, webLightTheme } from "@fluentui/react-components";
import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

import { POLL_SESSION_MAX_MS, POLL_SESSION_MS } from "../../platform/constants";
import { StreamingCaption } from "./StreamingCaption";

function show(poll: () => Promise<boolean>, polls = true) {
  return render(
    <FluentProvider theme={webLightTheme}>
      <StreamingCaption polls={polls} poll={poll} />
    </FluentProvider>,
  );
}

async function advance(ms: number) {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
}

beforeEach(() => {
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
  Object.defineProperty(document, "visibilityState", { configurable: true, value: "visible" });
});

describe("T-375 the streaming-turn poll", () => {
  test("T-375 polls every 5,000 ms and stops at 250 s with the Refresh state", async () => {
    const poll = vi.fn(() => Promise.resolve(true));
    show(poll);
    expect(screen.getByText("This answer is still being written.")).toBeInTheDocument();
    await advance(POLL_SESSION_MS - 1);
    expect(poll).not.toHaveBeenCalled();
    await advance(1);
    expect(poll).toHaveBeenCalledTimes(1);
    await advance(POLL_SESSION_MS);
    expect(poll).toHaveBeenCalledTimes(2);
    await advance(POLL_SESSION_MAX_MS);
    expect(
      screen.getByText("This answer is taking longer than expected. Refresh to check on it."),
    ).toBeInTheDocument();
    const calls = poll.mock.calls.length;
    expect(calls).toBeLessThanOrEqual(POLL_SESSION_MAX_MS / POLL_SESSION_MS);
    await advance(POLL_SESSION_MS * 3);
    expect(poll).toHaveBeenCalledTimes(calls);
    fireEvent.click(screen.getByRole("button", { name: "Refresh" }));
    await advance(0);
    expect(poll).toHaveBeenCalledTimes(calls + 1);
    expect(screen.getByText("This answer is still being written.")).toBeInTheDocument();
    await advance(POLL_SESSION_MS);
    expect(poll).toHaveBeenCalledTimes(calls + 2);
  });

  test("T-375 3 failed polls in a row show the Refresh state; a success resets the count", async () => {
    const results = [false, false, true, false, false, false];
    const poll = vi.fn(() => Promise.resolve(results.shift() ?? false));
    show(poll);
    await advance(POLL_SESSION_MS * 5);
    expect(screen.queryByRole("button", { name: "Refresh" })).toBeNull();
    await advance(POLL_SESSION_MS);
    expect(screen.getByRole("button", { name: "Refresh" })).toBeInTheDocument();
    expect(poll).toHaveBeenCalledTimes(6);
  });

  test("T-375 Auto-refresh off and a hidden tab pause polling", async () => {
    const poll = vi.fn(() => Promise.resolve(true));
    show(poll);
    fireEvent.click(screen.getByRole("switch", { name: "Auto-refresh" }));
    await advance(POLL_SESSION_MS * 3);
    expect(poll).not.toHaveBeenCalled();
    Object.defineProperty(document, "visibilityState", { configurable: true, value: "hidden" });
    fireEvent.click(screen.getByRole("switch", { name: "Auto-refresh" }));
    await advance(POLL_SESSION_MS * 3);
    expect(poll).not.toHaveBeenCalled();
    Object.defineProperty(document, "visibilityState", { configurable: true, value: "visible" });
    await advance(POLL_SESSION_MS);
    expect(poll).toHaveBeenCalledTimes(1);
  });

  test("T-375 an older streaming turn shows its caption without polling", async () => {
    const poll = vi.fn(() => Promise.resolve(true));
    show(poll, false);
    await advance(POLL_SESSION_MS * 2);
    expect(poll).not.toHaveBeenCalled();
    expect(screen.queryByRole("switch")).toBeNull();
  });

  test("T-375 unmounting ends the poll", async () => {
    let resolve: (value: boolean) => void = () => undefined;
    const poll = vi.fn(
      () =>
        new Promise<boolean>((done) => {
          resolve = done;
        }),
    );
    const { unmount } = show(poll);
    await advance(POLL_SESSION_MS);
    unmount();
    resolve(true);
    await advance(POLL_SESSION_MS * 2);
    expect(poll).toHaveBeenCalledTimes(1);
  });
});
