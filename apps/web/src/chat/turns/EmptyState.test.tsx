import { FluentProvider, webLightTheme } from "@fluentui/react-components";
import { fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, test, vi } from "vitest";

import type { ProjectSummary } from "../../api/queryClient";
import { IDS } from "../../test/handlers";
import { EmptyState } from "./EmptyState";

const project: ProjectSummary = {
  projectId: IDS.projectA,
  name: "Alpha",
  description: "Policies of the alpha team.",
  exampleQuestions: ["What is alpha?", "Who owns it?"],
  hasDefaultIndex: true,
};

function show(value: ProjectSummary, disabled = false) {
  const onSend = vi.fn();
  render(
    <FluentProvider theme={webLightTheme}>
      <EmptyState project={value} disabled={disabled} onSend={onSend} />
    </FluentProvider>,
  );
  return onSend;
}

describe("the empty state", () => {
  test("shows the heading, the description and a card per example, which sends it", async () => {
    const user = userEvent.setup();
    const onSend = show(project);
    expect(
      screen.getByRole("heading", { level: 2, name: "What would you like to know?" }),
    ).toBeInTheDocument();
    expect(screen.getByText("Policies of the alpha team.")).toBeInTheDocument();
    expect(screen.getByText("Try one of these")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "What is alpha?" }));
    fireEvent.keyDown(screen.getByRole("button", { name: "Who owns it?" }), { key: "Enter" });
    fireEvent.keyDown(screen.getByRole("button", { name: "Who owns it?" }), { key: "a" });
    expect(onSend.mock.calls).toEqual([["What is alpha?"], ["Who owns it?"]]);
  });

  test("uses the default body without a description, and no examples label without examples", () => {
    show({ ...project, description: "", exampleQuestions: [] });
    expect(
      screen.getByText(
        "Ask a question and DocChat answers from this project's documents, with numbered sources you can open.",
      ),
    ).toBeInTheDocument();
    expect(screen.queryByText("Try one of these")).toBeNull();
  });

  test("cards do nothing while sending is blocked", () => {
    const onSend = show(project, true);
    fireEvent.keyDown(screen.getByRole("button", { name: "What is alpha?" }), { key: " " });
    expect(onSend).not.toHaveBeenCalled();
  });
});
