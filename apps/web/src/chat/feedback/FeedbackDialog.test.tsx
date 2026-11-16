import { FluentProvider, webLightTheme } from "@fluentui/react-components";
import { render, screen, within } from "@testing-library/react";
import { describe, expect, test, vi } from "vitest";

import { FeedbackDialog, feedbackReasonText, firstCodePoints } from "./FeedbackDialog";

describe("the feedback dialog", () => {
  test("up mode holds the comment only, prefilled from saved up feedback", () => {
    render(
      <FluentProvider theme={webLightTheme}>
        <FeedbackDialog
          open
          mode="up"
          saved={{
            rating: "up",
            category: null,
            comment: "Kept",
            updatedAt: "2026-11-05T10:00:00Z",
          }}
          commentsEnabled
          onSubmit={vi.fn()}
          onClose={vi.fn()}
          onCommentsDisabled={vi.fn()}
        />
      </FluentProvider>,
    );
    const dialog = screen.getByRole("dialog", { name: "What made this answer helpful?" });
    expect(within(dialog).queryByRole("radiogroup")).toBeNull();
    expect(within(dialog).getByRole("textbox", { name: "Comment (optional)" })).toHaveValue("Kept");
    expect(
      within(dialog).getByText("Don't include passwords or other private details."),
    ).toBeInTheDocument();
  });

  test("down mode doesn't take saved up feedback as its start", () => {
    render(
      <FluentProvider theme={webLightTheme}>
        <FeedbackDialog
          open
          mode="down"
          saved={{
            rating: "up",
            category: null,
            comment: "Kept",
            updatedAt: "2026-11-05T10:00:00Z",
          }}
          commentsEnabled
          onSubmit={vi.fn()}
          onClose={vi.fn()}
          onCommentsDisabled={vi.fn()}
        />
      </FluentProvider>,
    );
    expect(screen.getByRole("textbox", { name: "Comment (optional)" })).toHaveValue("");
    expect(screen.getAllByRole("radio").map((radio) => radio.getAttribute("value"))).toEqual([
      "incorrect",
      "incomplete",
      "not_relevant",
      "other",
    ]);
  });

  test("reason texts and the code-point limit", () => {
    expect(feedbackReasonText("too_long")).toBe(
      "Comments can be up to 2,000 characters. Shorten yours, then send it.",
    );
    expect(feedbackReasonText("required")).toBe("This value isn't valid. Check it and correct it.");
    expect(firstCodePoints("ab😀c", 3)).toBe("ab😀");
    expect(firstCodePoints("abc", 3)).toBe("abc");
  });
});
