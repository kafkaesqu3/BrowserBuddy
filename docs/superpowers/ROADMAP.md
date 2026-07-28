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
| **M5+** Web dashboard | Spec build-order step 6; timeline, link-graph, session detail, chat-with-agent UI over the local backend | ⬜ later |

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
- Screenshots: dwell-gated `tabs.captureVisibleTab`, perceptual-hash deduped,
  stored in the blob store — needs the **REST blob endpoint** (M3a ships HTML
  inline in the WS event at a 2 MB cap; screenshots need out-of-band upload).

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
  archiving (MHTML), sidebar chat UI — all spec "Out of scope for v1".

---

## Where the detail lives

- Per-milestone plans: `docs/superpowers/plans/2026-07-*.md` — each has a
  "Follow-ups" section at the end capturing what its final review deferred.
- Spec sections: "Build order", "Roadmap (deferred, committed)", "Out of scope
  for v1".
- Git history on `main` is the record of what shipped; there is no separate
  changelog.
