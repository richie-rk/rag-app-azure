import { readFileSync, readdirSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, test } from "vitest";
import { z } from "zod";

/** The application's modules: `src/`, without tests, test helpers and generated files. */
function applicationModules(): string[] {
  return readdirSync("src", { recursive: true, encoding: "utf8" })
    .map((path) => join("src", path).split("\\").join("/"))
    .filter(
      (path) =>
        /\.tsx?$/.test(path) &&
        !/\.test\.tsx?$/.test(path) &&
        !path.endsWith(".d.ts") &&
        !path.startsWith("src/test/") &&
        !path.startsWith("src/api/generated/"),
    );
}

/** The specifier of each import declaration of `source` that loads a module at run time, in
 * order: `import type` declarations are erased by the compiler and load nothing. */
function runtimeImports(source: string): string[] {
  const declarations = source.matchAll(/^import\s+(?!type\s)(?:[^"';]*?\sfrom\s+)?"([^"]+)";/gms);
  return Array.from(declarations, (match) => match[1] ?? "");
}

const isZodSetup = (specifier: string) =>
  /(^|\/)bootstrap\/zodSetup$|^\.\/zodSetup$/.test(specifier);
const isGeneratedSchemas = (specifier: string) => specifier.endsWith("/generated/zod.gen");

describe("zodSetup", () => {
  test("T-412 zodSetup sets jitless before any schema is used", async () => {
    await import("./zodSetup");
    expect(z.config().jitless).toBe(true);
  });

  test("T-412 schemas parse without compiling a validator", async () => {
    await import("./zodSetup");
    const schema = z.object({ name: z.string() });
    expect(schema.parse({ name: "x", extra: 1 })).toEqual({ name: "x" });
  });

  test("T-412 main.tsx imports zodSetup first", () => {
    expect(runtimeImports(readFileSync("src/main.tsx", "utf8"))[0]).toBe("./bootstrap/zodSetup");
  });

  // The build moves Zod and the generated schemas into a chunk shared with the lazy screens, which
  // runs before the entry chunk's own code. A module that builds schemas therefore imports
  // zodSetup itself, ahead of the generated schemas, so the bundler keeps the two in that order.
  test("T-412 every module that builds schemas imports zodSetup before them", () => {
    const builders = applicationModules().filter((path) =>
      runtimeImports(readFileSync(path, "utf8")).some(
        (specifier) => specifier === "zod" || isGeneratedSchemas(specifier),
      ),
    );
    expect(builders).toContain("src/api/operations.ts");
    const late = builders.filter((path) => {
      if (path === "src/bootstrap/zodSetup.ts") return false;
      const imports = runtimeImports(readFileSync(path, "utf8"));
      const setup = imports.findIndex(isZodSetup);
      const schemas = imports.findIndex(isGeneratedSchemas);
      return setup === -1 || (schemas !== -1 && schemas < setup);
    });
    expect(late).toEqual([]);
  });
});
