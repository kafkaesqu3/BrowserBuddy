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

### Research sessions

Click the BrowserBuddy toolbar icon → name your session → **Start session**
(Firefox will ask for page access — that grant is what turns capture on).
Pages you read are captured (readable text + raw HTML) until you **Stop**.
Review from the terminal: `buddy sessions`, `buddy search "<anything you read>"`.

Inspect the link graph of a session: `buddy links "<session name>"` — shows how
you moved between pages (with the search query that led you there) and the
notable links you saw but didn't follow.

Pages you dwell on show up in `buddy search`'s results too (`dwell=42s`) —
a crude, focus-gated figure from the same tab-active-time clock Tier-1
already tracks, not a precise reading-time measurement.

During a session, the popup's "Capture screenshots" toggle takes a
dwell-gated viewport screenshot on a configurable interval (default 30s,
adjustable live while recording), and the "📷 Capture now" button takes an
on-demand full-page capture via scroll-and-stitch. `buddy screenshots
<session>` lists what was captured; visually near-duplicate frames (a mostly
static page) are deduped server-side and never stored.

### Recall, memory tools, and digests (M4)

The backend now exposes typed memory retrieval, cited recall answers, a read-only stdio MCP
server, and queued session digests. New commands require `buddy serve`; existing `search`,
`sessions`, `links`, and other local commands still work without it.

See [backend setup and limitations](backend/README.md) for model configuration, optional
semantic search, job recovery, and MCP connection instructions. Automatic digests are off
by default. Screenshots are exposed as metadata; OCR and image understanding are not included.

M4 integration is tested with deterministic model boundaries. The real embedding-model
smoke check remains pending because the development environment blocked the model download.
