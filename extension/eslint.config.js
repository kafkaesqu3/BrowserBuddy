import js from "@eslint/js";
import ts from "typescript-eslint";

export default ts.config(
  { ignores: ["dist/", "node_modules/"] },
  js.configs.recommended,
  ...ts.configs.recommended,
  // Build scripts run in Node — grant Node globals.
  {
    files: ["scripts/**/*.mjs"],
    languageOptions: { globals: { URL: "readonly", console: "readonly" } },
  },
);
