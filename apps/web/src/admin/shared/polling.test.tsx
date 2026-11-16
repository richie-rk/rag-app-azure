import { useQuery } from "@tanstack/react-query";
import { act, screen, waitFor } from "@testing-library/react";
import { describe, expect, test, vi } from "vitest";

import { renderWithProviders } from "../../test/render";
import { formatTime } from "../../ui/format";
import { AutoRefreshSwitch, PollStatusBar, usePollWatch, usePolling } from "./polling";

/** A polled query whose fetch answers from `answers` in turn: true succeeds, false fails. */
function Polled({ answers, active }: { answers: boolean[]; active: boolean }) {
  const polling = usePolling(5_000);
  const query = useQuery({
    queryKey: ["polled"],
    queryFn: () => {
      const ok = answers.shift() ?? true;
      return ok ? Promise.resolve({ at: Date.now() }) : Promise.reject(new Error("down"));
    },
    refetchInterval: () => polling.interval(active),
    refetchIntervalInBackground: false,
  });
  usePollWatch(polling, query);
  return (
    <>
      <AutoRefreshSwitch polling={polling} />
      <PollStatusBar polling={polling} result={query} />
      <output data-testid="state">{`${String(polling.failures)}:${String(polling.stopped)}`}</output>
    </>
  );
}

describe("polling (section 7.11)", () => {
  test("T-402 polls while the condition holds; Auto-refresh off stops it", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      const answers = [true, true, true, true, true];
      const { user } = renderWithProviders(<Polled answers={answers} active />);
      await waitFor(() => {
        expect(answers).toHaveLength(4);
      });
      await act(() => vi.advanceTimersByTimeAsync(5_000));
      expect(answers).toHaveLength(3);
      await user.click(screen.getByRole("switch", { name: "Auto-refresh" }));
      await act(() => vi.advanceTimersByTimeAsync(15_000));
      expect(answers).toHaveLength(3);
    } finally {
      vi.useRealTimers();
    }
  });

  test("T-402 a failed poll shows stale; 3 in a row stop polling and Refresh restarts it", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      const answers = [true, false, false, false, true, true];
      const { user } = renderWithProviders(<Polled answers={answers} active />);
      await waitFor(() => {
        expect(screen.getByTestId("state")).toHaveTextContent("0:false");
      });
      const firstSuccess = Date.now();
      await act(() => vi.advanceTimersByTimeAsync(5_000));
      expect(
        await screen.findByText(/Couldn't refresh\. Showing results from/),
      ).toBeInTheDocument();
      expect(screen.getByText(new RegExp(formatTime(firstSuccess)))).toBeInTheDocument();
      await act(() => vi.advanceTimersByTimeAsync(5_000));
      await act(() => vi.advanceTimersByTimeAsync(5_000));
      await waitFor(() => {
        expect(screen.getByTestId("state")).toHaveTextContent("3:true");
      });
      expect(
        screen.getByText("Automatic refresh stopped because DocChat couldn't be reached."),
      ).toBeInTheDocument();
      await act(() => vi.advanceTimersByTimeAsync(20_000));
      expect(answers).toHaveLength(2);
      await user.click(screen.getByRole("button", { name: "Refresh" }));
      await waitFor(() => {
        expect(screen.getByTestId("state")).toHaveTextContent("0:false");
      });
      expect(screen.queryByText(/Automatic refresh stopped/)).toBeNull();
      expect(answers).toHaveLength(1);
      await act(() => vi.advanceTimersByTimeAsync(5_000));
      expect(answers).toHaveLength(0);
    } finally {
      vi.useRealTimers();
    }
  });

  test("nothing polls while the condition doesn't hold", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      const answers = [true, true, true];
      renderWithProviders(<Polled answers={answers} active={false} />);
      await waitFor(() => {
        expect(answers).toHaveLength(2);
      });
      await act(() => vi.advanceTimersByTimeAsync(20_000));
      expect(answers).toHaveLength(2);
    } finally {
      vi.useRealTimers();
    }
  });
});
