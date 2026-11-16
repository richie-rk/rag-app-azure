import { FluentProvider, webLightTheme } from "@fluentui/react-components";
import { act, fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, test, vi } from "vitest";

import { Composer, type ComposerProps } from "./Composer";
import { resetDraftsForTests, setDraft, setDraftReason } from "./drafts";

afterEach(() => {
  act(() => {
    resetDraftsForTests();
  });
});

function show(overrides: Partial<ComposerProps> = {}) {
  const props: ComposerProps = {
    draftKey: "new:p",
    disabled: false,
    blocked: false,
    blockedUntil: null,
    stoppable: false,
    onSend: vi.fn(),
    onStop: vi.fn(),
    ...overrides,
  };
  render(
    <FluentProvider theme={webLightTheme}>
      <Composer {...props} />
    </FluentProvider>,
  );
  return { props, box: screen.getByRole("textbox", { name: "Your question" }) };
}

describe("the composer", () => {
  test("Enter sends the trimmed question and focus stays in the text area", async () => {
    const user = userEvent.setup();
    const { props, box } = show();
    expect(screen.getByRole("button", { name: "Send question" })).toBeDisabled();
    await user.type(box, "  How long?  {Enter}");
    expect(props.onSend).toHaveBeenCalledWith("How long?");
    await user.click(screen.getByRole("button", { name: "Send question" }));
    expect(props.onSend).toHaveBeenCalledTimes(2);
    expect(box).toHaveFocus();
    expect(
      screen.getByText("Press Enter to send. Press Shift+Enter for a new line."),
    ).toBeInTheDocument();
  });

  test("Shift+Enter, Alt+Enter and Ctrl+Enter insert a line break", async () => {
    const user = userEvent.setup();
    const { props, box } = show();
    await user.type(box, "a{Shift>}{Enter}{/Shift}b");
    expect(box).toHaveValue("a\nb");
    fireEvent.keyDown(box, { key: "Enter", altKey: true });
    fireEvent.keyDown(box, { key: "Enter", ctrlKey: true });
    expect((box as HTMLTextAreaElement).value.split("\n")).toHaveLength(4);
    expect(props.onSend).not.toHaveBeenCalled();
  });

  test("Enter does nothing while an input method composes or sending is blocked", async () => {
    const user = userEvent.setup();
    const { props, box } = show();
    await user.type(box, "Question");
    fireEvent.keyDown(box, { key: "Enter", isComposing: true });
    fireEvent.keyDown(box, { key: "Enter", keyCode: 229 });
    expect(props.onSend).not.toHaveBeenCalled();
  });

  test("blocked sending keeps Send disabled and Enter inert, and shows the wait", () => {
    act(() => {
      setDraft("new:p", "Question");
    });
    const { props, box } = show({ blocked: true, blockedUntil: Date.now() + 10_000 });
    fireEvent.keyDown(box, { key: "Enter" });
    expect(props.onSend).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "Send question" })).toBeDisabled();
    expect(screen.getByText(/You can send your next question at/)).toBeInTheDocument();
    expect(box).toBeEnabled();
  });

  test("a disabled composer disables the text area", () => {
    const { box } = show({ disabled: true });
    expect(box).toBeDisabled();
  });

  test("the counter shows from 3,600 code points, and above 4,000 sending is blocked", () => {
    act(() => {
      setDraft("new:p", "😀".repeat(3_600));
    });
    show();
    expect(screen.getByText(/3,600 of 4,000 characters/)).toBeInTheDocument();
    act(() => {
      setDraft("new:p", "a".repeat(4_001));
    });
    expect(screen.getByText(/Questions can be up to 4,000 characters/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Send question" })).toBeDisabled();
  });

  test("T-372 a reason from a 422 shows under the field until the text changes", async () => {
    const user = userEvent.setup();
    act(() => {
      setDraft("new:p", "?");
      setDraftReason("new:p", "Type a question with at least one letter, number or symbol.");
    });
    const { box } = show();
    expect(box).toHaveValue("?");
    expect(
      screen.getByText("Type a question with at least one letter, number or symbol."),
    ).toBeInTheDocument();
    await user.type(box, "!");
    expect(
      screen.queryByText("Type a question with at least one letter, number or symbol."),
    ).toBeNull();
  });

  test("Stop takes Send's place, and Escape stops too", async () => {
    const user = userEvent.setup();
    const { props, box } = show({ stoppable: true });
    expect(screen.queryByRole("button", { name: "Send question" })).toBeNull();
    await user.click(screen.getByRole("button", { name: "Stop answer" }));
    fireEvent.keyDown(box, { key: "Escape" });
    expect(props.onStop).toHaveBeenCalledTimes(2);
  });

  test("Escape without a running answer does nothing, and the height is reported", () => {
    const onHeight = vi.fn();
    const { props, box } = show({ onHeight });
    fireEvent.keyDown(box, { key: "Escape" });
    fireEvent.keyDown(box, { key: "a" });
    expect(props.onStop).not.toHaveBeenCalled();
    expect(onHeight).toHaveBeenCalled();
  });
});
