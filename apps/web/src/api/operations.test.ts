import { describe, expect, test } from "vitest";

import { readRepositoryText } from "../test/files";
import { operations, type OperationId } from "./operations";

interface DocumentOperation {
  readonly method: string;
  readonly path: string;
  readonly ifMatch: boolean;
}

/** Every operation of the committed OpenAPI document, by operationId. */
function documentOperations(): Map<string, DocumentOperation> {
  const document = JSON.parse(readRepositoryText("apps/api/openapi.json")) as {
    paths: Record<
      string,
      Record<string, { operationId: string; parameters?: { name: string; in: string }[] }>
    >;
  };
  const found = new Map<string, DocumentOperation>();
  for (const [path, methods] of Object.entries(document.paths)) {
    for (const [method, operation] of Object.entries(methods)) {
      found.set(operation.operationId, {
        method: method.toUpperCase(),
        path,
        ifMatch: (operation.parameters ?? []).some(
          (parameter) => parameter.in === "header" && parameter.name === "If-Match",
        ),
      });
    }
  }
  return found;
}

/** The text of one section of `10-web-client.md`. */
function section(heading: string): string {
  const text = readRepositoryText("docs/spec/10-web-client.md");
  const start = text.indexOf(`\n### ${heading}`);
  const end = text.indexOf("\n### ", start + 1);
  return text.slice(start, end);
}

/** The operationIds in the first column of section 5.2's table. */
function specifiedOperationIds(): string[] {
  return [...section("5.2 ").matchAll(/^\| `([A-Za-z]+)` \(/gm)].map((match) => match[1] ?? "");
}

describe("the operation table", () => {
  test("T-346 every entry matches the OpenAPI document's method and path", () => {
    const document = documentOperations();
    for (const [operationId, spec] of Object.entries(operations)) {
      const found = document.get(operationId);
      expect(found, operationId).toBeDefined();
      expect({ method: spec.method, path: spec.path }, operationId).toEqual({
        method: found?.method,
        path: found?.path,
      });
    }
  });

  test("T-346 the table holds exactly the operations of section 5.2", () => {
    const specified = specifiedOperationIds();
    expect(specified).toHaveLength(44);
    expect(specified).not.toContain("getHealth");
    expect(Object.keys(operations).sort()).toEqual([...specified].sort());
  });

  test("T-363 If-Match goes on exactly the 14 operations of section 5.6, as the document says", () => {
    // The first column of the section's table.
    const listed = section("5.6 ")
      .split("\n")
      .filter((line) => line.startsWith("| `"))
      .flatMap((line) => [...(line.split("|")[1] ?? "").matchAll(/`([a-z][A-Za-z]+)`/g)])
      .map((match) => match[1] ?? "");
    const withIfMatch = Object.entries(operations)
      .filter(([, spec]) => spec.ifMatch)
      .map(([id]) => id);
    expect(withIfMatch).toHaveLength(14);
    expect(withIfMatch.sort()).toEqual([...new Set(listed)].sort());
    const document = documentOperations();
    for (const id of Object.keys(operations) as OperationId[]) {
      expect(operations[id].ifMatch, id).toBe(document.get(id)?.ifMatch);
    }
  });

  test("T-363 feedback, upload and create operations send no If-Match", () => {
    for (const id of [
      "putTurnFeedback",
      "adminPreflightUploads",
      "adminReserveUpload",
      "adminUploadContent",
      "adminSubmitUploads",
      "adminCreateProject",
      "adminCreateIndex",
      "createChatTurn",
    ] as const) {
      expect(operations[id].ifMatch, id).toBe(false);
    }
  });

  test("the three file operations read PDFs within their limits", () => {
    const files = Object.entries(operations).filter(([, spec]) => spec.answer === "file");
    expect(files.map(([id]) => id).sort()).toEqual([
      "adminGetDocumentPdf",
      "getCitationContent",
      "getCitationDocument",
    ]);
    expect(operations.getCitationContent.maxBytes).toBe(20_971_520);
    expect(operations.getCitationDocument.maxBytes).toBe(104_857_600);
    expect(operations.adminGetDocumentPdf.maxBytes).toBe(104_857_600);
    expect(operations.createChatTurn.answer).toBe("ndjson");
    expect(operations.adminUploadContent.answer).toBe("upload");
  });
});
