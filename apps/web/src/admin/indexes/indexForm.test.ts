import { describe, expect, test } from "vitest";

import { clientError, type ApiError } from "../../api/problem";
import { PAGEWISE_METHOD, indexDetail, indexOptions, methodOption } from "../../test/adminData";
import { parseParameterSchema } from "../shared/ParamForm";
import {
  checkIndexForm,
  createIndexBody,
  defaultIndexValues,
  indexFieldOrder,
  indexFormValues,
  indexReasonText,
  indexSchemaFields,
  methodDefaults,
  methodLabel,
  methodOf,
  methodValue,
  parseMethodValue,
  routeIndexErrors,
  schemaOf,
  settingsChanged,
  updateIndexBody,
  withMethod,
  withModel,
} from "./indexForm";

const OPTIONS = indexOptions();
const RECURSIVE = schemaOf(methodOf(OPTIONS, "recursive@1"));

function unprocessable(errors: { path: string; reason: string }[]): ApiError {
  return { ...clientError("problem", { status: 422 }), errors };
}

describe("the index form", () => {
  test("New index starts from defaults, with the default method's settings", () => {
    expect(defaultIndexValues(OPTIONS)).toEqual({
      displayName: "",
      description: "",
      method: "recursive@1",
      methodParameters: { targetTokens: 512, overlapPercent: 25, tableMaxTokens: 1600 },
      embeddingModel: "text-embedding-3-large",
      embeddingDimensions: 1024,
      languageAnalyzer: "standard.lucene",
    });
  });

  test("Edit settings starts from the index's values", () => {
    expect(indexFormValues(indexDetail({ description: null }))).toMatchObject({
      displayName: "Policy library",
      description: "",
      method: "recursive@1",
      embeddingDimensions: 1024,
    });
  });

  test("methods: one choice per version, the version shown when a key has several", () => {
    expect(methodValue("recursive", 2)).toBe("recursive@2");
    expect(parseMethodValue("page_wise@12")).toEqual({ key: "page_wise", version: 12 });
    const two = indexOptions({ methods: [methodOption(), methodOption({ version: 2 })] });
    expect(methodLabel(two, methodOption({ version: 2 }))).toBe("By headings and paragraphs (2)");
    expect(methodLabel(OPTIONS, PAGEWISE_METHOD)).toBe("Page by page");
    expect(methodOf(OPTIONS, "nope@1")).toBeUndefined();
    expect(methodDefaults(undefined)).toEqual({});
    expect(methodDefaults(methodOption({ parameterSchema: { type: "array" } }))).toEqual({});
  });

  test("a new method takes its defaults; a new model its defaultDimensions", () => {
    const values = defaultIndexValues(OPTIONS);
    expect(withMethod(OPTIONS, values, "pagewise@1").methodParameters).toEqual({
      summarizeTables: true,
      ocrLanguages: ["eng"],
    });
    expect(withModel(OPTIONS, values, "text-embedding-3-small").embeddingDimensions).toBe(256);
    expect(withModel(OPTIONS, values, "unknown").embeddingDimensions).toBe(1024);
  });

  test("index.changeWarning: a change to the method, settings, model, dimensions or language", () => {
    const base = indexFormValues(indexDetail());
    expect(settingsChanged(base, { ...base, displayName: "x", description: "y" })).toBe(false);
    expect(settingsChanged(base, { ...base, languageAnalyzer: "en.microsoft" })).toBe(true);
    expect(
      settingsChanged(base, {
        ...base,
        methodParameters: { ...base.methodParameters, targetTokens: 640 },
      }),
    ).toBe(true);
  });

  test("the checks before sending", () => {
    const values = defaultIndexValues(OPTIONS);
    expect([...checkIndexForm(values, RECURSIVE)]).toEqual([["displayName", "Enter a name."]]);
    expect([
      ...checkIndexForm(
        {
          ...values,
          displayName: "x".repeat(81),
          description: "x".repeat(1201),
          methodParameters: { ...values.methodParameters, overlapPercent: 27 },
        },
        RECURSIVE,
      ),
    ]).toEqual([
      ["displayName", "This can be up to 80 characters."],
      ["description", "This can be up to 1200 characters."],
      ["methodParameters.overlapPercent", "This value isn't valid. Check it and correct it."],
    ]);
  });

  test("Create sends every field; an empty description is left out", () => {
    const values = { ...defaultIndexValues(OPTIONS), displayName: " Policy " };
    expect(createIndexBody(values, RECURSIVE)).toEqual({
      displayName: "Policy",
      methodKey: "recursive",
      methodVersion: 1,
      methodParameters: { targetTokens: 512, overlapPercent: 25, tableMaxTokens: 1600 },
      embeddingModel: "text-embedding-3-large",
      embeddingDimensions: 1024,
      languageAnalyzer: "standard.lucene",
    });
    expect(createIndexBody({ ...values, description: "D" }, null)).toMatchObject({
      description: "D",
      methodParameters: {},
    });
  });

  test("Save sends only the changed fields; a new method sends key and version; an emptied description is null", () => {
    const base = indexFormValues(indexDetail());
    expect(updateIndexBody(base, base, RECURSIVE)).toEqual({});
    const pagewise = withMethod(OPTIONS, base, "pagewise@1");
    expect(
      updateIndexBody(
        base,
        { ...pagewise, description: "", embeddingDimensions: 256 },
        schemaOf(PAGEWISE_METHOD),
      ),
    ).toEqual({
      description: null,
      methodKey: "pagewise",
      methodVersion: 1,
      methodParameters: { summarizeTables: true, ocrLanguages: ["eng"] },
      embeddingDimensions: 256,
    });
    expect(
      updateIndexBody(
        base,
        {
          ...base,
          displayName: " New ",
          description: " D ",
          embeddingModel: "text-embedding-3-small",
          languageAnalyzer: "fr.microsoft",
          methodParameters: { targetTokens: 1024 },
        },
        null,
      ),
    ).toEqual({
      displayName: "New",
      description: "D",
      embeddingModel: "text-embedding-3-small",
      languageAnalyzer: "fr.microsoft",
      methodParameters: { targetTokens: 1024 },
    });
  });

  test("T-401 a 422 at body.methodParameters.targetTokens marks that setting; methodKey marks the method", () => {
    const routed = routeIndexErrors(
      unprocessable([
        { path: "body.methodParameters.targetTokens", reason: "out_of_range" },
        { path: "body.methodKey", reason: "not_found" },
        { path: "body.displayName", reason: "too_long" },
        { path: "body.methodParameters.unknown", reason: "unknown_field" },
      ]),
      RECURSIVE,
    );
    expect([...routed.messages]).toEqual([
      ["methodParameters.targetTokens", "This value isn't valid. Check it and correct it."],
      ["method", "This value isn't valid. Check it and correct it."],
      ["displayName", "This can be up to 80 characters."],
    ]);
    expect(routed.unmatched.map((entry) => entry.path)).toEqual(["body.methodParameters.unknown"]);
    expect(indexReasonText("displayName", "required")).toBe("Enter a name.");
    expect(indexReasonText("description", "too_long")).toBe("This can be up to 1200 characters.");
    expect(indexReasonText("method", "too_long")).toBeNull();
    expect(indexReasonText("method", "not_found")).toBeNull();
  });

  test("field order and the generated schema's failures", () => {
    expect(indexFieldOrder(parseParameterSchema(PAGEWISE_METHOD.parameterSchema))).toEqual([
      "displayName",
      "description",
      "method",
      "methodParameters.summarizeTables",
      "methodParameters.ocrLanguages",
      "embeddingModel",
      "embeddingDimensions",
      "languageAnalyzer",
    ]);
    expect(indexFieldOrder(null)).toHaveLength(6);
    expect([
      ...indexSchemaFields({ displayName: "x", methodKey: "", embeddingModel: "m" }, true).keys(),
    ]).toEqual(["method"]);
    expect(indexSchemaFields({ methodVersion: 0 }, false).has("method")).toBe(true);
  });
});
