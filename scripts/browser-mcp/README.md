# BrowserBuddy Firefox smoke-test MCP server

An MCP server that drives **Firefox with the BrowserBuddy extension loaded** so
Claude can run the smoke test autonomously: launch the browser, pair it with the
backend, start a research session, browse/click to populate the capture queue,
stop, then inspect the backend and read back any console/JS errors.

It targets Firefox specifically (the browser BrowserBuddy is developed against)
via `selenium-webdriver` + geckodriver. Chrome is intentionally out of scope.

## How it works

- **Launch + extension**: geckodriver starts Firefox and installs the unpacked
  `extension/dist/firefox` as a *temporary* (unsigned) add-on.
- **Deterministic URLs**: the extension's moz-extension UUID is pinned via the
  `extensions.webextensions.uuids` pref, so the popup/options page URLs are known
  up front (no `about:debugging` scraping).
- **Navigation to extension pages**: recent Firefox blocks WebDriver navigation
  to privileged URLs; geckodriver's `--allow-system-access` flag lifts that.
- **Auto-grant permissions**: the popup requests the `<all_urls>` optional host
  permission on session start; `extensions.webextOptionalPermissionPrompts=false`
  auto-grants it so automation isn't blocked on a doorhanger.
- **Error capture**: console messages and uncaught JS exceptions are buffered via
  WebDriver BiDi (`get_logs` / `clear_logs`).
- **Isolated backend**: on first use the server starts `buddy serve` on a
  dedicated port (default **8799**) against the smoke data dir
  (`.smoke/data`), so it never collides with a dev backend on 8765 and the
  paired secret always matches. A backend already listening on 8799 is reused;
  one the server started is torn down on shutdown.

## Prerequisites

- Node ≥ 20.11, Firefox, and `uv` (for the backend) on `PATH`.
- Built extension: `cd extension && npm run build` (produces `dist/firefox`).
- `npm install` in this directory (installs Selenium + the MCP SDK; geckodriver
  is fetched automatically by Selenium Manager on first launch).

## Register with Claude Code

Add to `.mcp.json` at the repo root:

```json
{
  "mcpServers": {
    "browserbuddy-firefox": {
      "command": "node",
      "args": ["index.mjs"],
      "cwd": "/Users/david/Projects/BrowserBuddy/scripts/browser-mcp"
    }
  }
}
```

## Tools

| Tool | Purpose |
| --- | --- |
| `start_browser` | Launch Firefox + extension and ensure the backend is up (idempotent; other tools call it automatically). |
| `pair` | Read the secret via `buddy secret`, fill the options page, save, report connection status. |
| `open_popup` / `open_options` | Open the extension popup / options page. |
| `navigate` | Navigate the current tab to a URL. |
| `click` / `fill` / `get_text` / `wait_for` | Interact with page elements by CSS selector. |
| `screenshot` | PNG screenshot of the current page. |
| `evaluate` | Run JS in the page and return the result (the body must `return`). |
| `get_logs` / `clear_logs` | Read/clear buffered console + JS-error output. |
| `get_extension_uuid` | The pinned moz-extension UUID. |
| `buddy` | Run the `buddy` CLI against the smoke data dir (e.g. `["sessions"]`). |

## Environment overrides

- `BROWSERBUDDY_HEADLESS=1` — run Firefox headless (default: headful).
- `BROWSERBUDDY_PORT` / `BROWSERBUDDY_HOST` — backend host/port (default `127.0.0.1:8799`).

## Example smoke run

Ask Claude:

> Use browserbuddy-firefox to pair, start a session named "smoke-test", navigate
> to a couple of pages, stop the session, then run `buddy sessions` and show me
> the output and any extension logs.

Expected: the session appears in `buddy sessions` with the pages it captured, and
`get_logs` shows no errors.
