# BrowserBuddy Roadmap

Single source of truth for milestone status and what's next. Consolidates
scope that was previously scattered across the spec's build order and the
per-milestone plan headers/follow-ups. Update this when a milestone merges.

**Design spec:** `docs/superpowers/specs/2026-07-14-browserbuddy-design.md`
**Process:** each milestone gets its own spec-check → `writing-plans` →
`subagent-driven-development` → smoke → `finishing-a-development-branch` cycle.
Plans live in `docs/superpowers/plans/`.

---

## Status

| Milestone | Scope | State |
|---|---|---|
| **M1** Backend foundation | FastAPI + SQLite schema v1, seq/ack WS ingest, shared-secret auth, `places.sqlite` importer, `buddy` CLI (serve/secret/import-history/search/tabs) | ✅ merged |
| **M2** Extension Tier 1 | Firefox MV3 extension: tabs + history sync, durable queue, WS client, pairing options page | ✅ merged |
| **M3a** Session capture core | Explicit sessions (popup start/pause/stop + badge), Readability text + raw-HTML capture, content-addressed gzip blob store, schema v2 (`sessions`/`page_visits` + FTS), `buddy sessions` + page search | ✅ merged |
| **M3b** Richer session capture | see below | ⬜ next candidate |
| **M4** Recall agent + digests | see below | ⬜ next candidate |
| **M5** Web dashboard | Session list, stats, link-graph explorer, session-detail view, chat-with-agent UI; local React SPA served by the backend | ⬜ later |
| **M6** Sidebar | In-browser sidebar: chat with the current page, archive the current tab on demand, show active session state | ⬜ later |

Two live extension surfaces so far: toolbar popup (session controls) and the
options page (pairing). Backend is `~/.browserbuddy/` (SQLite WAL + secret +
`blobs/`). Both test suites green at merge (extension 50, backend 75).

---

## M3b — Richer session capture

Completes the "research historian" capture surface. Everything here was
deliberately deferred from M3a (plan `2026-07-17-session-capture-core.md`
header + "Follow-ups for M3b").

**Being built in slices** (too large for one spec→plan cycle): `0 → (A link
graph, B dwell+SPA) → C screenshots`. See
`specs/2026-07-24-m3b-slice0-foundation-design.md`.

- ✅ **Slice 0 — Foundation & smoke debt** (merged): popup ⚙ Settings link
  (`openOptionsPage`), client-side dangling-session reconcile (`stop` clears
  local state only after `session_end` is durably enqueued), and EventQueue
  per-event storage keys + legacy migration. Plan
  `plans/2026-07-24-m3b-slice0-foundation.md`. Interactive smoke still owed.
  The REST blob endpoint + gzip-to-thread moved to Slice C (built with their
  only consumer, screenshots).

**New capture (Tier-2 completion):**
- Clicked links and per-page catalogued outbound links.
- Navigation edges (how page B was reached from page A) + search-engine
  provenance derived from `webNavigation`/referrer, **not** DOM scraping.
- Per-page dwell time (scroll/visibility/focus signals in the content script).
- SPA change-driven recapture: Navigation API `navigatesuccess` + decaying
  re-extract, resubmit only when the DOM changed (content.ts one-shot today).
- Screenshots / screen recording (Slice C): dwell-gated `tabs.captureVisibleTab`,
  perceptual-hash deduped, stored in the blob store — needs the **REST blob
  endpoint** (M3a ships HTML inline in the WS event at a 2 MB cap; images need
  out-of-band upload).  
  **Screen-recording trade-off:** the `MediaRecorder` + `getDisplayMedia` API
  can capture a tab as a video stream, which preserves scroll motion and
  interaction context that periodic screenshots miss. However: (a) it requires
  a user-gesture-triggered permission prompt per session and cannot run silently
  in the background the way `captureVisibleTab` can, (b) raw video is 10–100×
  larger on disk, (c) querying/indexing video for recall is significantly harder
  than perceptual-hash-deduped frames. **Recommended decision point at Slice C:**
  default to periodic screenshots (current plan) and offer recording as an
  explicit opt-in via the session-settings UI (a "record video" toggle in the
  popup), stored as a WebM blob in the blob store with a separate `recordings`
  table row. The two approaches are not mutually exclusive.

**Carried engineering items (now load-bearing at Tier-2 volume):**
- ✅ **EventQueue scaling** (Slice 0): moved from the whole-array rewrite to
  per-event `storage.local` keys (`bb:queue:index` + `bb:q:<seq>`), so a queued
  2 MB HTML body is no longer rewritten per enqueue/ack.
- **Backend `gzip.compress` on the event loop**: fine single-user today; move
  to a thread when the REST blob endpoint arrives (Slice C).
- **Active-time honesty** (Slice B): integrate the `idle` API so
  focused-but-away time doesn't count as active.
- ✅ **Popup pairing path** (Slice 0): added the "⚙ Settings" link
  (`browser.runtime.openOptionsPage()`).
- ✅ **Dangling backend session** (Slice 0, client side): `stop` now enqueues
  `session_end` durably before clearing local state. A session orphaned by
  uninstall mid-recording still needs M4 digests to tolerate open-ended
  sessions.

---

## M4 — Recall agent, MCP, and digests

The payoff milestone (spec build-order steps 4–5; both v1 success criteria).
Depends only on M3a data (sessions + `page_visits` FTS already exist), so it
can proceed before M3b.

- **Recall agent** (Pydantic AI): answers "what did I read about X last week?"
  with citations. Model-agnostic via any OpenAI-compatible endpoint
  (`base_url`/model/key configurable). Typed tools: `search_memory`,
  `search_history`, `get_open_tabs`, `list_sessions`, `get_session_digest`,
  `get_page`, `get_screenshots`.
- **Hybrid search / embeddings**: `sqlite-vec` vectors over `page_visits`
  (and history), fused with FTS5 — a *tracked* pipeline step with per-document
  completion state (never fire-and-forget; avoids Hister's index-drift).
  Embedding params from the spec: ~512-token chunks / 64 overlap, separate
  metadata embedding, title-prefixed body chunks, batched with retry.
- **MCP server** (FastMCP): exposes the same tools so Claude Code / any MCP
  client can query the memory. Tool descriptions embed live capability state
  (e.g. semantic-search availability).
- **Session digests**: on session end, generate a markdown digest (what was
  read, key findings, notable un-followed outbound links, open questions).
  Must tolerate open-ended sessions (see M3b dangling-session item).
- **CLI**: `buddy ask "..."`, `buddy digest <session>`.

**Recommendation:** M4 next — real captured sessions are already in the DB, so
the agent and digests deliver visible value immediately; M3b is incremental
capture polish and its link-graph/dwell data only accrues going forward.

---

## M5 — Web dashboard

Local React SPA served by the FastAPI backend (e.g. `GET /` → Vite-built
static bundle, or `buddy serve --ui`). No external hosting required; all data
stays local.

**Views:**

- **Sessions list** — sortable/filterable table; duration, page count, active
  time, status badge (active / paused / ended).
- **Session detail** — timeline of `page_visits` with dwell bars, thumbnails
  (if screenshots present), Readability text preview. Links to related sessions
  via the link graph.
- **Link graph explorer** — D3 (or similar) force-directed graph of
  `nav_edges`; nodes are page-visits coloured by session, edges weighted by
  click count. Filterable by session / time range / domain.
- **Stats / overview** — daily active time, pages per day, top domains, top
  outbound links, search-engine query extraction.
- **Chat UI** — calls the M4 recall agent via a thin `/api/chat` SSE endpoint;
  citation cards link to session-detail pages.

**Architecture notes:** The backend already has CORS-friendly FastAPI routers.
Add a `/ui` static-files mount and a `buddy serve --open` flag. The SPA talks
to the existing REST + SSE endpoints — no GraphQL layer needed at this scale.
Authentication reuses the shared-secret header.

**Dependencies on prior milestones:** M3b screenshots (thumbnails) and M4
recall agent (chat) are additive; the sessions list and link-graph ship before
them.

---

## M6 — In-browser sidebar

A Firefox sidebar panel (`sidebar_action` in the manifest) that is a
full-featured frontend to the BrowserBuddy backend, scoped to the current tab.
It communicates directly with the backend REST API (shared-secret auth,
same origin the popup already uses) — not through the background script.
This gives it access to everything the backend knows and everything it can do.

**Architecture:** The sidebar is a small React app (same build pipeline as M5
if M5 ships first, or standalone Vite bundle otherwise). It holds its own HTTP
client pointed at `http://localhost:<port>` with the shared secret, and
subscribes to SSE streams for live updates. It does NOT open a second WS
connection — passive session state comes from the backend REST API, not the
ingest channel.

**Current-tab page tools:**
- **Outbound link analysis** — on demand, POST the current page URL (or its
  already-stored `page_visit` ID) to a new `/api/pages/{id}/links/analyze`
  endpoint; the backend fetches the stored link catalogue (M3b) and returns
  ranked/categorised outbound links (by domain, novelty relative to session
  history, or recall relevance via the M4 agent). Results render as a
  clickable, filterable list in the sidebar — double-clicking a link can open
  it in a new tab and optionally archive it immediately.
- **Archive / capture on demand** — POST to `/api/pages/capture` with the
  current URL; the backend fetches + stores the page even without an active
  session. Returns the `page_visit` ID so the sidebar can link to the M5
  session-detail view.
- **Chat with this page** — sends the current page's Readability extract to
  the M4 recall agent via `/api/chat` with a page-scoped system prompt.
  Streaming SSE response. The agent can answer questions, summarise, extract
  entities, or explain the page using the full memory index as context.

**Session controls:**
- Shows active session name, elapsed time, page count, and a stop/pause button
  (same controls as the popup, but persistent — the popup closes on blur).
- "Add this page to session" button for pages visited outside the session
  recording window.

**Backend interaction surface (new API endpoints needed):**

| Endpoint | Purpose |
|---|---|
| `GET /api/pages/{id}/links` | Retrieve stored outbound link catalogue |
| `POST /api/pages/{id}/links/analyze` | Rank/categorise links via agent or heuristic |
| `POST /api/pages/capture` | On-demand capture outside session flow |
| `GET /api/sessions/active` | Current session state for live display |
| `POST /api/chat` (SSE) | Already planned for M4/M5; reused here |

The sidebar can grow additional backend-backed tools (e.g. "find related pages
in my history", "what did I previously read from this domain") without
extension changes — new backend endpoints surface automatically as new sidebar
panels.

**Implementation notes:** On-demand capture requires injecting the content
script into the active tab to extract Readability text; this is the one new
extension code path. Everything else is sidebar ↔ backend HTTP.

**Dependencies:** M3a (capture infrastructure) is sufficient for archive +
link catalogue; M3b (link graph) unlocks link analysis; M4 (recall agent)
is required for chat and relevance ranking.

---

## Cross-cutting deferred items (not a milestone; fold in where noted)

- **Sensitive-content firewall at ingest** (spec Roadmap): regex-reject secrets
  (private keys, cloud creds, API tokens) before storage. User-deferred
  2026-07-17; **required before the tool leaves single-user use.**
- **Chrome port**: the seams exist (per-target manifests, pluggable history
  importer). Remaining: `globalThis.browser ??= chrome` alias in `options.ts`,
  `popup.ts`, and `content.ts` (background.ts already has it); verify
  `optional_host_permissions`/`scripting` behavior on Chrome MV3.
- **AMO submission**: add `browser_specific_settings.gecko.data_collection_permissions`
  (the one standing `web-ext lint` warning; becomes mandatory for AMO upload).
- **At-rest encryption**, cross-session synthesis, faithful visual page
  archiving (MHTML) — all spec "Out of scope for v1". Sidebar chat UI
  promoted to M6.

---

## Where the detail lives

- Per-milestone plans: `docs/superpowers/plans/2026-07-*.md` — each has a
  "Follow-ups" section at the end capturing what its final review deferred.
- Spec sections: "Build order", "Roadmap (deferred, committed)", "Out of scope
  for v1".
- Git history on `main` is the record of what shipped; there is no separate
  changelog.
