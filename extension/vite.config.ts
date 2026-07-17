import { resolve } from "node:path";
import { defineConfig } from "vitest/config";

export default defineConfig({
  resolve: { alias: { "@src": resolve(import.meta.dirname, "src") } },
  build: {
    outDir: "dist/assets",
    emptyOutDir: true,
    rollupOptions: {
      input: {
        background: resolve(import.meta.dirname, "src/background.ts"),
        options: resolve(import.meta.dirname, "src/options/options.html"),
        popup: resolve(import.meta.dirname, "src/popup/popup.html"),
      },
      output: { entryFileNames: "[name].js" },
    },
    // MV3 background must be a plain module graph, no dynamic-import splitting.
    modulePreload: false,
    target: "es2022",
  },
  test: { environment: "node", include: ["tests/**/*.test.ts"] },
});
