# M3b Slice A — Smoke Test Plan (Link graph & navigation provenance)

Manual, in-browser verification of the session link graph: outbound-link
catalogue, clicked links, navigation edges, and search-engine provenance.

**Plan/spec:** `plans/2026-07-30-m3b-slice-a-link-graph.md`,
`specs/2026-07-30-m3b-slice-a-link-graph-design.md`

## What is already automated (don't re-test manually)

The Task 13 protocol e2e drove a real `buddy serve` over a WebSocket and
verified the **backend** half end to end: acks for `session_start` /
`page_capture(links)` / `nav_edge` / `link_click` / `session_end`, an **error
frame** for `nav_edge` after `session_end` (no active session), and `buddy
links` showing the edge with `q="vectors"`, the un-followed link present, and
the clicked link absent from the un-followed set.

This plan therefore focuses on what the e2e cannot exercise: the **live browser
capture path** — the content-script outbound-link catalogue + click listener,
the background `webNavigation` edge emission, real search-engine provenance, and
the new-tab / reload / pause edge cases.

## Preconditions

1. From the repo root: `./scripts/smoke.sh --fresh --build`
   (`--fresh` wipes the isolated data dir + Firefox profile for a clean pairing;
   `--build` rebuilds the extension so the Slice A code is loaded.)
2. In the launched Firefox: open the BrowserBuddy popup → **⚙ Settings** → paste
   the printed **shared secret** → Save. The popup's `Backend:` line should show
   a connected status.
3. Open a **second terminal** for inspection (the isolated dir the script uses):

   ```bash
   cd /Users/david/Projects/BrowserBuddy/backend
   export BROWSERBUDDY_DATA_DIR=/Users/david/Projects/BrowserBuddy/.smoke/data
   ```

   Inspect with: `uv run buddy links "<session name>"`, `uv run buddy sessions`,
   `uv run buddy search "<text>"`.

Throughout: use ordinary, non-private windows (private windows are never
captured). Prefer a few well-known sites with obvious on-page links (e.g.
Wikipedia articles) and a real search engine.

---

## Test cases

Each case lists the **feature** it verifies and an explicit **PASS** criterion.

### TC1 — Baseline capture still works (regression)
- **Steps:** Start a session named `slice-a`. Visit 2 article pages. Stop.
- **Verify:** `uv run buddy sessions` shows `slice-a` **closed** (an end time,
  not `active`) with page_count ≥ 2; `uv run buddy search "<phrase you read>"`
  returns the page under `[slice-a]`.
- **PASS:** session closed + pages searchable (Slice 0 reconcile + M3a capture
  intact).

### TC2 — Outbound-link catalogue
- **Feature:** content-script `extractLinks` → `page_capture.links` →
  `outbound_links`.
- **Steps:** Start session `graph`. Open a link-rich article (e.g. a Wikipedia
  page). Do NOT click anything yet. Stop.
- **Verify:** `uv run buddy links graph` → under **notable un-followed links**,
  several of the page's links appear (with anchor text after `—`).
- **PASS:** the page's outbound links are listed as un-followed.

### TC3 — Clicked link leaves the un-followed set
- **Feature:** content-script click listener → `link_click` →
  `mark_link_clicked` (clicked=1).
- **Steps:** Start session `clicks`. Open a link-rich page. **Click** one link
  (left-click, same tab) and read the destination. Go back. Stop.
- **Verify:** `uv run buddy links clicks` → the clicked link's URL is **absent**
  from the un-followed list, while other links on that page remain.
- **PASS:** the clicked link is no longer "un-followed".

### TC4 — Navigation edges from on-page link follows
- **Feature:** background `webNavigation.onCommitted` → `nav_edge`
  (`transition: link`).
- **Steps:** Start session `nav`. On page A, click a link to page B, then a link
  on B to page C. Stop.
- **Verify:** `uv run buddy links nav` → under **navigation edges**, edges
  `A → B [link]` and `B → C [link]` appear with correct `from`/`to` URLs.
- **PASS:** the A→B→C path is recorded with a non-`(start)` origin for the second
  hop.

### TC5 — Search-engine provenance
- **Feature:** `searchProvenance` derives the query from the referring search
  URL (not DOM scraping).
- **Steps:** Start session `search-prov`. Go to Google (or DuckDuckGo), search
  for a distinctive phrase (e.g. `hierarchical navigable small worlds`), click a
  result. Stop.
- **Verify:** `uv run buddy links search-prov` → an edge from the search-results
  URL to the clicked result, annotated `q="hierarchical navigable small worlds"`
  (note: `+` in the URL is decoded to spaces).
- **PASS:** the edge shows the actual query string via `q="…"`.

### TC6 — New-tab open records an opener → target edge
- **Feature:** `webNavigation.onCreatedNavigationTarget` seeds the child tab's
  origin; the child's `onCommitted` emits the edge.
- **Steps:** Start session `newtab`. On page A, **middle-click** (or Ctrl/Cmd+
  click) a link to open page B in a new tab. Switch to the new tab. Stop.
- **Verify:** `uv run buddy links newtab` → an edge `A → B` exists (origin is
  A, not `(start)`).
- **PASS:** the new-tab navigation is attributed to its opener page.

### TC7 — Non-navigating / new-tab click is still captured
- **Feature:** the click listener catches clicks `webNavigation` can't attribute
  (middle-click, JS-handled, downloads).
- **Steps:** Start session `click-only`. On a page, middle-click a link (opens a
  background tab you don't switch to) OR click a link handled by JS that doesn't
  do a full navigation. Stop.
- **Verify:** `uv run buddy links click-only` → the clicked href is **absent**
  from the un-followed set (it was recorded as clicked) even though it may not
  have produced a normal same-tab navigation edge.
- **PASS:** the click is recorded independent of a nav edge.

### TC8 — Reload does not create a self-edge
- **Feature:** `navEdgeEvent` drops `from == to` (self/reload) edges.
- **Steps:** Start session `reload`. Open a page, press **Reload** twice. Stop.
- **Verify:** `uv run buddy links reload` → **no** `X → X` self-edge for the
  reloaded page.
- **PASS:** reloads produce no spurious edges.

### TC9 — Pause captures nothing
- **Feature:** all three streams gate on `status === "recording"`.
- **Steps:** Start session `pause-test`. Visit page A. **Pause** (badge `II`).
  While paused, visit page B and click a link on B. **Resume**, visit page C.
  Stop.
- **Verify:** `uv run buddy links pause-test` → no edges or links attributable to
  the paused browsing of B; A and C activity is present. `buddy sessions` page
  count excludes the paused page.
- **PASS:** nothing browsed while paused is captured.

### TC10 — Concurrent-tab provenance (mutex sanity)
- **Feature:** the serialized per-tab from-URL map survives rapid, interleaved
  navigations across tabs.
- **Steps:** Start session `concurrent`. Open 2–3 tabs and navigate them in
  quick succession (click links in each, alternating fast). Stop.
- **Verify:** `uv run buddy links concurrent` → each tab's edges have a correct
  `from` URL (belonging to that tab's prior page, not another tab's). No edges
  with an obviously wrong origin.
- **PASS:** origins are not cross-contaminated between tabs.

### TC11 — CLI selection & format
- **Feature:** `buddy links` session selection (by name, by id, default latest)
  and output layout.
- **Steps:** Run `uv run buddy links` (no arg), `uv run buddy links "<a name>"`,
  and `uv run buddy links <id>` (id from `buddy sessions`... use the leftmost
  session; if ids aren't shown, use a name).
- **Verify:** no-arg targets the newest session; name/id select the right one;
  output has a `Session:` header, a `-- navigation edges --` block, and a
  `-- notable un-followed links --` block (each shows `(none)` when empty).
- **PASS:** all three selectors resolve correctly and the format is as described.

---

## Results

Run: 2026-07-31, branch `m3b-slice-a-link-graph`, Firefox 153, extension rebuilt
from this branch. Driven **headful** and automated via the `browserbuddy-firefox`
MCP server (Selenium/geckodriver); pages: English Wikipedia + DuckDuckGo (TC5).

| # | Case | Result | Notes |
|---|------|--------|-------|
| TC1 | Baseline capture (regression) | ✅ PASS | `slice-a` closed, 2 pages; `search "neighbor"` returns the page. |
| TC2 | Outbound-link catalogue | ✅ PASS | 565 outbound links captured (incl. prose like `Machine_learning`). Digest surfacing was chrome-only at first (Finding 1) — since resolved: links are now flagged `in_main` and the digest shows article content. |
| TC3 | Clicked link leaves un-followed | ✅ PASS | Clicked `Machine_learning`; absent from the un-followed set afterward. |
| TC4 | Nav edges from link follows | ✅ PASS | `Vector_database → Machine_learning → Machine_Learning_(journal)`, both `[link]`. |
| TC5 | Search-engine provenance | ✅ PASS | DuckDuckGo → HNSW result; edge annotated `q="hierarchical navigable small worlds"`. |
| TC6 | New-tab opener → target edge | ✅ PASS | Cmd+click new tab → `Vector_database → Machine_learning` (origin is the opener, not `(start)`). |
| TC7 | Non-navigating click captured | ✅ PASS | Middle-click background tab; href recorded as clicked (absent from un-followed) with no same-tab nav edge. |
| TC8 | Reload no self-edge | ✅ PASS | Reloaded ×2; no `X → X` self-edge. |
| TC9 | Pause captures nothing | ✅ PASS | 2 pages captured (A + resumed page); the two paused pages excluded from the count. |
| TC10 | Concurrent-tab provenance | ✅ PASS | tab1 `Vector_database → Machine_learning`, tab2 `Cluster_analysis → Machine_learning`; each edge keeps its own tab's origin (no cross-contamination). |
| TC11 | CLI selection & format | ✅ PASS | No-arg targets newest; name/id select correctly; `Session:` / edges / un-followed sections present. |

### Findings (non-blocking)

1. **`notable un-followed links` surfaced boilerplate, not content — RESOLVED
   2026-08-05.** `Storage.list_unfollowed_links` used `ORDER BY p.visited_at,
   o.id LIMIT 50` over links inserted in DOM order, and Wikipedia (and most
   sites) emit sidebar / TOC / footer / interwiki chrome *before* article prose —
   so on a 565-link page the digest was 50 chrome links and none of the notable
   content links.
   **Fix:** the content script now flags each captured link `in_main` when it
   sits inside Readability's main-content region (vs. nav/header/footer/sidebar/
   ads); the digest filters to `in_main = 1`. The full catalogue is still stored
   (in_main=0 links retained for M4 ranking/analysis). Re-verified live: on the
   same page, 339/565 links flagged `in_main`, digest now leads with
   `Machine learning`, `Deep learning`, `embeddings`, `nearest neighbor search`,
   … and no nav/footer/interwiki chrome. (extension `extract.ts`; backend
   migration v4 + `list_unfollowed_links`.)

2. **Stale `from_url` across sessions (expected, per design note).** The first
   edge of a new session in a tab left open from a prior session inherits that
   tab's last URL as origin (e.g. `K-d_tree → Vector_database` when opening the
   `concurrent` session). Matches the documented "`from_url` may be stale" quirk.

## Teardown

Close Firefox (or Ctrl-C the smoke script) — the backend is torn down
automatically. The isolated `.smoke/` data + profile persist for the next run;
use `--fresh` to reset.

## Known limitations / expected quirks (not failures)

- **Bare-origin trailing slash:** a link to `https://y.com` is stored as
  `https://y.com/` (DOM URL normalization — real-browser behavior).
- **`from_url` may be `(start)`** for the first page of a session in a tab that
  was open before Start, or after a worker restart clears the in-memory tab-URL
  map — the edge still records, only the origin is unknown. This is by design.
- **Orphan clicks dropped:** a click on a page that was never captured (e.g.
  clicked before the ~500 ms settle capture fired) is dropped, not errored.
- **Provenance engine list is fixed** (Google/Bing/DuckDuckGo/Brave/Ecosia/
  Startpage/Yahoo/Yandex); other engines yield an edge without a `query`.
- **`web-ext lint`** still reports 3 known warnings (2× bundled-Readability
  `innerHTML`, 1× AMO `data_collection_permissions`) — not Slice A regressions.
