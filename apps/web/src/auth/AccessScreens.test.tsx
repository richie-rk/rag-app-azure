import { FluentProvider, webLightTheme } from "@fluentui/react-components";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactElement } from "react";
import { describe, expect, test, vi } from "vitest";

import { clientError } from "../api/problem";
import { renderWithProviders } from "../test/render";
import {
  AdminOnlyScreen,
  ConfigErrorScreen,
  ConsentMissingScreen,
  LoadUserFailedScreen,
  NoProjectsState,
  NotAssignedScreen,
  RedirectingScreen,
  SignInFailedScreen,
  SignedOutScreen,
  StartingScreen,
  UnsupportedBrowserScreen,
} from "./AccessScreens";

function show(ui: ReactElement) {
  return render(<FluentProvider theme={webLightTheme}>{ui}</FluentProvider>);
}

describe("the access screens", () => {
  test("Starting and Redirecting show their text under the product mark", () => {
    show(<StartingScreen />);
    expect(screen.getByText("Starting DocChat…")).toBeInTheDocument();
    expect(screen.getByText("DocChat")).toBeInTheDocument();
    expect(document.title).toBe("DocChat");
    show(<RedirectingScreen />);
    expect(screen.getByText("Taking you to sign-in…")).toBeInTheDocument();
  });

  test("each full-page state has its h1 and the document title <title> · DocChat", () => {
    show(<UnsupportedBrowserScreen />);
    expect(
      screen.getByRole("heading", { level: 1, name: "This browser can't run DocChat" }),
    ).toBeInTheDocument();
    expect(screen.getByText(/Open DocChat in a current version/)).toBeInTheDocument();
    expect(document.title).toBe("This browser can't run DocChat · DocChat");
    expect(screen.queryByRole("button")).toBeNull();
  });

  test("T-341 Configuration error offers Reload", async () => {
    const onReload = vi.fn();
    show(<ConfigErrorScreen onReload={onReload} />);
    expect(screen.getByText(/Its settings didn't load/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Reload" }));
    expect(onReload).toHaveBeenCalled();
  });

  test("Signed out offers Sign in", async () => {
    const onSignIn = vi.fn();
    show(<SignedOutScreen onSignIn={onSignIn} />);
    expect(screen.getByRole("heading", { name: "You're signed out" })).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));
    expect(onSignIn).toHaveBeenCalled();
  });

  test("Not assigned names the account and offers Retry, then Sign out", async () => {
    const onRetry = vi.fn();
    const onSignOut = vi.fn();
    show(<NotAssignedScreen account="ada@example.test" onRetry={onRetry} onSignOut={onSignOut} />);
    expect(
      screen.getByText(
        "Your account, ada@example.test, isn't set up to use DocChat. Ask your DocChat administrator to give you access, then select Retry.",
      ),
    ).toBeInTheDocument();
    const buttons = screen.getAllByRole("button");
    expect(buttons.map((button) => button.textContent)).toEqual(["Retry", "Sign out"]);
    await userEvent.click(screen.getByRole("button", { name: "Retry" }));
    await userEvent.click(screen.getByRole("button", { name: "Sign out" }));
    expect(onRetry).toHaveBeenCalled();
    expect(onSignOut).toHaveBeenCalled();
  });

  test("T-350 Consent missing shows the reference ID and Sign out", async () => {
    const onSignOut = vi.fn();
    show(<ConsentMissingScreen referenceId="corr-1" onSignOut={onSignOut} />);
    expect(screen.getByText("Reference ID: corr-1")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Sign out" }));
    expect(onSignOut).toHaveBeenCalled();
  });

  test("T-350 Sign-in failed shows the reference ID when there is one", () => {
    const { unmount } = show(<SignInFailedScreen referenceId="corr-2" onRetry={vi.fn()} />);
    expect(screen.getByText("Reference ID: corr-2")).toBeInTheDocument();
    unmount();
    show(<SignInFailedScreen referenceId={null} onRetry={vi.fn()} />);
    expect(screen.queryByText(/Reference ID/)).toBeNull();
  });

  test("T-351 Couldn't load your account shows the failed call's error bar with its trace ID", async () => {
    const onRetry = vi.fn();
    const onSignOut = vi.fn();
    const error = clientError("http", {
      operationId: "getCurrentUser",
      status: 403,
      traceId: "b".repeat(32),
    });
    show(<LoadUserFailedScreen error={error} onRetry={onRetry} onSignOut={onSignOut} />);
    expect(
      screen.getByRole("heading", { name: "We couldn't load your account" }),
    ).toBeInTheDocument();
    expect(screen.getByText("Something went wrong")).toBeInTheDocument();
    expect(screen.getByText(`Reference ID: ${"b".repeat(32)}`)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Retry" }));
    await userEvent.click(screen.getByRole("button", { name: "Sign out" }));
    expect(onRetry).toHaveBeenCalled();
    expect(onSignOut).toHaveBeenCalled();
  });

  test("T-353 a user with no projects sees No projects with Refresh", async () => {
    const onRefresh = vi.fn();
    const { user } = renderWithProviders(<NoProjectsState isAdmin={false} onRefresh={onRefresh} />);
    expect(
      screen.getByRole("heading", { name: "You aren't in any projects yet" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("region", { name: "You aren't in any projects yet" }),
    ).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Refresh" }));
    expect(onRefresh).toHaveBeenCalled();
  });

  test("T-353 an admin with no projects sees the variant with Go to Projects", async () => {
    const onRefresh = vi.fn();
    const { user, router } = renderWithProviders(
      <NoProjectsState isAdmin onRefresh={onRefresh} />,
      { route: "/chat" },
    );
    expect(screen.getByRole("heading", { name: "There are no projects yet" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Refresh" })).toBeNull();
    await user.click(screen.getByRole("button", { name: "Go to Projects" }));
    expect(router.state.location.pathname).toBe("/admin/projects");
    expect(onRefresh).not.toHaveBeenCalled();
  });

  test("T-352 Admin only offers Go to chat", async () => {
    const { user, router } = renderWithProviders(<AdminOnlyScreen />, { route: "/admin/projects" });
    expect(
      screen.getByRole("heading", { level: 1, name: "This page is for administrators" }),
    ).toBeInTheDocument();
    expect(document.title).toBe("This page is for administrators · DocChat");
    await user.click(screen.getByRole("button", { name: "Go to chat" }));
    expect(router.state.location.pathname).toBe("/chat");
  });
});
