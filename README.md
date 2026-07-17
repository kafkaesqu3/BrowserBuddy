# BrowserBuddy

A Firefox extension + local backend that acts as a personal research
historian: explicit research sessions, browsing memory, and an agent that
can answer "what did I read about X last week?"

Design spec: `docs/superpowers/specs/2026-07-14-browserbuddy-design.md`

## Backend quick start

```bash
cd backend
uv sync
uv run buddy import-history   # backfill Firefox history
uv run buddy search "quantum" # search it
uv run buddy serve            # start the ingest server (127.0.0.1:8765)
uv run buddy secret           # secret to paste into the extension
```

Data lives in `~/.browserbuddy/` (SQLite + secret). Override with
`BROWSERBUDDY_DATA_DIR`.

## Extension (Firefox) quick start

```bash
cd extension
npm install
npm run build          # emits dist/firefox and dist/chrome
npm run smoke          # web-ext: launches Firefox with the extension loaded
```

Pair it: run `buddy secret`, paste into the extension's options page
(about:addons → BrowserBuddy → Preferences), Save. The extension then
continuously syncs open tabs and history to the backend (`buddy tabs`,
`buddy search ...`). Offline-safe: events buffer in extension storage and
replay on reconnect.
