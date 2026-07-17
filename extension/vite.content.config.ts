import { resolve } from "node:path";
import { defineConfig } from "vite";

export default defineConfig({
  resolve: { alias: { "@src": resolve(import.meta.dirname, "src") } },
  build: {
    outDir: "dist/assets",
    emptyOutDir: false, // main pass already populated it
    lib: {
      entry: resolve(import.meta.dirname, "src/content.ts"),
      formats: ["iife"],
      name: "bbcontent",
      fileName: () => "content.js",
    },
    target: "es2022",
  },
});
