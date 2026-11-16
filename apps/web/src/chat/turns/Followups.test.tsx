import { FluentProvider, webLightTheme } from "@fluentui/react-components";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, test, vi } from "vitest";

import { Followups, followupText } from "./Followups";

const ITEMS = [
  "Is https://evil.example/p still valid?",
  "What about //evil.example/p here?",
  "See www.evil.example now",
  "Open https:​//evil.example/p please",
  "What does \\\\host\\share hold?",
  "See example.net/abc",
  "Ask name@example.net",
];

describe("T-420 follow-up suggestions", () => {
  test("T-420 show and send their text with every URL redacted, even one the turn allowed", async () => {
    const user = userEvent.setup();
    const onSend = vi.fn();
    render(
      <FluentProvider theme={webLightTheme}>
        <Followups items={ITEMS} disabled={false} onSend={onSend} />
      </FluentProvider>,
    );
    const group = screen.getByRole("group", { name: "Suggested questions" });
    const buttons = [...group.querySelectorAll("button")];
    expect(buttons.map((button) => button.textContent)).toEqual([
      "Is [link removed] still valid?",
      "What about [link removed] here?",
      "See [link removed] now",
      "Open [link removed] please",
      "What does \\\\host\\share hold?",
      "See example.net/abc",
      "Ask name@example.net",
    ]);
    for (const button of buttons) await user.click(button);
    expect(onSend.mock.calls.map((call) => call[0] as string)).toEqual(
      buttons.map((button) => button.textContent),
    );
    expect(followupText("https://evil.example/p")).toBe("[link removed]");
  });

  test("T-420 are disabled while sending is blocked, and absent without items", () => {
    const { rerender } = render(
      <FluentProvider theme={webLightTheme}>
        <Followups items={["Next?"]} disabled onSend={vi.fn()} />
      </FluentProvider>,
    );
    expect(screen.getByRole("button", { name: "Next?" })).toBeDisabled();
    rerender(
      <FluentProvider theme={webLightTheme}>
        <Followups items={[]} disabled={false} onSend={vi.fn()} />
      </FluentProvider>,
    );
    expect(screen.queryByRole("group")).toBeNull();
  });
});
