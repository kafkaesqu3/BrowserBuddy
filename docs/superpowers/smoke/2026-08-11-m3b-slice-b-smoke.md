# M3b Slice B — Smoke Test Plan (SPA recapture & minimal dwell)

Manual, in-browser verification of change-driven SPA recapture and per-page
dwell time. Written to be run by an agent with Firefox + the
`browserbuddy-firefox` smoke-test MCP server available (same tooling Slice
A's smoke run used), or by a human following the same steps manually.

**Plan/spec:** `plans/2026-08-11-m3b-slice-b-spa-recapture.md`,
`specs/2026-08-11-m3b-slice-b-spa-recapture-design.md`

## What is already automated (don't re-test manually)

The plan's Task 11 e2e drove a real `buddy serve` over a WebSocket and
verified the **protocol** half end to end: acks for `session_start` /
`page_capture` / two additive `page_dwell` flushes (3000ms + 1500ms →
summed to 4500ms), an **error frame** for `page_dwell` after `session_end`
(no active session, connection survives), and `buddy search` showing
`dwell=4s`. Backend suite (105 tests) and extension suite (80 tests) are
green.

This plan therefore focuses on what that e2e cannot exercise: the **live
browser behavior** — the content script actually detecting and re-sending
SPA content changes (Navigation API vs. the decaying poll, and the
noise-filtering threshold), and the Tier-1 tab-active-time clock actually
being focus-gated and flushed correctly at real page/tab/session
boundaries.

## Preconditions

1. From the repo root: `./scripts/smoke.sh --fresh --build`
   (`--fresh` wipes the isolated data dir + Firefox profile for a clean
   pairing; `--build` rebuilds the extension so the Slice B code is loaded.)
2. In the launched Firefox: open the BrowserBuddy popup → **⚙ Settings** →
   paste the printed **shared secret** → Save. The popup's `Backend:` line
   should show a connected status.
3. Open a **second terminal** for inspection (the isolated dir the script
   uses):

   ```bash
   cd /Users/david/Projects/BrowserBuddy/backend
   export BROWSERBUDDY_DATA_DIR=/Users/david/Projects/BrowserBuddy/.smoke/data
   ```

   Inspect with: `uv run buddy search "<text>"`, `uv run buddy sessions`,
   `uv run buddy links "<session name>"`.
4. **Build the SPA test fixture.** Several test cases below need a page you
   can mutate on demand without a full reload — real-world SPA sites are
   unpredictable about exactly how much text changes and whether they use
   the Navigation API, which makes them unsuitable for verifying the
   *threshold* and *decay* behavior precisely. Save this file (e.g. as
   `~/spa-fixture.html`):

   ```html
   <!doctype html>
   <html>
   <head><title>BrowserBuddy Slice B SPA fixture</title></head>
   <body>
     <h1>SPA recapture fixture</h1>
     <div id="content"></div>
     <div id="status"></div>
     <button id="btn-noise">Small change (must NOT recapture)</button>
     <button id="btn-poll">Silent big change, no navigation (poll must catch it)</button>
     <button id="btn-nav">Big change + pushState (navigatesuccess must catch it fast)</button>
     <script>
       // Content only ever grows, so every button's delta is safely on one
       // side of the threshold regardless of click order: noise adds ~150
       // chars (always under max(0.2*len, 200) once len >= 750, which it
       // always is here); poll/nav each add ~500 chars (always over it).
       let content = 'INITIAL-MARKER-ALPHA ' +
         'lorem ipsum dolor sit amet consectetur adipiscing elit sed do '.repeat(15); // ~1000 chars
       const contentEl = document.getElementById('content');
       const statusEl = document.getElementById('status');
       const render = (label) => {
         contentEl.innerHTML = '<p>' + content + '</p>';
         statusEl.textContent = label + ' — length now ' + content.length;
         console.log('[fixture]', label, content.length, 'chars');
       };
       render('initial load');
       document.getElementById('btn-noise').onclick = () => {
         content += ' NOISE-MARKER-DELTA ' + 'x'.repeat(130); // +150 chars
         render('noise click');
       };
       document.getElementById('btn-poll').onclick = () => {
         content += ' POLL-MARKER-BETA ' + 'y'.repeat(480); // +500 chars, no navigation call
         render('poll click (no pushState)');
       };
       document.getElementById('btn-nav').onclick = () => {
         content += ' NAV-MARKER-GAMMA ' + 'z'.repeat(480); // +500 chars
         history.pushState({}, '', '/page' + Date.now()); // should fire navigation.navigatesuccess
         render('nav click (pushState)');
       };
     </script>
   </body>
   </html>
   ```

   Serve it locally (content scripts don't reliably run the same way on
   `file://`): `python3 -m http.server 8901 --directory ~` from the
   directory containing the file, then the fixture is at
   `http://localhost:8901/spa-fixture.html`.

Throughout: use ordinary, non-private windows. **Start the session before
navigating to the fixture** (dynamically-registered content scripts apply
to future loads, not already-open tabs — same precondition Slice A's smoke
plan used).

---

## Test cases

Each case lists the **feature** it verifies and an explicit **PASS**
criterion.

### TC1 — Baseline capture still works (regression)

- **Steps:** Start a session named `slice-b`. Visit 2 ordinary article
  pages (e.g. Wikipedia). Stop.
- **Verify:** `uv run buddy sessions` shows `slice-b` **closed** with
  page_count ≥ 2; `uv run buddy search "<phrase you read>"` finds a page.
- **PASS:** session closed + pages searchable (M3a/Slice 0 capture intact).

### TC2 — Noise below the threshold is NOT recaptured

- **Feature:** `textChangedEnough` — a small delta must not trigger a
  resubmit.
- **Steps:** Start session `spa-noise`. Navigate to the fixture. Wait ~2s
  for the initial capture. Click **"Small change" exactly once** (adds
  ~150 chars, under the 200-char floor). Wait ~10s (through at least two
  poll ticks) **without clicking it again**. Stop.
- **Do not click the button more than once before checking.** The poll
  compares whatever the DOM looks like *at each tick* against the
  last-*sent* length — it has no memory of individual clicks between
  ticks. Three rapid clicks (450 cumulative chars) landing before the next
  tick fires is a *materially changed page* from the algorithm's point of
  view and **correctly** triggers a recapture; that's a test-design error,
  not a bug, and was caught exactly this way in an earlier run of this
  plan — see the spec's threshold description before assuming a single
  extra capture here is a regression.
- **Verify:** `uv run buddy sessions` → session `spa-noise` has
  **page_count = 1** for the fixture (only the initial capture — the one
  noise click did not produce a new row). `uv run buddy search
  "NOISE-MARKER-DELTA"` returns **no results** (that text was never sent).
- **PASS:** no extra capture from one sub-threshold change.

### TC3 — Decaying poll catches a silent DOM change

- **Feature:** the fallback poll (`recheck`/`scheduleRecheck` in
  `content.ts`) re-extracts and resubmits a material change even with no
  Navigation API event.
- **Steps:** Start session `spa-poll`. Navigate to the fixture. Within the
  first few seconds, click **"Silent big change"** (no pushState). Wait
  ~8s (covers the 2s and 5s poll ticks). Stop.
- **Verify:** `uv run buddy search "POLL-MARKER-BETA"` finds the page under
  `[spa-poll]`. `uv run buddy sessions` shows page_count = 2 for this
  session (initial + the poll-caught change).
- **PASS:** the silent DOM change was caught without any navigation event.

### TC4 — `navigatesuccess` catches a pushState change immediately

- **Feature:** the Navigation API listener bypasses the poll wait.
- **Steps:** Start session `spa-nav`. Navigate to the fixture. Click **"Big
  change + pushState"** and note the time. Within ~1-2s, check whether the
  page was already captured (poll on `uv run buddy search
  "NAV-MARKER-GAMMA"` a couple of times a second apart if needed). Stop.
- **Verify:** the page appears under `[spa-nav]` well before the first
  poll tick would have fired (~2s) — i.e. clearly faster than TC3's catch.
- **PASS:** immediate catch via `navigatesuccess`. **If the test Firefox
  build doesn't support the Navigation API**, this degrades gracefully to
  the poll fallback (still caught, just not "immediately") — note which
  behavior was observed; a poll-speed catch here is a known,
  non-blocking degradation, not a failure of Slice B itself.

### TC5 — Poll resets to the fast interval after a caught change

- **Feature:** `step` resets to 0 (2s) on every resubmit, so bursty
  follow-up edits keep getting caught quickly rather than waiting out a
  decayed interval.
- **Steps:** Start session `spa-decay`. Navigate to the fixture. Wait
  ~15s untouched (let the poll decay through 2s→5s→10s ticks). Click
  **"Silent big change"**. Wait only ~3s (would be too soon for the *next*
  decayed step, e.g. 20s, but is enough time if the schedule reset to 2s).
  Stop.
- **Verify:** `uv run buddy search "POLL-MARKER-BETA"` finds the page
  under `[spa-decay]`.
- **PASS:** caught within ~3s of the change despite 15s of prior
  inactivity — proves the reset, not just that the poll eventually runs.

### TC6 — Dwell excludes time spent away from the tab (focus-gated)

- **Feature:** dwell reuses the Tier-1 `tabs.ts` `active_ms` clock, so it's
  gated on tab focus.
- **Steps:** Start session `dwell-focus`. Open an ordinary article page and
  stay focused/reading for **~15s**. Switch to a different tab or
  application and stay away for **~20s**. Switch back to the article tab
  and stay for another **~10s**. Stop.
- **Verify:** `uv run buddy search "<phrase from the article>"` shows
  `dwell=` roughly **25s** (15+10), not ~45s.
- **PASS:** the ~20s away is excluded; observed dwell is close to 25s (a
  few seconds of slack for extraction/settle delays is fine, but it must
  be clearly closer to 25 than to 45).

### TC7 — Dwell flushes on tab close (not just on session stop)

- **Feature:** `flushDwell` on `browser.tabs.onRemoved`.
- **Steps:** Start session `dwell-close`. Open an article, stay focused for
  **~10s**, then **close the tab** (do not Stop the session first — leave
  it running). Then open the popup in a new tab and Stop.
- **Verify:** `uv run buddy search "<phrase from that article>"` shows
  `dwell=` close to **10s**, not `dwell=0` / absent.
- **PASS:** the abruptly-closed tab's dwell was still recorded.

### TC8 — Dwell flushes on session stop (last page, no other trigger)

- **Feature:** `flushAllDwell` in the "stop" case, enqueued before
  `session_end`.
- **Steps:** Start session `dwell-stop`. Open an article, stay focused for
  **~10s**, then **Stop** directly (don't navigate away or close the tab
  first — this is the only page in the session, so nothing else would
  ever flush it).
- **Verify:** `uv run buddy search "<phrase from that article>"` shows
  `dwell=` close to **10s**.
- **PASS:** the final page's dwell wasn't lost for lack of a later
  boundary.

### TC9 — Known limitation: dwell keeps accruing through a pause

- **Feature:** documents the accepted gap in
  `specs/2026-08-11-m3b-slice-b-spa-recapture-design.md` §3
  ("Deliberately not a boundary: `nav_edge` and pause") — this is an
  **expected quirk, not a failure**.
- **Steps:** Start session `dwell-pause`. Open an article, stay focused for
  **~5s**. **Pause** (badge `II`). While paused, stay focused on the
  *same* tab (as if still reading) for **~15s**. **Resume**, stay focused
  **~5s** more. Stop.
- **Verify:** `uv run buddy search "<phrase>"` — the reported `dwell=` is
  expected to be **close to 25s** (5+15+5), i.e. it does **not** exclude
  the 15s spent "paused," because the underlying Tier-1 clock is ambient
  and doesn't freeze on pause.
- **PASS (of the documentation, not a bug):** confirms the known
  limitation behaves exactly as the spec describes — if dwell instead
  showed ~10s (correctly excluding pause), that would mean the limitation
  was silently fixed without updating the spec, which should be flagged
  back, not treated as a bonus.

### TC10 — Regression: Slice A link graph still intact

- **Feature:** outbound-link catalogue, click tracking, nav edges,
  search-engine provenance (Slice A) must be unaffected by this slice's
  changes to `content.ts`/`background.ts`.
- **Steps:** Start session `regress-links`. Visit a link-rich page, click
  one on-page link. Stop.
- **Verify:** `uv run buddy links regress-links` shows navigation edges and
  the clicked link absent from "notable un-followed links," same as
  Slice A's own TC2-TC4.
- **PASS:** link-graph capture is unaffected.

### TC11 — Regression: pause captures nothing

- **Feature:** existing pause gating (M3a/Slice 0), must still hold with
  the new persistent content-script watcher running.
- **Steps:** Start session `regress-pause`. Visit page A. **Pause**. While
  paused, visit page B (a different page) and wait long enough that, if
  recapture were still active, a poll tick could have fired. **Resume**,
  visit page C. Stop.
- **Verify — session-scoped signals only:** `uv run buddy links
  regress-pause` (no nav edge to/from page B) and `uv run buddy sessions`
  (page_count for this session excludes B) — no Tier-2 activity
  attributable to page B; A and C are present.
- **Expected, not a failure: page B may still appear in plain `buddy
  search` output.** `browser.history.onVisited` (Tier-1 ambient history
  sync) is **always on**, completely independent of session/pause state
  by design (`pushVisit` in `sync.ts` has no recording-status check at
  all) — it's a separate table (`history_entries`) from session capture
  (`page_visits`). A bare, session-tag-less, "(untitled)"-titled line for
  page B in `buddy search`'s plain (non `-- session pages --`) block is
  Tier-1 doing its job, not a Tier-2 pause-gate leak. This exact
  confusion happened in an earlier run of this plan — don't re-flag it.
  If page B instead appears **under `-- session pages --` with a
  `[regress-pause]` tag**, or shows up in `buddy links regress-pause`,
  *that* would be the real regression to report.
- **PASS:** no Tier-2 (session-scoped) activity attributable to page B;
  a Tier-1 history line for B is fine and expected.

### TC12 — CLI dwell format

- **Feature:** `buddy search`'s dwell suffix only appears when non-zero.
- **Steps:** Use the sessions from TC6-TC9 (nonzero dwell) plus TC1 (likely
  zero dwell, since those visits were brief/incidental).
- **Verify:** pages with recorded dwell show `dwell=Ns`; pages with none
  show no suffix at all (not `dwell=0s`).
- **PASS:** suffix present only when `dwell_ms > 0`, matching
  `cli.py`'s `if row["dwell_ms"]` gate.

---

## Results

_Not yet run — fill in after executing this plan._

| # | Case | Result | Notes |
|---|------|--------|-------|
| TC1 | Baseline capture (regression) | | |
| TC2 | Noise below threshold not recaptured | | |
| TC3 | Decaying poll catches silent change | | |
| TC4 | `navigatesuccess` catches pushState change immediately | | |
| TC5 | Poll resets to fast interval after a catch | | |
| TC6 | Dwell excludes time away from tab | | |
| TC7 | Dwell flushes on tab close | | |
| TC8 | Dwell flushes on session stop | | |
| TC9 | Known limitation: dwell accrues through pause | | |
| TC10 | Regression: link graph (Slice A) | | |
| TC11 | Regression: pause captures nothing | | |
| TC12 | CLI dwell format | | |

## Teardown

Close Firefox (or Ctrl-C the smoke script) — the backend is torn down
automatically. Stop the `python3 -m http.server` fixture server. The
isolated `.smoke/` data + profile persist for the next run; use `--fresh`
to reset.

## Known limitations / expected quirks (not failures)

- **Dwell accrues through pause** (TC9) — by design for this slice; see
  the spec's "Deliberately not a boundary" note. Only worth escalating if
  it's ever observed to be *worse* than described (e.g. dwell missing
  entirely, not just over-counted).
- **`nav_edge` is not its own dwell-flush boundary** — in practice a real
  navigation almost always produces a `page_capture` shortly after, which
  does flush; the only gap is an intermediate page that's never captured
  at all (a PDF, `about:`, empty extraction), whose focused time folds
  into whichever URL was captured last.
- **Same-URL-in-two-tabs ambiguity** (carried from Slice A's identical
  `mark_link_clicked` tradeoff): if the same URL is open in two tabs in one
  session, dwell increments land on whichever `page_visit` row is "latest,"
  not necessarily the tab that earned them. Not exercised by this plan;
  documented for completeness.
- **Navigation API browser support**: if the test browser lacks it, TC4
  degrades to poll-speed catching (see that test case's note) — not a
  Slice B regression.
