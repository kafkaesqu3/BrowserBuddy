// Shared paths and constants for the BrowserBuddy Firefox smoke-test MCP server.
//
// Everything is derived from this file's location so the server works no matter
// what cwd it is launched from.
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
export const REPO_ROOT = resolve(HERE, "..", "..");

export const SMOKE_DIR = resolve(REPO_ROOT, ".smoke");
export const DATA_DIR = resolve(SMOKE_DIR, "data");
export const BACKEND_DIR = resolve(REPO_ROOT, "backend");
export const EXTENSION_DIR = resolve(REPO_ROOT, "extension", "dist", "firefox");

export const HOST = process.env.BROWSERBUDDY_HOST ?? "127.0.0.1";
// A dedicated smoke port (not the default 8765) so the smoke backend stays
// isolated from any dev `buddy serve` a developer already has running — the
// extension must pair with OUR backend (smoke data dir) for the secret to match
// and for `buddy` inspection to reflect what the extension synced.
export const PORT = Number(process.env.BROWSERBUDDY_PORT ?? 8799);
export const WS_URL = `ws://${HOST}:${PORT}/ws`;

// The extension's fixed add-on id (extension/manifest.firefox.json). Pinning a
// UUID for it via the `extensions.webextensions.uuids` pref makes the
// moz-extension:// origin deterministic, so popup/options URLs are knowable
// up front instead of scraped from about:debugging.
export const EXTENSION_ID = "browserbuddy@tulis.dev";
export const EXTENSION_UUID = "d3b8f8a2-1c4e-4b6a-9f2d-5e7c9a1b2c3d";

const ORIGIN = `moz-extension://${EXTENSION_UUID}`;
export const POPUP_URL = `${ORIGIN}/src/popup/popup.html`;
export const OPTIONS_URL = `${ORIGIN}/src/options/options.html`;

// Headful by default so a human can watch; set BROWSERBUDDY_HEADLESS=1 for CI.
export const HEADLESS = process.env.BROWSERBUDDY_HEADLESS === "1";
