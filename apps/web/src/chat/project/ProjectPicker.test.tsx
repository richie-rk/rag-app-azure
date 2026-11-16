import { FluentProvider, webLightTheme } from "@fluentui/react-components";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, test, vi } from "vitest";

import type { ProjectSummary } from "../../api/queryClient";
import { IDS } from "../../test/handlers";
import { ProjectPicker, projectLabel, type ProjectPickerProps } from "./ProjectPicker";

const alpha: ProjectSummary = {
  projectId: IDS.projectA,
  name: "Alpha",
  description: "",
  exampleQuestions: [],
  hasDefaultIndex: true,
};
const beta: ProjectSummary = {
  ...alpha,
  projectId: IDS.projectB,
  name: "Beta",
  hasDefaultIndex: false,
};

function show(overrides: Partial<ProjectPickerProps>) {
  const props: ProjectPickerProps = {
    projects: [beta, alpha],
    selected: null,
    inSession: false,
    failed: false,
    onSelect: vi.fn(),
    ...overrides,
  };
  render(
    <FluentProvider theme={webLightTheme}>
      <ProjectPicker {...props} />
    </FluentProvider>,
  );
  return props;
}

describe("the project picker", () => {
  test("lists projects by name, marks one that isn't ready, and reports a choice", async () => {
    const user = userEvent.setup();
    const props = show({});
    expect(screen.getByText("Choose a project to start asking questions.")).toBeInTheDocument();
    await user.click(screen.getByRole("combobox", { name: "Project" }));
    const options = screen.getAllByRole("option").map((option) => option.textContent);
    expect(options).toEqual(["Alpha", "Beta (no documents yet)"]);
    await user.click(screen.getByRole("option", { name: "Beta (no documents yet)" }));
    expect(props.onSelect).toHaveBeenCalledWith(IDS.projectB);
  });

  test("in a session it shows the change hint, and choosing the same project does nothing", async () => {
    const user = userEvent.setup();
    const props = show({ selected: alpha, inSession: true });
    expect(screen.getByText("Choosing a different project starts a new chat.")).toBeInTheDocument();
    expect(screen.getByRole("combobox", { name: "Project" })).toHaveTextContent("Alpha");
    await user.click(screen.getByRole("combobox", { name: "Project" }));
    await user.click(screen.getByRole("option", { name: "Alpha" }));
    expect(props.onSelect).not.toHaveBeenCalled();
  });

  test("while projects load the picker is disabled with a spinner; after a failure without one", () => {
    show({ projects: null });
    expect(screen.getByRole("combobox", { name: "Project" })).toBeDisabled();
    expect(screen.getByRole("progressbar")).toBeInTheDocument();
  });

  test("a failed load leaves the picker disabled without a spinner", () => {
    show({ projects: null, failed: true });
    expect(screen.queryByRole("progressbar")).toBeNull();
  });

  test("labels", () => {
    expect(projectLabel(alpha)).toBe("Alpha");
    expect(projectLabel(beta)).toBe("Beta (no documents yet)");
  });
});
