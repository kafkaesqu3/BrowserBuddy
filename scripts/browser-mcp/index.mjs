#!/usr/bin/env node
// BrowserBuddy Firefox smoke-test MCP server.
//
// Exposes browser-control tools over stdio so Claude can autonomously run the
// smoke test: launch Firefox with the extension loaded, pair it, start a
// session, browse/click to populate the capture queue, stop, then inspect the
// backend with the `buddy` CLI and read back any console/JS errors.
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import webdriver from "selenium-webdriver";
import { z } from "zod";

import { ensureBackend, stopBackend } from "./backend.mjs";
import { runBuddy } from "./buddy.mjs";
import { EXTENSION_UUID, OPTIONS_URL, POPUP_URL, WS_URL } from "./config.mjs";
import { FirefoxSession } from "./firefox.mjs";

const { By, until } = webdriver;

const session = new FirefoxSession();
let backendState = null;

/** Lazily bring up the backend and browser, then return the live driver. */
async function ensureStarted() {
  if (!backendState) backendState = await ensureBackend();
  if (!session.started) await session.start();
  return session.driver;
}

const text = (s) => ({ content: [{ type: "text", text: s }] });
const json = (obj) => text(JSON.stringify(obj, null, 2));

const server = new McpServer({
  name: "browserbuddy-firefox",
  version: "0.1.0",
});

server.registerTool(
  "start_browser",
  {
    description:
      "Launch Firefox with the BrowserBuddy extension loaded and ensure the backend is running. " +
      "Idempotent; other tools call this automatically on first use.",
    inputSchema: {},
  },
  async () => {
    await ensureStarted();
    return json({
      status: "started",
      extension_uuid: EXTENSION_UUID,
      popup_url: POPUP_URL,
      options_url: OPTIONS_URL,
      backend_owned: backendState?.owned ?? false,
    });
  },
);

server.registerTool(
  "navigate",
  {
    description: "Navigate the current tab to a URL.",
    inputSchema: { url: z.string().describe("Absolute URL to open") },
  },
  async ({ url }) => {
    const driver = await ensureStarted();
    await driver.get(url);
    return text(`navigated to ${await driver.getCurrentUrl()}`);
  },
);

server.registerTool(
  "open_popup",
  {
    description: "Open the extension popup page (moz-extension URL).",
    inputSchema: {},
  },
  async () => {
    const driver = await ensureStarted();
    await driver.get(POPUP_URL);
    return text(`opened popup: ${POPUP_URL}`);
  },
);

server.registerTool(
  "open_options",
  {
    description: "Open the extension options page (moz-extension URL).",
    inputSchema: {},
  },
  async () => {
    const driver = await ensureStarted();
    await driver.get(OPTIONS_URL);
    return text(`opened options: ${OPTIONS_URL}`);
  },
);

server.registerTool(
  "pair",
  {
    description:
      "Pair the extension with the backend: read the shared secret via `buddy secret`, " +
      "fill the options page (WebSocket URL + secret), save, and report the connection status.",
    inputSchema: {},
  },
  async () => {
    const driver = await ensureStarted();
    const secretResult = await runBuddy(["secret"]);
    const secret = secretResult.stdout.trim();
    if (!secret) {
      return { ...text(`could not read secret: ${secretResult.stderr}`), isError: true };
    }

    await driver.get(OPTIONS_URL);
    const url = await driver.wait(until.elementLocated(By.id("url")), 10000);
    await url.clear();
    await url.sendKeys(WS_URL);
    const secretField = await driver.findElement(By.id("secret"));
    await secretField.clear();
    await secretField.sendKeys(secret);
    await driver.findElement(By.id("save")).click();

    // Give the extension a moment to open its WebSocket and update status.
    const statusEl = await driver.findElement(By.id("status"));
    await driver.sleep(1500);
    return json({
      ws_url: WS_URL,
      status_text: await statusEl.getText(),
      status_class: await statusEl.getAttribute("class"),
    });
  },
);

server.registerTool(
  "click",
  {
    description: "Click the first element matching a CSS selector.",
    inputSchema: { selector: z.string().describe("CSS selector") },
  },
  async ({ selector }) => {
    const driver = await ensureStarted();
    await driver.findElement(By.css(selector)).click();
    return text(`clicked ${selector}`);
  },
);

server.registerTool(
  "fill",
  {
    description: "Clear an input matching a CSS selector and type text into it.",
    inputSchema: {
      selector: z.string().describe("CSS selector for an input"),
      value: z.string().describe("Text to type"),
    },
  },
  async ({ selector, value }) => {
    const driver = await ensureStarted();
    const el = await driver.findElement(By.css(selector));
    await el.clear();
    await el.sendKeys(value);
    return text(`filled ${selector}`);
  },
);

server.registerTool(
  "get_text",
  {
    description: "Return the visible text of the first element matching a CSS selector.",
    inputSchema: { selector: z.string().describe("CSS selector") },
  },
  async ({ selector }) => {
    const driver = await ensureStarted();
    return text(await driver.findElement(By.css(selector)).getText());
  },
);

server.registerTool(
  "wait_for",
  {
    description: "Wait up to timeout_ms for an element matching a CSS selector to appear.",
    inputSchema: {
      selector: z.string().describe("CSS selector"),
      timeout_ms: z.number().int().positive().optional().describe("Default 10000"),
    },
  },
  async ({ selector, timeout_ms }) => {
    const driver = await ensureStarted();
    await driver.wait(until.elementLocated(By.css(selector)), timeout_ms ?? 10000);
    return text(`found ${selector}`);
  },
);

server.registerTool(
  "screenshot",
  {
    description: "Capture a PNG screenshot of the current page.",
    inputSchema: {},
  },
  async () => {
    const driver = await ensureStarted();
    const data = await driver.takeScreenshot();
    return { content: [{ type: "image", data, mimeType: "image/png" }] };
  },
);

server.registerTool(
  "evaluate",
  {
    description:
      "Run JavaScript in the current page and return the result as JSON. " +
      "The script body must `return` a value to read it back.",
    inputSchema: { script: z.string().describe("JS body, e.g. `return document.title`") },
  },
  async ({ script }) => {
    const driver = await ensureStarted();
    const result = await driver.executeScript(script);
    return json({ result: result ?? null });
  },
);

server.registerTool(
  "get_logs",
  {
    description:
      "Return buffered console messages and uncaught JS errors captured via WebDriver BiDi.",
    inputSchema: {},
  },
  async () => {
    await ensureStarted();
    return json(session.getLogs());
  },
);

server.registerTool(
  "clear_logs",
  { description: "Clear the buffered console/error log.", inputSchema: {} },
  async () => {
    session.clearLogs();
    return text("cleared logs");
  },
);

server.registerTool(
  "get_extension_uuid",
  { description: "Return the pinned moz-extension UUID.", inputSchema: {} },
  async () => text(EXTENSION_UUID),
);

server.registerTool(
  "buddy",
  {
    description:
      "Run the `buddy` CLI against the smoke data dir (e.g. args ['sessions'] or " +
      "['search', 'query']). Returns stdout, stderr, and exit code.",
    inputSchema: {
      args: z.array(z.string()).describe("CLI arguments, e.g. ['sessions']"),
    },
  },
  async ({ args }) => json(await runBuddy(args)),
);

async function shutdown() {
  await session.stop();
  stopBackend(backendState);
}
for (const sig of ["SIGINT", "SIGTERM"]) {
  process.on(sig, async () => {
    await shutdown();
    process.exit(0);
  });
}

// Keep stdin in flowing mode so the event loop stays alive even before the
// first message arrives. Without this, Node.js can exit prematurely when the
// MCP client spawns the server and there is a brief delay before the
// initialize handshake — causing a ConnectionClosed (-32000) error.
process.stdin.resume();
const transport = new StdioServerTransport();
await server.connect(transport);
