import { describe, expect, test } from "vitest";

import type { UploadPreflightResult } from "../../api/generated/types.gen";
import { uuid } from "../../test/adminData";
import type { UploadRow } from "./uploadBatch";
import {
  checkText,
  maxSizeText,
  preflightText,
  progressText,
  resultText,
  sharedIndexText,
  summaryText,
} from "./uploadText";

const MAX = 52_428_800;

function result(overrides: Partial<UploadPreflightResult>): UploadPreflightResult {
  return {
    name: "a.pdf",
    status: "new",
    reason: null,
    documentId: null,
    existingDocument: null,
    currentVersionNumber: null,
    ...overrides,
  };
}

function row(overrides: Partial<UploadRow>): UploadRow {
  return {
    key: "file-1",
    file: new File(["x"], "a.pdf"),
    sha256: null,
    check: { kind: "preflight", result: result({}) },
    confirmUpdate: false,
    upload: { kind: "none" },
    ...overrides,
  };
}

describe("upload text (section 10.10)", () => {
  test("each preflight result picks its text (section 10.3)", () => {
    expect(preflightText(result({ status: "new" }), MAX)).toBe("New");
    expect(preflightText(result({ status: "unchanged" }), MAX)).toBe("Already uploaded. Skipped.");
    expect(
      preflightText(
        result({
          status: "duplicate",
          existingDocument: { documentId: uuid(1), name: "travel.pdf" },
        }),
        MAX,
      ),
    ).toBe("Same file as travel.pdf. Skipped.");
    expect(preflightText(result({ status: "duplicate" }), MAX)).toBe("Same file as . Skipped.");
    expect(preflightText(result({ status: "update" }), MAX)).toBe(
      "This document is already ingested. Update it?",
    );
    const rejected = (reason: UploadPreflightResult["reason"]) =>
      preflightText(result({ status: "rejected", reason }), MAX);
    expect(rejected("unsupported_type")).toBe("Not a PDF, Word or PowerPoint file. Skipped.");
    expect(rejected("too_large")).toBe("Larger than 50.0 MB. Skipped.");
    expect(rejected("empty_file")).toBe("This file is empty. Skipped.");
    expect(rejected("invalid_name")).toBe(
      "This file name can't be used. Rename the file, then choose it in a new selection.",
    );
    expect(rejected("duplicate_in_batch")).toBe("This file is already in this batch. Skipped.");
    expect(rejected(null)).toBe("Can't be uploaded. Skipped.");
  });

  test("the checks before the preflight", () => {
    expect(checkText({ kind: "pending" }, MAX)).toBe("");
    expect(checkText({ kind: "hashed" }, MAX)).toBe("");
    expect(checkText({ kind: "rejectedType" }, MAX)).toBe(
      "Not a PDF, Word or PowerPoint file. Skipped.",
    );
    expect(checkText({ kind: "rejectedSize" }, 1_048_576)).toBe("Larger than 1.0 MB. Skipped.");
    expect(checkText({ kind: "unreadable" }, MAX)).toBe(
      "This file couldn't be read. Check it, then choose it in a new selection.",
    );
    expect(maxSizeText(MAX)).toBe("50.0 MB");
  });

  test("each upload outcome picks its text", () => {
    expect(resultText(row({}), MAX)).toBe("New");
    expect(resultText(row({ upload: { kind: "uploading", percent: 10 } }), MAX)).toBe("New");
    expect(resultText(row({ upload: { kind: "done", versionId: uuid(3) } }), MAX)).toBe("Uploaded");
    expect(resultText(row({ upload: { kind: "failed", reason: "It broke." } }), MAX)).toBe(
      "Not uploaded. It broke.",
    );
    expect(resultText(row({ upload: { kind: "skipped", reason: "Same bytes." } }), MAX)).toBe(
      "Same bytes.",
    );
    expect(resultText(row({ upload: { kind: "stopped" } }), MAX)).toBe(
      "Not uploaded. You stopped the upload.",
    );
    expect(resultText(row({ upload: { kind: "paused" } }), MAX)).toBe(
      "Not uploaded. Uploads are paused.",
    );
    expect(progressText("a.pdf", 42)).toBe("Uploading a.pdf: 42%");
  });

  test("T-418 the shared-index warning names every project up to 10, then the count of the rest", () => {
    const projects = (count: number) =>
      Array.from({ length: count }, (_, index) => ({
        projectId: uuid(index + 1),
        name: `P${String(index + 1)}`,
      }));
    expect(sharedIndexText(projects(1))).toBe(
      "Everyone in the project that uses this index can find these documents in chat: P1.",
    );
    expect(sharedIndexText(projects(3))).toBe(
      "Everyone in the 3 projects that use this index can find these documents in chat: P1, P2, P3.",
    );
    expect(sharedIndexText(projects(12))).toBe(
      "Everyone in the 12 projects that use this index can find these documents in chat: P1, P2, P3, P4, P5, P6, P7, P8, P9, P10 and 2 more.",
    );
    const unknown =
      "Everyone in every project that uses this index can find these documents in chat.";
    expect(sharedIndexText(null)).toBe(unknown);
    expect(sharedIndexText([])).toBe(unknown);
  });

  test("the summary: complete when every chosen file was submitted, otherwise partial", () => {
    expect(summaryText({ runId: uuid(4), submitted: 1, total: 1 })).toEqual({
      complete: true,
      title: "Upload complete",
      body: "Ingestion has started for 1 file.",
    });
    expect(summaryText({ runId: uuid(4), submitted: 3, total: 3 }).body).toBe(
      "Ingestion has started for 3 files.",
    );
    expect(summaryText({ runId: null, submitted: 2, total: 3 })).toEqual({
      complete: false,
      title: null,
      body: "2 of 3 files uploaded. Ingestion has started for those. Choose the others in a new batch to upload them.",
    });
  });
});
