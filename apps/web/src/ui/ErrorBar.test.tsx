import { Button, FluentProvider, webLightTheme } from "@fluentui/react-components";
import { act, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement } from "react";
import { describe, expect, test, vi } from "vitest";

import { LiveRegions } from "../a11y/LiveRegions";
import { clientError, type ApiError } from "../api/problem";
import { readRepositoryText } from "../test/files";
import { ApiErrorBar, ErrorBar, errorText } from "./ErrorBar";
import { formatRetryTime } from "./format";

function show(ui: ReactElement) {
  return render(
    <FluentProvider theme={webLightTheme}>
      {ui}
      <LiveRegions />
    </FluentProvider>,
  );
}

interface CatalogueEntry {
  readonly code: string;
  readonly status: number;
  readonly title: string;
  readonly detail: string;
}

/** The problem codes of `11-api.md` section 2.2: the fixture of the catalogue. */
function catalogue(): CatalogueEntry[] {
  const text = readRepositoryText("docs/spec/11-api.md");
  const start = text.indexOf("### 2.2 Problem codes");
  const end = text.indexOf("### 2.3", start);
  return text
    .slice(start, end)
    .split("\n")
    .filter((line) => line.startsWith("| `"))
    .map((line) => line.split(" | "))
    .map((cells) => ({
      code: (cells[0] ?? "").replace(/^\| `|`$/g, ""),
      status: Number(cells[1]),
      title: cells[2] ?? "",
      detail: cells[3] ?? "",
    }))
    .filter((entry) => Number.isInteger(entry.status));
}

function problemError(entry: CatalogueEntry): ApiError {
  return {
    ...clientError("problem", {
      operationId: "getCurrentUser",
      status: entry.status,
      code: entry.code,
    }),
    title: entry.title,
    detail: entry.detail,
    referenceId: "0123456789abcdef0123456789abcdef",
  };
}

describe("the error bar", () => {
  test("T-360 every catalogue code shows its title and detail word for word with the reference ID", () => {
    const entries = catalogue();
    expect(entries.length).toBeGreaterThan(40);
    for (const entry of entries) {
      const { unmount, container } = show(<ApiErrorBar error={problemError(entry)} />);
      const bar = within(container);
      expect(bar.getByText(entry.title), entry.code).toBeInTheDocument();
      expect(bar.getByText(entry.detail), entry.code).toBeInTheDocument();
      expect(bar.getByText("Reference ID: 0123456789abcdef0123456789abcdef")).toBeInTheDocument();
      unmount();
    }
  });

  test.each([
    ["http", "Something went wrong", /DocChat had a problem on its side/],
    ["network", "Can't reach DocChat", /Check your connection, then select Retry/],
    ["unexpected", "Unexpected reply", /DocChat sent a reply this page couldn't read/],
  ] as const)(
    "T-360 a %s failure shows its error text with the reference ID",
    (kind, title, body) => {
      show(<ApiErrorBar error={clientError(kind, { traceId: "f".repeat(32) })} />);
      expect(screen.getByText(title)).toBeInTheDocument();
      expect(screen.getByText(body)).toBeInTheDocument();
      expect(screen.getByText(`Reference ID: ${"f".repeat(32)}`)).toBeInTheDocument();
    },
  );

  test("session_expired shows error.sessionExpired, and aborted shows nothing", () => {
    expect(errorText(clientError("session_expired"))).toEqual({
      title: "Your sign-in has expired",
      body: "You need to sign in before this can finish.",
    });
    const { container } = show(<ApiErrorBar error={clientError("aborted")} />);
    expect(container.querySelector("[role='log']")?.parentElement?.textContent).toBe("");
    expect(errorText({ ...clientError("problem"), title: null, detail: null })).toEqual({
      title: "Something went wrong",
      body: "",
    });
  });

  test("T-364 a problem with retryAfterSeconds adds error.retryAt, and Retry can wait for it", async () => {
    const onRetry = vi.fn();
    const error: ApiError = {
      ...problemError({
        code: "write_rate_limited",
        status: 429,
        title: "Too many changes",
        detail: "Wait.",
      }),
      retryAfterSeconds: 20,
      receivedAt: 1_000_000,
    };
    const { rerender } = show(
      <ApiErrorBar
        error={error}
        onRetry={onRetry}
        retryDisabled
        now={1_000_000}
        lines={["Your edits are still here."]}
      />,
    );
    expect(
      screen.getByText(`You can retry at ${formatRetryTime(1_020_000, 1_000_000)}.`),
    ).toBeInTheDocument();
    expect(screen.getByText("Your edits are still here.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Retry" })).toBeDisabled();
    rerender(
      <FluentProvider theme={webLightTheme}>
        <ApiErrorBar error={error} onRetry={onRetry} actions={<Button>New chat</Button>} />
      </FluentProvider>,
    );
    await userEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(onRetry).toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "New chat" })).toBeInTheDocument();
  });

  test("an error bar without a reference ID or actions shows neither", () => {
    show(<ErrorBar title="Couldn't copy" body={null} />);
    expect(screen.getByText("Couldn't copy")).toBeInTheDocument();
    expect(screen.queryByText(/Reference ID/)).toBeNull();
    expect(screen.queryByRole("button")).toBeNull();
  });

  test("Copy reference ID copies it and announces it", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const writeText = vi.fn(() => Promise.resolve());
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText } });
    show(<ErrorBar title="Failed" referenceId="ref-1" />);
    await userEvent.click(screen.getByRole("button", { name: "Copy reference ID" }));
    expect(writeText).toHaveBeenCalledWith("ref-1");
    await act(async () => {
      await vi.advanceTimersByTimeAsync(60);
    });
    expect(screen.getByRole("status")).toHaveTextContent("Reference ID copied");
    writeText.mockImplementationOnce(() => Promise.reject(new Error("denied")));
    await userEvent.click(screen.getByRole("button", { name: "Copy reference ID" }));
    vi.useRealTimers();
  });
});
