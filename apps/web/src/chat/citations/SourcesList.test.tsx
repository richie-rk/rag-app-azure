import { FluentProvider, webLightTheme } from "@fluentui/react-components";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, test, vi } from "vitest";

import { CITATION, present } from "../../test/chat";
import { SourcesList } from "./SourcesList";

describe("the Sources list", () => {
  test("shows one button per citation reading title and page, each opening the viewer", async () => {
    const user = userEvent.setup();
    const onOpen = vi.fn();
    const docx = {
      ...CITATION,
      number: 2,
      title: "Finance handbook",
      pageNumber: 12,
      contentType: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    } as const;
    render(
      <FluentProvider theme={webLightTheme}>
        <SourcesList citations={[CITATION, docx]} onOpen={onOpen} />
      </FluentProvider>,
    );
    const section = screen.getByRole("region", { name: "Sources" });
    const buttons = within(section).getAllByRole("button");
    expect(buttons.map((button) => button.textContent)).toEqual([
      "Expenses guide, page 3",
      "Finance handbook, page 12",
    ]);
    await user.click(present(buttons[1]));
    expect(onOpen).toHaveBeenCalledWith(docx, buttons[1]);
  });

  test("renders nothing without citations", () => {
    const { container } = render(<SourcesList citations={[]} onOpen={vi.fn()} />);
    expect(container).toBeEmptyDOMElement();
  });
});
