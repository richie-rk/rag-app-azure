import { render, waitFor } from "@testing-library/react";
import { describe, expect, test, vi } from "vitest";

import { FocusFirstControl } from "./FocusFirstControl";

function Surface() {
  return (
    <div role="dialog" aria-label="Confirm" tabIndex={-1}>
      <input aria-label="dummy" data-tabster-dummy="" />
      <button type="button" disabled>
        Busy
      </button>
      <button type="button">Delete</button>
      <FocusFirstControl />
    </div>
  );
}

describe("focus on a dialog's first control", () => {
  test("moves focus to the first enabled control when the dialog opened without it", () => {
    const { getByRole } = render(<Surface />);
    expect(getByRole("button", { name: "Delete" })).toHaveFocus();
  });

  test("an open dialog marked hidden focuses its first control again, at most three times", async () => {
    const { getByRole } = render(<Surface />);
    const surface = getByRole("dialog");
    const button = getByRole("button", { name: "Delete" });
    const focus = vi.spyOn(button, "focus");
    for (let time = 1; time <= 4; time += 1) {
      surface.setAttribute("aria-hidden", "true");
      await waitFor(() => {
        expect(focus).toHaveBeenCalledTimes(Math.min(time, 3));
      });
      surface.removeAttribute("aria-hidden");
    }
    expect(button).toHaveFocus();
  });

  test("outside a dialog it does nothing", () => {
    const { container } = render(
      <div>
        <button type="button">Elsewhere</button>
        <FocusFirstControl />
      </div>,
    );
    expect(container.querySelector("button")).not.toHaveFocus();
  });
});
