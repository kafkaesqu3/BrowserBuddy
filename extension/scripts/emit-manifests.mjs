// Merge manifest.base.json with a per-browser overlay and copy built assets
// into dist/<target>/ so each directory is a loadable extension.
import { cpSync, mkdirSync, readFileSync, writeFileSync, rmSync } from "node:fs";
import { join } from "node:path";

const root = new URL("..", import.meta.url).pathname;
const base = JSON.parse(readFileSync(join(root, "manifest.base.json"), "utf8"));

for (const target of ["firefox", "chrome"]) {
  const overlay = JSON.parse(readFileSync(join(root, `manifest.${target}.json`), "utf8"));
  const manifest = { ...base, ...overlay }; // top-level overlay wins; arrays replaced
  const outDir = join(root, "dist", target);
  rmSync(outDir, { recursive: true, force: true });
  mkdirSync(outDir, { recursive: true });
  cpSync(join(root, "dist", "assets"), outDir, { recursive: true });
  writeFileSync(join(outDir, "manifest.json"), JSON.stringify(manifest, null, 2));
  console.log(`wrote dist/${target}/manifest.json`);
}
