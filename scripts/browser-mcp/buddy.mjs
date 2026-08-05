// Thin wrapper around the `buddy` CLI, pinned to the smoke data dir so it reads
// the same database the extension synced into.
import { spawn } from "node:child_process";

import { BACKEND_DIR, DATA_DIR } from "./config.mjs";

/**
 * Run `uv run buddy <args...>` in the backend dir with the smoke data dir set.
 *
 * @param {string[]} args CLI arguments (e.g. ["sessions"] or ["search", "foo"]).
 * @returns {Promise<{stdout: string, stderr: string, code: number}>}
 */
export function runBuddy(args) {
  return new Promise((resolve) => {
    const proc = spawn("uv", ["run", "buddy", ...args], {
      cwd: BACKEND_DIR,
      env: { ...process.env, BROWSERBUDDY_DATA_DIR: DATA_DIR },
    });
    let stdout = "";
    let stderr = "";
    proc.stdout.on("data", (d) => (stdout += d));
    proc.stderr.on("data", (d) => (stderr += d));
    proc.on("error", (err) => resolve({ stdout, stderr: stderr + err.message, code: -1 }));
    proc.on("close", (code) => resolve({ stdout, stderr, code: code ?? -1 }));
  });
}
