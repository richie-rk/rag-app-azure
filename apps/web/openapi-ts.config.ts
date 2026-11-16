import { defineConfig } from "@hey-api/openapi-ts";

export default defineConfig({
  input: "../api/openapi.json",
  output: "src/api/generated",
  plugins: ["@hey-api/typescript", { name: "zod", definitions: true }],
});
