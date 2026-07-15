# BrowserBuddy — Design Spec

**Date:** 2026-07-14
**Status:** Approved

## What it is

A Firefox browser extension paired with a local Python backend that acts as a
personal research historian. During explicit, user-started research sessions it
captures what you read (extracted text + periodic screenshots), the links you
click, the outbound links on pages, navigation paths, and per-page dwell time.
Outside of sessions it maintains ambient awareness of open tabs and browser
history (metadata only). A local agent answers recall questions ("what did I
read about X last week?"), writes post-session digests, and exposes the whole
memory to other agents via MCP.

## Decisions made during brainstorming

| Decision | Choice |
|---|---|
| AI locality | Model-agnostic: any OpenAI-compatible endpoint (OpenRouter, LiteLLM, Ollama, etc.) |
| Capture depth | Extracted text + periodic screenshots |
| Capture scope | Session-based recording (explicit start/stop, recording indicator) + always-on tab/history metadata |
| Interaction | Web dashboard + CLI + MCP server (no sidebar) |
| Backend stack | Python, FastAPI, Pydantic AI |
| v1 priorities | 1) Recall Q&A with citations, 2) Post-session digests |
| Architecture | Approach A: thin extension, smart backend |

## Prior-art findings (2026-07)

No existing project combines: explicit research sessions, browser-semantic
capture (text + screenshots + link graph + dwell), local-first storage,
model-agnostic LLM, recall Q&A + digests, MCP access, and Firefox. Closest
neighbors: Hister (capture→index→MCP, no sessions/agent), screenpipe (OS-level
recall+MCP, no web semantics), Pieces (proprietary workstream memory via MCP),
Promnesia (Firefox+local Python link provenance, no AI), Perplexity
Comet/ChatGPT Atlas (cloud recall silos). Key lessons: Rewind/Limitless was
acquired and remotely bricked (argument for local-first, open formats); Memex
retreated from index-everything (session-scoping fixes that failure mode);
Microsoft Recall's launch disaster shows consent architecture is everything —
explicit sessions is the cleanest consent story and is untried.

Reusable patterns: Readability.js (in-extension extraction), trafilatura
(Python fallback), SQLite FTS5 + sqlite-vec + sentence-transformers,
eyalzh/browser-control-mcp's extension↔localhost security handshake,
ArchiveBox's multi-artifact storage philosophy, `places.sqlite` read-only
backfill.

## Two-tier capture model

### Tier 1 — Ambient awareness (always on, metadata only)

- All open tabs: URL, title, window, tab group, Firefox container, when
  opened, last activated, cumulative active time.
- Browser history: ongoing sync via `browser.history` API; one-time backfill by
  reading the Firefox profile's `places.sqlite` (read-only copy) from the
  backend. The backfill importer is a pluggable per-browser module (Firefox
  `places.sqlite` now; Chrome's `History` SQLite file later, same interface).
- No page content, no screenshots. Same sensitivity level as what Firefox
  already stores.
- Private windows are never captured in either tier.

### Tier 2 — Research sessions (explicit start/stop, deep capture)

Started/stopped from the toolbar popup with a session name; visible recording
indicator; pausable. While active:

- Full Readability.js text extraction of every page read (fallback: raw text).
- Dwell-gated screenshots via `tabs.captureVisibleTab`: after ~5s on a page and
  after major scrolls; JPEG; perceptual-hash deduped.
- Every link clicked; all outbound links catalogued per page.
- Navigation edges (how page B was reached from page A).
- Per-page dwell time from scroll/visibility/focus signals.

## Components

### Firefox WebExtension (TypeScript, thin by design)

- **Background script** — session state machine, tab/history telemetry, event
  batching, WebSocket client to the backend. Written as an **ephemeral,
  stateless event handler**: all session state (active session, pause state,
  per-tab dwell accounting) lives in `storage.session` and is rehydrated on
  wake; timers use the `alarms` API; WebSocket heartbeat every ~20s. Event
  batches carry **sequence numbers and acks** so worker death or reconnect
  can't lose or duplicate events. When the backend is unreachable, events
  buffer in `storage.local` and retry with backoff; a warning badge is shown.
  Session data is never lost to a dead backend.
- **Content script** — injected only during active sessions: Readability.js
  extraction, click/outbound-link capture, scroll/visibility signals.
- **Toolbar popup** — start/stop/pause session with a name, recording
  indicator, backend connection status.

### Local backend (Python 3.12+, FastAPI + Pydantic AI)

- **Ingest layer** — WebSocket for the event stream, REST for screenshot
  blobs. Bound to `127.0.0.1` only. Shared-secret auth generated at first
  pairing (pattern from eyalzh/browser-control-mcp); the secret, not the
  extension origin, is the auth (accepts both `moz-extension://` and
  `chrome-extension://` origins). Versioned event schema with sequence
  numbers and acks (idempotent, at-least-once delivery) so extension and
  backend evolve independently and reconnects are safe.
- **Storage** — single SQLite DB (WAL mode): `sessions`, `page_visits`,
  `nav_edges`, `outbound_links`, `tab_snapshots`, `history_entries`. FTS5 for
  full text; `sqlite-vec` for embeddings (one file, no second DB). Screenshots
  as content-addressed JPEGs on disk. Digests as markdown files. All open
  formats.
- **Enrichment pipeline** — background workers, never blocking ingest: chunk +
  embed page text (local sentence-transformers), OCR screenshots, generate the
  session digest on session end.
- **Agent layer** — one Pydantic AI agent; model via configurable
  OpenAI-compatible `base_url`/model/key. Typed tools:
  - `search_memory` — hybrid FTS5 + vector search with citations (URL + session)
  - `search_history` — Tier-1 history search
  - `get_open_tabs` — current tab inventory with open durations
  - `list_sessions`, `get_session_digest`, `get_page`, `get_screenshots`
- **MCP server** — FastMCP exposing the same tools, so Claude Code or any MCP
  client can query the memory.
- **CLI** — `buddy ask "..."`, `buddy sessions`, `buddy search "..."`.
- **Web dashboard** — served by the backend: session timeline, session detail
  with link graph, hybrid search, chat with the recall agent.

## Portability policy (Firefox first, Chrome cheap later)

**Priority rule: when portability and Firefox capture fidelity conflict,
Firefox wins.** Portability is achieved by layering, not by lowest common
denominator.

**Portable core** — everything BrowserBuddy *requires* uses APIs identical in
both browsers (`tabs`, `history`, `webNavigation`, `tabs.captureVisibleTab`,
`storage`, `scripting`, `alarms`, `tabGroups`), promise-based `browser.*` via
`webextension-polyfill`:

- **MV3 only.** One codebase; a small build step generates per-target
  manifests (`background.scripts` event page for Firefox vs
  `background.service_worker` for Chrome; `browser_specific_settings`
  stripped for Chrome). Note MV2 + persistent background page was considered
  and rejected: it would simplify state handling but bets the architecture on
  MV2's lifespan and adds no collection capability.
- **Ephemeral background.** Not a Chrome concession — Firefox MV3 event pages
  are also suspended after ~30s idle. The background script assumes it can
  die at any moment: state in `storage.session`, `alarms` instead of timers,
  sequence-numbered/acked event batches (protects against Firefox event-page
  suspension too), WebSocket heartbeat < 30s.
- **No DOM or compute in the background.** Costs Firefox nothing today: all
  DOM work happens in content scripts (Readability); screenshot hashing/dedup
  happens in the backend — the extension ships raw JPEGs over localhost. If a
  future capture need requires background DOM (Firefox event pages have it;
  Chrome workers don't), Firefox wins and Chrome uses an offscreen document.
- **Backend seams.** Auth by shared secret, not extension origin; pluggable
  history importer.

**Firefox enhancement layer** — Firefox-only APIs are used freely for capture
richness, feature-detected, carried as optional fields in the event schema (a
Chrome port leaves them null):

- **Container identity** (`contextualIdentities`): tab snapshots and session
  page visits record which Firefox container they belong to — valuable
  research-context metadata with no Chrome equivalent.
- Any future Firefox-only capture API goes in this layer rather than being
  rejected for portability.

## Data flow

Session start → content scripts activate → page/click/dwell/screenshot events
stream to the backend → pipeline embeds/OCRs asynchronously → session end →
digest generated → queryable via dashboard, CLI, or MCP. Tier-1 tab/history
sync runs continuously regardless of sessions. Capture never depends on the
LLM: if the endpoint is down, recording continues and digests queue.

## Session digest contents

Generated on session end, stored as markdown, regenerable:

- What was read (pages, time spent, in reading order)
- Key findings/claims per major page
- Notable outbound links seen but not followed
- Open questions / loose threads

## Error handling

| Failure | Behavior |
|---|---|
| Backend down | Extension buffers events locally, warning badge, retry with backoff |
| Screenshot capture fails | Log and continue — text is the primary record |
| LLM endpoint down | Capture unaffected; digest generation queues and retries |
| Readability fails on a page | Fall back to raw text extraction |
| Malformed/oversized events | Reject with versioned schema validation; log |

## Privacy & threat model

- Everything local: `127.0.0.1`-bound backend, shared-secret auth, local disk.
- Explicit sessions are the consent mechanism for deep capture; Tier 1 is
  metadata-only.
- Private windows never captured.
- Data at rest is plaintext SQLite on the user's own disk — same trust level
  as Firefox's own `places.sqlite`. At-rest encryption is explicitly out of
  scope for v1.
- Nothing leaves the machine except LLM calls, whose destination the user
  fully controls (can be a local model).

## Testing

- **Backend:** pytest unit tests for the enrichment pipeline and hybrid
  search; agent tools tested with Pydantic AI's `TestModel` (no live LLM);
  contract tests for the versioned ingest protocol.
- **Extension:** capture/session logic unit-tested with mocked WebExtension
  APIs (vitest); manual smoke testing via `web-ext run`.

## Build order

1. Backend skeleton: FastAPI app, SQLite schema, ingest protocol
2. Extension Tier 1 (tabs/history sync) + `places.sqlite` backfill —
   immediately useful history/tab search via CLI
3. Extension Tier 2 session capture: text, links, dwell first; screenshots last
4. Recall agent + hybrid search + CLI + MCP server ← v1 success criterion #1
5. Session digests ← v1 success criterion #2
6. Web dashboard

## Out of scope for v1

- At-rest encryption
- Chrome/other-browser ports (the Chrome portability constraints above keep
  this to a build-config change + history importer later)
- Cross-session synthesis and research-trail visualization (post-v1)
- Full HTML/MHTML page archiving
- Sidebar chat UI
