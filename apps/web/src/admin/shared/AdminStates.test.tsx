import { screen } from "@testing-library/react";
import { describe, expect, test, vi } from "vitest";

import { ApiFailure, clientError, type ApiError } from "../../api/problem";
import { renderWithProviders } from "../../test/render";
import { LoadErrorBar, NotFoundBar, ReloadErrorBar } from "./AdminStates";

const notFound: ApiError = {
  ...clientError("problem", { status: 404, code: "project_not_found", traceId: "t" }),
  title: "Project not found",
  detail: "This project doesn't exist, or you don't have access to it.",
  referenceId: "c0ffee",
};

describe("admin error states", () => {
  test("a failed load shows the screen's text, the failure's text, the retry time and Retry", async () => {
    const onRetry = vi.fn();
    const { user } = renderWithProviders(
      <LoadErrorBar
        title="Projects didn't load."
        error={{
          ...clientError("problem", { status: 429, traceId: "t" }),
          title: "Too many requests",
          detail: "Wait a moment.",
          retryAfterSeconds: 1,
        }}
        onRetry={onRetry}
      />,
    );
    expect(screen.getByText("Projects didn't load.")).toBeInTheDocument();
    expect(screen.getByText("Too many requests")).toBeInTheDocument();
    expect(screen.getByText("Wait a moment.")).toBeInTheDocument();
    expect(screen.getByText(/You can retry at/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Retry" }));
    expect(onRetry).toHaveBeenCalled();
  });

  test("an aborted load shows nothing", () => {
    const { result } = renderWithProviders(
      <>
        <LoadErrorBar title="x" error={clientError("aborted")} onRetry={vi.fn()} />
        <NotFoundBar error={clientError("aborted")} list="Projects" to="/admin/projects" />
      </>,
    );
    expect(result.container.querySelector(".fui-MessageBar")).toBeNull();
  });

  test("a 404 shows the problem's text and a link back to the list", () => {
    renderWithProviders(<NotFoundBar error={notFound} list="Projects" to="/admin/projects" />);
    expect(screen.getByText("Project not found")).toBeInTheDocument();
    expect(screen.getByText("Reference ID: c0ffee")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Back to Projects" })).toHaveAttribute(
      "href",
      "/admin/projects",
    );
  });

  test("a failed reload keeps the rows and shows a bar with Retry; a failed next page doesn't", async () => {
    const refetch = vi.fn(() => Promise.resolve());
    const failure = new ApiFailure(notFound);
    const { user, result } = renderWithProviders(
      <ReloadErrorBar list={{ data: {}, error: failure, isError: true, refetch }} />,
    );
    await user.click(screen.getByRole("button", { name: "Retry" }));
    expect(refetch).toHaveBeenCalled();
    result.rerender(
      <ReloadErrorBar
        list={{ data: {}, error: failure, isError: true, isFetchNextPageError: true, refetch }}
      />,
    );
    expect(screen.queryByRole("button", { name: "Retry" })).toBeNull();
    result.rerender(
      <ReloadErrorBar list={{ data: {}, error: new Error("x"), isError: true, refetch }} />,
    );
    expect(screen.queryByRole("button", { name: "Retry" })).toBeNull();
  });
});
