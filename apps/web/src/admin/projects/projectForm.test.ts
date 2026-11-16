import { describe, expect, test } from "vitest";

import { clientError, type ApiError } from "../../api/problem";
import { adminProject } from "../../test/adminData";
import {
  EMPTY_PROJECT_FORM,
  checkProjectForm,
  createProjectBody,
  normalizedProject,
  projectFieldOrder,
  projectFormValues,
  projectReasonText,
  projectSchemaFields,
  routeProjectErrors,
  sentExampleRows,
  updateProjectBody,
} from "./projectForm";

function unprocessable(errors: { path: string; reason: string }[]): ApiError {
  return { ...clientError("problem", { status: 422 }), errors };
}

describe("the project form", () => {
  test("loads a project's values", () => {
    expect(projectFormValues(adminProject())).toEqual({
      name: "Field notes",
      description: "Shared notes for the field team",
      exampleQuestions: ["What changed in the policy?"],
      instructions: "Explain acronyms.",
      isDefault: false,
    });
  });

  test("blank example rows aren't sent, and sent indices map back to rows", () => {
    const values = { ...EMPTY_PROJECT_FORM, exampleQuestions: ["a", " ", "b"] };
    expect(sentExampleRows(values.exampleQuestions)).toEqual([0, 2]);
    expect(normalizedProject(values).exampleQuestions).toEqual(["a", "b"]);
    expect(projectFieldOrder(values)).toEqual([
      "name",
      "description",
      "exampleQuestions.0",
      "exampleQuestions.1",
      "exampleQuestions.2",
      "instructions",
      "isDefault",
    ]);
  });

  test("Create sends every field and the chosen people in order", () => {
    expect(
      createProjectBody({ ...EMPTY_PROJECT_FORM, name: "N", exampleQuestions: ["", "q"] }, [
        "u2",
        "u1",
      ]),
    ).toEqual({
      name: "N",
      description: "",
      instructions: "",
      exampleQuestions: ["q"],
      isDefault: false,
      initialMemberIds: ["u2", "u1"],
    });
  });

  test("Save sends only the changed fields", () => {
    const base = projectFormValues(adminProject());
    expect(updateProjectBody(base, base)).toEqual({});
    expect(
      updateProjectBody(base, { ...base, exampleQuestions: [...base.exampleQuestions, ""] }),
    ).toEqual({});
    expect(
      updateProjectBody(base, {
        ...base,
        name: "New",
        description: "D",
        instructions: "I",
        exampleQuestions: [],
        isDefault: true,
      }),
    ).toEqual({
      name: "New",
      description: "D",
      instructions: "I",
      exampleQuestions: [],
      isDefault: true,
    });
  });

  test("the checks: Name required, every text within its code-point limit", () => {
    expect([...checkProjectForm(EMPTY_PROJECT_FORM)]).toEqual([["name", "Enter a name."]]);
    const messages = checkProjectForm({
      name: "x".repeat(81),
      description: "x".repeat(401),
      exampleQuestions: ["ok", "x".repeat(161)],
      instructions: "x".repeat(2001),
      isDefault: false,
    });
    expect([...messages]).toEqual([
      ["name", "This can be up to 80 characters."],
      ["description", "This can be up to 400 characters."],
      ["exampleQuestions.1", "This can be up to 160 characters."],
      ["instructions", "This can be up to 2000 characters."],
    ]);
    expect(checkProjectForm({ ...EMPTY_PROJECT_FORM, name: "😀".repeat(80) }).size).toBe(0);
  });

  test("reason texts: required and too_long have the screen's text", () => {
    expect(projectReasonText("name", "required")).toBe("Enter a name.");
    expect(projectReasonText("name", "too_short")).toBe("Enter a name.");
    expect(projectReasonText("exampleQuestions.3", "too_long")).toBe(
      "This can be up to 160 characters.",
    );
    expect(projectReasonText("instructions", "too_long")).toBe(
      "This can be up to 2000 characters.",
    );
    expect(projectReasonText("description", "too_long")).toBe("This can be up to 400 characters.");
    expect(projectReasonText("isDefault", "too_long")).toBeNull();
    expect(projectReasonText("exampleQuestions.0", "duplicate")).toBeNull();
  });

  test("T-361 a 422 at body.exampleQuestions.2 marks the row it came from", () => {
    const values = { ...EMPTY_PROJECT_FORM, name: "N", exampleQuestions: ["a", "", "b", "c"] };
    const routed = routeProjectErrors(
      unprocessable([
        { path: "body.exampleQuestions.2", reason: "duplicate" },
        { path: "body.initialMemberIds.0", reason: "not_found" },
        { path: "path.projectId", reason: "invalid_uuid" },
      ]),
      values,
      ["initialMemberIds.0"],
    );
    expect([...routed.messages]).toEqual([
      ["exampleQuestions.3", "This value isn't valid. Check it and correct it."],
      ["initialMemberIds.0", "This value isn't valid. Check it and correct it."],
    ]);
    expect(routed.unmatched.map((entry) => entry.path)).toEqual(["path.projectId"]);
  });

  test("the generated schema's failures land on form fields", () => {
    const values = { ...EMPTY_PROJECT_FORM, name: "N", exampleQuestions: ["", "q"] };
    const body = { ...createProjectBody(values, []), isDefault: "no" as unknown as boolean };
    expect([...projectSchemaFields(body, values, true).keys()]).toEqual(["isDefault"]);
    expect(projectSchemaFields({ name: "" }, values, false).size).toBe(1);
  });
});
