import { render } from "@testing-library/react";
import { describe, expect, test } from "vitest";

import { zProjectCreate } from "../../api/generated/zod.gen";
import { clientError, type ApiError } from "../../api/problem";
import {
  problemLines,
  routeFieldErrors,
  schemaIssuePaths,
  tooLongMessage,
  tooLongText,
  unmatchedLines,
  useFieldFocus,
} from "./formErrors";

function unprocessable(errors: { path: string; reason: string }[]): ApiError {
  return { ...clientError("problem", { status: 422, code: "validation_failed" }), errors };
}

describe("form errors", () => {
  test("T-361 body entries go to their fields with the screen's text; the rest to the error bar", () => {
    const routed = routeFieldErrors(
      unprocessable([
        { path: "body.exampleQuestions.2", reason: "too_long" },
        { path: "body.name", reason: "duplicate" },
        { path: "query.limit", reason: "out_of_range" },
        { path: "body.nope", reason: "unknown_field" },
      ]),
      ["name", "exampleQuestions.2"],
      (field, reason) => (reason === "too_long" ? `long ${field}` : null),
    );
    expect([...routed.messages]).toEqual([
      ["exampleQuestions.2", "long exampleQuestions.2"],
      ["name", "This value isn't valid. Check it and correct it."],
    ]);
    expect(unmatchedLines(routed.unmatched)).toEqual([
      "query.limit: out_of_range",
      "body.nope: unknown_field",
    ]);
    expect(
      routeFieldErrors(unprocessable([{ path: "body.name", reason: "x" }]), ["name"]).messages.get(
        "name",
      ),
    ).toBe("This value isn't valid. Check it and correct it.");
  });

  test("a 428 adds the defect line; other failures add nothing", () => {
    expect(problemLines(clientError("problem", { status: 428 }))).toEqual([
      "Reload the page. If this keeps happening, send the reference ID to your DocChat administrator.",
    ]);
    expect(problemLines(clientError("problem", { status: 409 }))).toEqual([]);
  });

  test("limits count code points", () => {
    expect(tooLongMessage(80)).toBe("This can be up to 80 characters.");
    expect(tooLongText("😀".repeat(80), 80)).toBeNull();
    expect(tooLongText("x".repeat(81), 80)).toBe("This can be up to 80 characters.");
  });

  test("the generated schema's issues come back as dotted paths, text lengths left to code points", () => {
    expect(schemaIssuePaths(zProjectCreate, { name: "A" })).toEqual([]);
    expect(schemaIssuePaths(zProjectCreate, { name: "😀".repeat(80) })).toEqual([]);
    expect(
      schemaIssuePaths(zProjectCreate, {
        name: "A",
        exampleQuestions: ["ok", ""],
        isDefault: "no",
      }),
    ).toEqual(["exampleQuestions.1", "isDefault"]);
  });

  test("a failed submit focuses the first invalid field in the form's order", () => {
    let focusFirst: ReturnType<typeof useFieldFocus>["focusFirst"] = () => undefined;
    function Form() {
      const focus = useFieldFocus();
      focusFirst = focus.focusFirst;
      return (
        <>
          <input aria-label="a" ref={focus.register("a")} />
          <div ref={focus.register("b")}>
            <input aria-label="b" />
          </div>
        </>
      );
    }
    const { getByLabelText, unmount } = render(<Form />);
    focusFirst(["a", "b"], new Map([["b", "x"]]));
    expect(getByLabelText("b")).toHaveFocus();
    focusFirst(
      ["a", "b"],
      new Map([
        ["a", "x"],
        ["b", "x"],
      ]),
    );
    expect(getByLabelText("a")).toHaveFocus();
    focusFirst(["a"], new Map([["c", "x"]]));
    expect(getByLabelText("a")).toHaveFocus();
    unmount();
  });
});
