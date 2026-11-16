import { FluentProvider, webLightTheme } from "@fluentui/react-components";
import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, test } from "vitest";

import { TEST_ACCOUNT, fakeMsal, type FakeMsal } from "../test/fakeMsal";
import { TEST_CONFIG } from "../test/handlers";
import { SessionExpiredBar } from "./SessionExpiredBar";
import { expireSession, initTokens } from "./tokens";

let instance: FakeMsal;

beforeEach(() => {
  const fake = fakeMsal();
  instance = fake.instance;
  initTokens({ msal: fake.msal, account: TEST_ACCOUNT, scope: TEST_CONFIG.apiScope });
});

describe("the Session expired bar", () => {
  test("T-354 appears only once the session has expired, and Sign in redirects back here", async () => {
    render(
      <FluentProvider theme={webLightTheme}>
        <SessionExpiredBar />
      </FluentProvider>,
    );
    expect(screen.queryByText("Your sign-in has expired")).toBeNull();
    act(() => {
      expireSession();
    });
    expect(screen.getByText("Your sign-in has expired")).toBeInTheDocument();
    expect(screen.getByText(/Copy any text you haven't sent first/)).toBeInTheDocument();
    instance.acquireTokenRedirect.mockRejectedValueOnce(new Error("blocked"));
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));
    expect(instance.acquireTokenRedirect).toHaveBeenCalledWith(
      expect.objectContaining({ scopes: [TEST_CONFIG.apiScope], account: TEST_ACCOUNT }),
    );
  });
});
