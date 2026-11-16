import { FluentProvider, webLightTheme } from "@fluentui/react-components";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, test, vi } from "vitest";

import { clientError } from "../../api/problem";
import { ChatUnavailable } from "./ChatUnavailable";

describe("Chat not available", () => {
  test("shows the problem's title and detail with New chat", async () => {
    const user = userEvent.setup();
    const onNewChat = vi.fn();
    render(
      <FluentProvider theme={webLightTheme}>
        <ChatUnavailable
          error={{
            ...clientError("problem", { status: 404, code: "session_not_found" }),
            title: "Chat not found",
            detail: "This chat doesn't exist or you can't open it.",
          }}
          onNewChat={onNewChat}
        />
      </FluentProvider>,
    );
    expect(screen.getByRole("heading", { level: 2, name: "Chat not found" })).toBeInTheDocument();
    expect(screen.getByText("This chat doesn't exist or you can't open it.")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "New chat" }));
    expect(onNewChat).toHaveBeenCalled();
  });
});
