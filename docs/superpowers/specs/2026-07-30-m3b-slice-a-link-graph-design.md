# M3b Slice A — Link graph & navigation provenance

**Parent spec:** `docs/superpowers/specs/2026-07-14-browserbuddy-design.md`
**Roadmap:** `docs/superpowers/ROADMAP.md` (M3b, slice `0 → (A, B) → C`)
**Date:** 2026-07-30

Slice A adds the "how you moved through the web" layer to session capture:
every page's outbound links, which of them you clicked, and the navigation
edges (how page B was reached from page A) including search-engine provenance.
It is independent of the other M3b slices and completes two Tier-2 spec items:
"Every link clicked; all outbound links catalogued per page" and "Navigation
edges … including search-engine provenance … derived from
`webNavigation`/referrer data — not DOM scraping."

Three capture streams feed two new tables:
- **outbound-link catalogue** (content script) — every `<a href>` on a captured page.
- **click listener** (content script) — actual anchor clicks, including
  non-navigating ones (downloads, JS handlers, new-tab opens) that
  `webNavigation` cannot attribute.
- **navigation edges** (background `webNavigation`) — the real navigation graph
  with transition type and search-query provenance.

---

## 1. Data model — schema v3 (new migration, additive)

```sql
CREATE TABLE nav_edges (
    id INTEGER PRIMARY KEY,
    session_id INTEGER NOT NULL REFERENCES sessions (id),
    from_url TEXT,                         -- null when unknown (fresh tab / lost tracking)
    to_url TEXT NOT NULL,
    transition TEXT NOT NULL DEFAULT '',   -- webNavigation transitionType
    qualifiers TEXT NOT NULL DEFAULT '',   -- comma-joined transitionQualifiers
    query TEXT,                            -- search query when from_url is a known engine
    occurred_at INTEGER NOT NULL,
    UNIQUE (session_id, from_url, to_url, occurred_at)
);
CREATE INDEX idx_nav_edges_session ON nav_edges (session_id);

CREATE TABLE outbound_links (
    id INTEGER PRIMARY KEY,
    page_visit_id INTEGER NOT NULL REFERENCES page_visits (id),
    href TEXT NOT NULL,
    anchor_text TEXT NOT NULL DEFAULT '',
    rel TEXT NOT NULL DEFAULT '',
    clicked INTEGER NOT NULL DEFAULT 0,
    UNIQUE (page_visit_id, href)
);
CREATE INDEX idx_outbound_links_page ON outbound_links (page_visit_id);
```

`nav_edges` denormalizes URLs (no FK to `page_visits`) so the graph survives for
pages navigated *through* but not captured as "reads". `outbound_links` FK to
the `page_visit` they were seen on; `clicked = 0` is the "notable un-followed
link" set the M4 digest wants.

## 2. Protocol (additive; stays v1)

New backends accept the events; older backends error-frame unknown types and
clients skip them (existing forward-compat rule).

- **Extend `PageCaptureEvent`** with `links: list[OutboundLink] = []`, where
  `OutboundLink = { href: str, text: str = "", rel: str = "" }`. The content
  script catalogues the page's links and ships them with the capture (they
  belong to that snapshot). Capped at 1000 links/page; anchor text ≤ 300 chars.
- **New `LinkClickEvent`** — `{ type: "link_click", page_url, href, text = "",
  clicked_at }`.
- **New `NavEdgeEvent`** — `{ type: "nav_edge", from_url: str | None, to_url,
  transition = "", qualifiers = "", query: str | None, occurred_at }`.

All three require an active session; with none, the backend raises `LookupError`
→ error frame (same path as `page_capture`), connection survives.

## 3. Extension capture

- **`content.ts`** — after extraction:
  - collect `document.links` → absolute href, trimmed anchor text, `rel`; dedup
    by href; cap; include in the `bb:page` message.
  - add a capture-phase `click` **and** `auxclick` listener; for a click landing
    on (or inside) an `http(s)` anchor, send `bb:click { page_url, href, text }`.
    This is the stream that catches downloads, JS-handled clicks, and new-tab
    opens `webNavigation` alone can't attribute.
- **`background.ts`** — register `webNavigation.onCommitted` (top frame,
  `frameId === 0`) and `onCreatedNavigationTarget` (new-tab/window opens).
  Maintain a per-tab last-committed-URL map in `storage.session` so `from_url`
  is known. On each navigation build a `nav_edge` (from = tab's previous URL,
  to = `details.url`, transition/qualifiers from the event, `query` from search
  provenance) and **enqueue only while recording** — but track last-URL
  *always*, so the first edge after Start still has a correct `from_url`.
  Handle `bb:click` → `LinkClickEvent` (recording only).
- **`searchProvenance.ts`** — pure, testable, never throws: host → query-param
  map (Google/Bing/DuckDuckGo/Brave/Ecosia/Startpage `q`, Yahoo `p`, Yandex
  `text`); returns the query string or null.
- **`capture.ts` / `types.ts`** — add `OutboundLink`, extend `PageCapture.links`,
  add `LinkClick` and `NavEdge` to `EventPayload`; conversion helpers apply the
  caps.
- **Manifest** — add `"webNavigation"` to both overlays' `permissions`.

## 4. Backend, CLI, testing

- **`models.py`** — `OutboundLink`; extend `PageCaptureEvent.links`;
  `LinkClickEvent`; `NavEdgeEvent`; widen the envelope union.
- **`storage.py`**
  - `add_page_visit` returns the page_visit **id** (the existing id on a
    duplicate `INSERT OR IGNORE`) so links can attach.
  - `add_outbound_links(page_visit_id, links)` — bulk insert-or-ignore.
  - `add_nav_edge(session_id, *, from_url, to_url, transition, qualifiers,
    query, occurred_at)`.
  - `mark_link_clicked(session_id, page_url, href, text)` — find the latest
    `page_visit` for `(session_id, page_url)`, upsert its `outbound_link` with
    `clicked = 1` (insert the row if the link wasn't in the catalogue, e.g. a
    dynamically added link). Orphan click (no matching page_visit) → dropped and
    logged.
  - reads: `list_nav_edges(session_id)`, `list_unfollowed_links(session_id)`.
- **`app.py`** — dispatch `NavEdgeEvent` and `LinkClickEvent`, plus
  outbound-links insertion inside the `PageCaptureEvent` branch; all gated on an
  active session (else `LookupError` → error frame).
- **CLI `buddy links <session>`** — session by id or name (default: latest).
  Prints nav edges (`from → to  [transition]  q="…"`) and notable un-followed
  outbound links.
- **Testing** — backend TDD throughout (v3 migration keeps v1/v2 data intact;
  storage; models; app dispatch for both new events + outbound links; CLI).
  Extension TDD for the pure units (link extraction, click-event conversion,
  **search provenance**, capture conversion, nav-edge building); background
  wiring + `webNavigation` verified by build + the interactive smoke.

## Error handling

- Link/URL/query parsing never throws (guarded; a malformed href is skipped).
- `from_url` null is tolerated end to end.
- Orphan clicks (page not captured) are dropped and logged, not errors.
- Caps (1000 links/page, 300-char anchor text) bound event size under the WS
  frame limit alongside the existing 2 MB HTML.
- Unknown session → error frame (existing pattern), connection survives.

## Out of scope for Slice A

Dwell/idle/SPA recapture and screenshots (Slices B/C); embeddings, digests, and
the recall agent that will *consume* this graph (M4); the REST blob endpoint
(Slice C). No DOM scraping of search-result pages — provenance is derived from
`webNavigation`/referrer only.
