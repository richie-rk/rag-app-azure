import { fireEvent, screen } from "@testing-library/react";
import { useState } from "react";
import { describe, expect, test, vi } from "vitest";

import { PAGEWISE_SCHEMA, RECURSIVE_SCHEMA } from "../../test/adminData";
import { renderWithProviders } from "../../test/render";
import {
  ParamForm,
  describeParameters,
  formatParameter,
  initialParameters,
  invalidParameters,
  parameterField,
  parametersToSend,
  parseParameterSchema,
  type ParamSchema,
  type ParamValues,
} from "./ParamForm";

function parsed(schema: unknown): ParamSchema {
  const result = parseParameterSchema(schema);
  if (result === null) throw new Error("unsupported");
  return result;
}

const TEXT_SCHEMA = {
  type: "object",
  additionalProperties: false,
  required: ["label"],
  properties: {
    label: { type: "string", title: "Label", minLength: 2, maxLength: 5 },
    mode: { type: "string", enum: ["fast", "slow"], default: "fast" },
    ratio: { type: "number", minimum: 0, maximum: 1 },
  },
};

describe("the method settings form's schema subset", () => {
  test("parses both shipped schemas", () => {
    const pagewise = parsed(PAGEWISE_SCHEMA);
    expect(pagewise.properties.map((p) => [p.name, p.kind, p.required])).toEqual([
      ["summarizeTables", "boolean", true],
      ["ocrLanguages", "choices", true],
    ]);
    const recursive = parsed(RECURSIVE_SCHEMA);
    expect(recursive.properties[1]).toMatchObject({
      name: "overlapPercent",
      kind: "number",
      integer: true,
      minimum: 0,
      maximum: 50,
      multipleOf: 5,
      title: "Overlap between passages (%)",
    });
    expect(parsed(TEXT_SCHEMA).properties.map((p) => p.kind)).toEqual(["text", "choice", "number"]);
  });

  test.each([
    ["a root keyword outside the subset", { ...RECURSIVE_SCHEMA, $schema: "x" }],
    ["additionalProperties allowed", { ...RECURSIVE_SCHEMA, additionalProperties: true }],
    ["a root that isn't an object", { ...RECURSIVE_SCHEMA, type: "array" }],
    ["required naming an unknown property", { ...RECURSIVE_SCHEMA, required: ["nope"] }],
    ["required that isn't a list of names", { ...RECURSIVE_SCHEMA, required: [1] }],
    [
      "a property keyword outside the subset",
      { ...TEXT_SCHEMA, properties: { label: { type: "string", pattern: "^a" } } },
    ],
    [
      "an unsupported type",
      { ...TEXT_SCHEMA, required: [], properties: { x: { type: "object" } } },
    ],
    [
      "an array of free text",
      {
        ...TEXT_SCHEMA,
        required: [],
        properties: { x: { type: "array", items: { type: "string" } } },
      },
    ],
    [
      "uniqueItems false",
      {
        ...TEXT_SCHEMA,
        required: [],
        properties: {
          x: { type: "array", uniqueItems: false, items: { type: "string", enum: ["a"] } },
        },
      },
    ],
    [
      "a oneOf entry with another keyword",
      {
        ...TEXT_SCHEMA,
        required: [],
        properties: {
          x: { type: "string", oneOf: [{ const: "a", title: "A", description: "d" }] },
        },
      },
    ],
    [
      "both enum and oneOf",
      {
        ...TEXT_SCHEMA,
        required: [],
        properties: { x: { type: "string", enum: ["a"], oneOf: [{ const: "a" }] } },
      },
    ],
    [
      "a non-numeric bound",
      { ...TEXT_SCHEMA, required: [], properties: { x: { type: "integer", minimum: "1" } } },
    ],
    [
      "a zero multipleOf",
      { ...TEXT_SCHEMA, required: [], properties: { x: { type: "integer", multipleOf: 0 } } },
    ],
    [
      "a negative maxLength",
      { ...TEXT_SCHEMA, required: [], properties: { x: { type: "string", maxLength: -1 } } },
    ],
    [
      "a title that isn't text",
      { ...TEXT_SCHEMA, properties: { label: { type: "string", title: 1 } } },
    ],
    [
      "an empty enum",
      { ...TEXT_SCHEMA, required: [], properties: { x: { type: "string", enum: [] } } },
    ],
    ["no schema", null],
  ])("T-401 %s is unsupported", (_, schema) => {
    expect(parseParameterSchema(schema)).toBeNull();
  });

  test("starting values come from defaultParameters, else each property's default", () => {
    const schema = parsed(TEXT_SCHEMA);
    expect(initialParameters(schema, { label: "ab" })).toEqual({ label: "ab", mode: "fast" });
  });

  test("checks follow the schema: required, integer, bounds, steps, lengths and item counts", () => {
    const recursive = parsed(RECURSIVE_SCHEMA);
    expect(
      invalidParameters(recursive, { targetTokens: 512, overlapPercent: 25, tableMaxTokens: 1600 }),
    ).toEqual([]);
    expect(
      invalidParameters(recursive, { targetTokens: 100, overlapPercent: 27, tableMaxTokens: 1.5 }),
    ).toEqual(["targetTokens", "overlapPercent", "tableMaxTokens"]);
    expect(invalidParameters(recursive, { targetTokens: "512" })).toEqual([
      "targetTokens",
      "overlapPercent",
      "tableMaxTokens",
    ]);
    const pagewise = parsed(PAGEWISE_SCHEMA);
    expect(invalidParameters(pagewise, { summarizeTables: "yes", ocrLanguages: [] })).toEqual([
      "summarizeTables",
      "ocrLanguages",
    ]);
    expect(
      invalidParameters(pagewise, {
        summarizeTables: true,
        ocrLanguages: ["eng", "fra", "deu", "spa"],
      }),
    ).toEqual(["ocrLanguages"]);
    expect(
      invalidParameters(pagewise, { summarizeTables: true, ocrLanguages: ["eng", "eng"] }),
    ).toEqual(["ocrLanguages"]);
    const text = parsed(TEXT_SCHEMA);
    expect(invalidParameters(text, { label: "a", mode: "other" })).toEqual(["label", "mode"]);
    expect(invalidParameters(text, { label: 5, mode: "fast", ratio: 0.5 })).toEqual(["label"]);
    expect(invalidParameters(text, { label: "abc" })).toEqual([]);
  });

  test("only the schema's non-empty settings are sent", () => {
    const schema = parsed(TEXT_SCHEMA);
    expect(parametersToSend(schema, { label: "ab", mode: "", ratio: null, extra: 1 })).toEqual({
      label: "ab",
    });
    expect(parameterField("targetTokens")).toBe("methodParameters.targetTokens");
  });

  test("values read as text: choice titles, Yes or No, Not set", () => {
    const pagewise = parsed(PAGEWISE_SCHEMA);
    expect(formatParameter(pagewise.properties[1], ["eng", "fra"])).toBe("English, French");
    expect(formatParameter(pagewise.properties[0], false)).toBe("No");
    expect(formatParameter(undefined, null)).toBe("Not set");
    expect(describeParameters(null, { targetTokens: 512 })).toEqual([
      { name: "targetTokens", title: "targetTokens", value: "512" },
    ]);
    expect(describeParameters(pagewise, { summarizeTables: true, ocrLanguages: ["deu"] })).toEqual([
      { name: "summarizeTables", title: "Summarise tables", value: "Yes" },
      { name: "ocrLanguages", title: "Languages in scanned pages", value: "German" },
    ]);
  });
});

function Harness({ schema, initial }: { schema: ParamSchema; initial: ParamValues }) {
  const [values, setValues] = useState(initial);
  return (
    <>
      <ParamForm
        schema={schema}
        values={values}
        onChange={setValues}
        errors={new Map([["methodParameters.targetTokens", "This value isn't valid."]])}
      />
      <output data-testid="values">{JSON.stringify(values)}</output>
    </>
  );
}

describe("the method settings form", () => {
  test("renders a SpinButton per number with its bounds, marks required fields and shows errors", () => {
    renderWithProviders(
      <Harness
        schema={parsed(RECURSIVE_SCHEMA)}
        initial={{ targetTokens: 512, overlapPercent: 25, tableMaxTokens: 1600 }}
      />,
    );
    const size = screen.getByRole("spinbutton", { name: /Passage size \(tokens\)/ });
    expect(size).toHaveValue("512");
    expect(size).toHaveAttribute("aria-valuemin", "128");
    expect(size).toHaveAttribute("aria-valuemax", "2048");
    expect(size).toBeRequired();
    expect(size).toHaveAttribute("aria-invalid", "true");
    expect(screen.getByText("This value isn't valid.")).toBeInTheDocument();
    expect(
      screen.getByText(
        "Share of each passage repeated at the start of the next one in the same section.",
      ),
    ).toBeInTheDocument();
    fireEvent.change(size, { target: { value: "640" } });
    fireEvent.blur(size);
    expect(screen.getByTestId("values").textContent).toContain('"targetTokens":640');
    fireEvent.change(size, { target: { value: "" } });
    fireEvent.blur(size);
    expect(screen.getByTestId("values").textContent).toContain('"targetTokens":null');
  });

  test("a Switch per boolean, a multi-select per enumerated list, a Dropdown and an Input", async () => {
    const onChange = vi.fn();
    const { user } = renderWithProviders(
      <Harness
        schema={parsed(PAGEWISE_SCHEMA)}
        initial={{ summarizeTables: true, ocrLanguages: ["eng"] }}
      />,
    );
    await user.click(screen.getByRole("switch", { name: /Summarise tables/ }));
    expect(screen.getByTestId("values").textContent).toContain('"summarizeTables":false');
    await user.click(screen.getByRole("combobox", { name: /Languages in scanned pages/ }));
    await user.click(await screen.findByRole("menuitemcheckbox", { name: "German" }));
    expect(screen.getByTestId("values").textContent).toContain('"ocrLanguages":["eng","deu"]');
    expect(onChange).not.toHaveBeenCalled();
  });

  test("a Dropdown per enumerated string and an Input per free text", async () => {
    const { user } = renderWithProviders(
      <Harness schema={parsed(TEXT_SCHEMA)} initial={{ label: "ab", mode: "fast" }} />,
    );
    await user.type(screen.getByRole("textbox", { name: /Label/ }), "c");
    expect(screen.getByTestId("values").textContent).toContain('"label":"abc"');
    await user.click(screen.getByRole("combobox", { name: "mode" }));
    await user.click(await screen.findByRole("option", { name: "slow" }));
    expect(screen.getByTestId("values").textContent).toContain('"mode":"slow"');
    const ratio = screen.getByRole("spinbutton", { name: "ratio" });
    fireEvent.change(ratio, { target: { value: "abc" } });
    fireEvent.blur(ratio);
    expect(screen.getByTestId("values").textContent).toContain('"ratio":"abc"');
  });
});
