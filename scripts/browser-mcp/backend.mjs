// Backend lifecycle: make sure `buddy serve` is listening before we drive the
// browser, so the extension has somewhere to sync to and buddy_run inspects the
// same database. Reuses an already-running backend; only tears down one we own.
import { spawn } from "node:child_process";
import net from "node:net";

import { BACKEND_DIR, DATA_DIR, HOST, PORT } from "./config.mjs";

/** Resolve true if something is accepting TCP connections on HOST:PORT. */
function isPortOpen() {
  return new Promise((resolveOpen) => {
    const socket = net.connect({ host: HOST, port: PORT });
    const done = (open) => {
      socket.destroy();
      resolveOpen(open);
    };
    socket.setTimeout(1000);
    socket.once("connect", () => done(true));
    socket.once("timeout", () => done(false));
    socket.once("error", () => done(false));
  });
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function waitForPort(timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (await isPortOpen()) return true;
    await sleep(500);
  }
  return false;
}

/**
 * Ensure the ingest backend is up.
 *
 * @returns {Promise<{owned: boolean, proc: import('node:child_process').ChildProcess|null}>}
 *   `owned` is true when we spawned it (and must stop it on shutdown).
 */
export async function ensureBackend() {
  if (await isPortOpen()) {
    return { owned: false, proc: null };
  }

  const proc = spawn("uv", ["run", "buddy", "serve"], {
    cwd: BACKEND_DIR,
    env: {
      ...process.env,
      BROWSERBUDDY_DATA_DIR: DATA_DIR,
      BROWSERBUDDY_HOST: HOST,
      BROWSERBUDDY_PORT: String(PORT),
    },
    stdio: "ignore",
    detached: false,
  });
  proc.on("error", (err) => {
    process.stderr.write(`[backend] failed to spawn: ${err.message}\n`);
  });

  const up = await waitForPort(15000);
  if (!up) {
    proc.kill("SIGTERM");
    throw new Error(
      `Backend did not come up on ${HOST}:${PORT} within 15s. ` +
        `Check that 'uv' is installed and 'uv run buddy serve' works in ${BACKEND_DIR}.`,
    );
  }
  return { owned: true, proc };
}

/** Stop a backend we started; no-op for a reused one. */
export function stopBackend(state) {
  if (state?.owned && state.proc && !state.proc.killed) {
    state.proc.kill("SIGTERM");
  }
}
