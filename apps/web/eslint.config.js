import js from "@eslint/js";
import prettier from "eslint-config-prettier";
import jsxA11y from "eslint-plugin-jsx-a11y";
import reactHooks from "eslint-plugin-react-hooks";
import { defineConfig, globalIgnores } from "eslint/config";
import globals from "globals";
import tseslint from "typescript-eslint";

// Each restriction below gives one choke point (10-web-client.md section 1.6). A file that is the
// choke point for one of them restates the others, because a rule's options replace each other
// across configuration blocks rather than merging.

const htmlInsertion = [
  {
    selector: "JSXAttribute[name.name='dangerouslySetInnerHTML']",
    message: "Only src/chat/render/AnswerBody.tsx inserts HTML.",
  },
];
const otherSyntax = [
  {
    selector: "AssignmentExpression[left.property.name=/^(innerHTML|outerHTML)$/]",
    message: "Assigning innerHTML or outerHTML inserts HTML outside AnswerBody.",
  },
  {
    selector: "CallExpression[callee.property.name='insertAdjacentHTML']",
    message: "insertAdjacentHTML inserts HTML outside AnswerBody.",
  },
  {
    selector:
      "CallExpression[callee.object.name='document'][callee.property.name=/^(write|writeln)$/]",
    message: "document.write inserts HTML outside AnswerBody.",
  },
  {
    selector: "CallExpression[callee.object.name='z'][callee.property.name='compile']",
    message: "Compiled Zod validators need new Function, which the site's CSP refuses.",
  },
];

const networkGlobals = [
  { name: "fetch", message: "Only src/api/http.ts calls fetch." },
  { name: "XMLHttpRequest", message: "Only src/api/http.ts uses XMLHttpRequest." },
];
const storageGlobals = [
  { name: "localStorage", message: "Only src/platform/storage.ts touches browser storage." },
  { name: "sessionStorage", message: "Only src/platform/storage.ts touches browser storage." },
  { name: "indexedDB", message: "Only src/platform/storage.ts touches browser storage." },
];

const renderImports = [
  {
    group: ["dompurify", "unified", "remark-*", "rehype-*"],
    message: "Rendering lives in src/chat/render/.",
  },
];
const msalImports = [
  {
    group: ["@azure/msal-browser"],
    allowTypeImports: true,
    message: "MSAL is used only in src/bootstrap/ and src/auth/.",
  },
];

export default defineConfig(
  globalIgnores(["dist/", "coverage/", "src/api/generated/**"]),
  js.configs.recommended,
  tseslint.configs.strictTypeChecked,
  tseslint.configs.stylisticTypeChecked,
  reactHooks.configs.flat.recommended,
  jsxA11y.flatConfigs.strict,
  {
    languageOptions: {
      globals: globals.browser,
      parserOptions: { projectService: true, tsconfigRootDir: import.meta.dirname },
    },
    rules: {
      "@typescript-eslint/no-floating-promises": "error",
      "no-console": "error",
      "no-eval": "error",
      "no-implied-eval": "error",
      "no-new-func": "error",
      "no-restricted-syntax": ["error", ...htmlInsertion, ...otherSyntax],
      "no-restricted-globals": ["error", ...networkGlobals, ...storageGlobals],
      "no-restricted-imports": ["error", { patterns: [...renderImports, ...msalImports] }],
    },
  },
  {
    files: ["src/chat/render/AnswerBody.tsx"],
    rules: { "no-restricted-syntax": ["error", ...otherSyntax] },
  },
  {
    files: ["src/api/http.ts"],
    rules: { "no-restricted-globals": ["error", ...storageGlobals] },
  },
  {
    files: ["src/platform/storage.ts"],
    rules: { "no-restricted-globals": ["error", ...networkGlobals] },
  },
  {
    files: ["src/chat/render/**"],
    rules: { "no-restricted-imports": ["error", { patterns: msalImports }] },
  },
  {
    files: ["src/bootstrap/**", "src/auth/**"],
    rules: { "no-restricted-imports": ["error", { patterns: renderImports }] },
  },
  {
    // The one development-only log; import.meta.env.DEV removes it from production builds.
    files: ["src/platform/devLog.ts"],
    rules: { "no-console": "off" },
  },
  {
    files: ["**/*.js", "**/*.mjs"],
    extends: [tseslint.configs.disableTypeChecked],
    languageOptions: { globals: globals.node },
  },
  {
    files: ["scripts/**/*.ts", "vite.config.ts", "vite.config.test.ts", "openapi-ts.config.ts"],
    languageOptions: { globals: globals.node },
  },
  prettier,
);
