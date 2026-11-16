import { act, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

import {
  STATUS_REFILL_MS,
  announceStatus,
  appendToAnswerLog,
  registerLiveRegions,
} from "./announce";
import { LiveRegions } from "./LiveRegions";

beforeEach(() => {
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
});

describe("the live regions", () => {
  test("T-410 both polite regions are present and empty from the first paint", () => {
    render(<LiveRegions />);
    const log = screen.getByRole("log");
    const status = screen.getByRole("status");
    expect(log).toHaveAttribute("aria-live", "polite");
    expect(status).toHaveAttribute("aria-live", "polite");
    expect(status).toHaveAttribute("aria-atomic", "true");
    expect(log).toBeEmptyDOMElement();
    expect(status).toBeEmptyDOMElement();
  });

  test("T-410 streamed text reaches the log sentence by sentence, keeping at most 20 children", () => {
    render(<LiveRegions />);
    const log = screen.getByRole("log");
    for (let index = 1; index <= 25; index += 1) appendToAnswerLog(`Sentence ${String(index)}.`);
    appendToAnswerLog("");
    expect(log.children).toHaveLength(20);
    expect(log.firstElementChild?.tagName).toBe("P");
    expect(log.firstElementChild?.textContent).toBe("Sentence 6.");
    expect(log.lastElementChild?.textContent).toBe("Sentence 25.");
  });

  test("T-410 status messages replace the status region's text, 50 ms after it is emptied", () => {
    render(<LiveRegions />);
    const status = screen.getByRole("status");
    announceStatus("Answer copied");
    expect(status).toHaveTextContent("");
    act(() => {
      vi.advanceTimersByTime(STATUS_REFILL_MS);
    });
    expect(status).toHaveTextContent("Answer copied");
    announceStatus("Answer copied");
    expect(status).toHaveTextContent("");
    act(() => {
      vi.advanceTimersByTime(STATUS_REFILL_MS);
    });
    expect(status).toHaveTextContent("Answer copied");
  });

  test("T-410 a newer message cancels a pending one", () => {
    render(<LiveRegions />);
    announceStatus("first");
    announceStatus("second");
    act(() => {
      vi.advanceTimersByTime(STATUS_REFILL_MS);
    });
    expect(screen.getByRole("status")).toHaveTextContent("second");
  });

  test("announcements without regions do nothing, and unregistering forgets only its own", () => {
    const { unmount } = render(<LiveRegions />);
    unmount();
    expect(() => {
      announceStatus("lost");
      appendToAnswerLog("lost");
    }).not.toThrow();
    const log = document.createElement("div");
    const status = document.createElement("div");
    const unregisterFirst = registerLiveRegions(log, status);
    const otherLog = document.createElement("div");
    const otherStatus = document.createElement("div");
    const unregisterSecond = registerLiveRegions(otherLog, otherStatus);
    unregisterFirst();
    appendToAnswerLog("kept");
    expect(otherLog.textContent).toBe("kept");
    unregisterSecond();
  });
});
