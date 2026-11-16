import { FluentProvider, webLightTheme } from "@fluentui/react-components";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, test, vi } from "vitest";

import { ConfirmDialog } from "./ConfirmDialog";

function show(props: Partial<Parameters<typeof ConfirmDialog>[0]> = {}) {
  const onConfirm = vi.fn();
  const onCancel = vi.fn();
  render(
    <FluentProvider theme={webLightTheme}>
      <ConfirmDialog
        open
        title="Delete Alpha?"
        body="You can't undo this."
        actionLabel="Delete"
        onConfirm={onConfirm}
        onCancel={onCancel}
        {...props}
      />
    </FluentProvider>,
  );
  return { onConfirm, onCancel };
}

describe("the confirmation dialog", () => {
  test("names the item and says what happens, with the action first and Cancel", async () => {
    const { onConfirm, onCancel } = show();
    const dialog = screen.getByRole("dialog", { name: "Delete Alpha?" });
    expect(dialog).toHaveTextContent("You can't undo this.");
    const buttons = screen.getAllByRole("button").map((button) => button.textContent);
    expect(buttons).toEqual(["Delete", "Cancel"]);
    await userEvent.click(screen.getByRole("button", { name: "Delete" }));
    expect(onConfirm).toHaveBeenCalled();
    await userEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(onCancel).toHaveBeenCalledTimes(1);
  });

  test("Escape cancels", async () => {
    const { onCancel } = show();
    await userEvent.keyboard("{Escape}");
    expect(onCancel).toHaveBeenCalled();
  });

  test("while the action runs both buttons wait, and Escape does nothing", async () => {
    const { onCancel } = show({ busy: true, cancelLabel: "Stay signed in" });
    expect(screen.getByRole("button", { name: "Delete" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Stay signed in" })).toBeDisabled();
    await userEvent.keyboard("{Escape}");
    expect(onCancel).not.toHaveBeenCalled();
  });

  test("a closed dialog shows nothing", () => {
    show({ open: false });
    expect(screen.queryByRole("dialog")).toBeNull();
  });
});
