import { describe, expect, test } from "vitest";

import { indexOptions } from "../../test/adminData";
import { UPLOAD_ACCEPT, checkFile, checkSelection, extensionOf, sha256Hex } from "./uploadChecks";

const LIMITS = indexOptions().uploadLimits;

function file(name: string, size = 10): Pick<File, "name" | "size"> {
  return { name, size };
}

describe("the checks before hashing (section 10.1)", () => {
  test("T-403 a .PDF file passes, compared case-insensitively, as do .docx and .pptx", () => {
    expect(checkFile(file("Policy.PDF"), LIMITS)).toBe("accepted");
    expect(checkFile(file("notes.docx"), LIMITS)).toBe("accepted");
    expect(checkFile(file("deck.PpTx"), LIMITS)).toBe("accepted");
  });

  test("T-403 .xlsx, .png, .zip and a name without an extension are rejected by type", () => {
    for (const name of ["sheet.xlsx", "image.png", "archive.zip", "README", "pdf"]) {
      expect(checkFile(file(name), LIMITS)).toBe("rejectedType");
    }
  });

  test("T-403 a file above maxFileBytes is rejected by size; one at the limit passes", () => {
    expect(checkFile(file("big.pdf", LIMITS.maxFileBytes + 1), LIMITS)).toBe("rejectedSize");
    expect(checkFile(file("edge.pdf", LIMITS.maxFileBytes), LIMITS)).toBe("accepted");
    // The type is checked first.
    expect(checkFile(file("big.zip", LIMITS.maxFileBytes + 1), LIMITS)).toBe("rejectedType");
  });

  test("T-403 a selection that would make 101 files is refused whole", () => {
    const files = Array.from({ length: 2 }, (_, index) => file(`f${String(index)}.pdf`));
    expect(checkSelection(99, files, LIMITS)).toEqual({ tooMany: true });
    expect(checkSelection(98, files, LIMITS)).toEqual({
      tooMany: false,
      files: ["accepted", "accepted"],
    });
    expect(
      checkSelection(0, [file("a.pdf"), file("b.png")], { ...LIMITS, maxFilesPerBatch: 1 }),
    ).toEqual({ tooMany: true });
  });

  test("the checks follow the limits the API names", () => {
    const limits = { ...LIMITS, maxFileBytes: 5, allowedExtensions: [".pdf", ".docx", ".PPTX"] };
    expect(checkSelection(0, [file("a.pptx", 5), file("b.pdf", 6)], limits as never)).toEqual({
      tooMany: false,
      files: ["accepted", "rejectedSize"],
    });
  });

  test("extensions and the input's accept list", () => {
    expect(extensionOf("a.b.PDF")).toBe(".pdf");
    expect(extensionOf("none")).toBe("");
    expect(UPLOAD_ACCEPT).toBe(".pdf,.docx,.pptx");
  });
});

describe("hashing (section 10.2)", () => {
  test("a file's SHA-256 is 64 lower-case hexadecimal characters", async () => {
    expect(await sha256Hex(new File(["abc"], "a.pdf"))).toBe(
      "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
    );
  });

  test("a file that can't be read rejects", async () => {
    const unreadable = new File(["abc"], "a.pdf");
    Object.defineProperty(unreadable, "arrayBuffer", {
      value: () => Promise.reject(new Error("NotReadableError")),
    });
    await expect(sha256Hex(unreadable)).rejects.toThrow("NotReadableError");
  });
});
