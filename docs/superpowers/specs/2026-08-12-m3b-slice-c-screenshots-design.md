# M3b Slice C — Screenshots

**Parent spec:** `docs/superpowers/specs/2026-07-14-browserbuddy-design.md`
**Roadmap:** `docs/superpowers/ROADMAP.md` (M3b, slice `0 → (A, B) → C`)
**Date:** 2026-08-12

Slice C closes the last capture gap named in the M3b roadmap: dwell-gated
screenshots. It ships the REST blob endpoint and the `gzip.compress`-off-the-
event-loop fix that Slices 0/A/B deliberately deferred until screenshots — the
only consumer that needs either — actually existed.

**Scope decisions made during brainstorming (see conversation, not re-litigated
here):**
- **Screenshots only.** Screen/video recording (`getDisplayMedia` +
  `MediaRecorder`, sketched as a trade-off in the roadmap) is explicitly out of
  scope — documented as a future option, no code or UI stub. Its capture model
  (continuous recording of one explicitly-granted surface) is fundamentally
  different from periodic stills and would not reuse this slice's scheduler;
  it would reuse the REST blob upload path.
- **A screenshots-only on/off + interval toggle**, not a general per-capture-
  type toggle framework. Generalizing toggles to `page_capture`/`link_click`/
  `page_dwell` is deferred to its own future slice.
- **Periodic capture is viewport-only** (`tabs.captureVisibleTab`). **On-demand
  capture (button, always requires an active session) is full-page**, via
  scroll-and-stitch — the deliberate differentiator between the two triggers.
- **Backend assumed reachable.** No durable local retry-buffer for the REST
  upload step; a failed upload is logged and dropped, matching the parent
  spec's existing "screenshot capture fails: log and continue" tolerance.

---

## 1. Data model — schema v6 (new migration, additive)

```sql
CREATE TABLE screenshots (
    id INTEGER PRIMARY KEY,
    page_visit_id INTEGER NOT NULL REFERENCES page_visits (id),
    blob_key TEXT NOT NULL,
    captured_at INTEGER NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('periodic', 'on_demand_viewport', 'on_demand_fullpage')),
    phash TEXT NOT NULL
);

CREATE INDEX idx_screenshots_page_visit ON screenshots (page_visit_id);
```

One row per *stored* screenshot (near-duplicates are dropped before insert —
see §4). `page_visit_id` gives page-level filtering ("all screenshots for page
Y") via a direct join; site/domain-level filtering ("all screenshots for site
X") is **not** solved here — no table in this schema has a normalized host
column yet (nav_edges, outbound_links, page_visits all store full URLs only),
and that's a cross-cutting gap shared with the existing M3b "URL
canonicalization" follow-up, not something to solve just for screenshots.

## 2. Protocol (additive; stays v1)

```python
class PageScreenshotEvent(ProtocolModel):
    type: Literal["page_screenshot"]
    url: str = Field(min_length=1)
    occurred_at: int = Field(ge=0)
    kind: Literal["periodic", "on_demand_viewport", "on_demand_fullpage"]
    blob_keys: list[str] = Field(min_length=1)  # 1 for viewport, N ordered tiles for fullpage
```

Resolved to a `page_visit` via the same "latest `page_visit` for
`(session_id, url)`" lookup `add_page_dwell`/`mark_link_clicked` already use —
no new page-visit-id plumbing needed client-side. Requires an active session;
with none, `NoActiveSessionError` → error frame (existing pattern, connection
survives).

**New REST endpoint** (separate from the WS ingest channel — binary bytes
don't belong in a JSON WS frame, same reasoning that already keeps
`page_capture`'s `html` field inline-but-small and routes bigger blobs
out-of-band):

```
POST /api/blobs
Header: X-BB-Secret: <shared secret>   (same secret as WS auth; no client_id —
                                         this endpoint is a pure content-in/
                                         key-out store, no session-scoped
                                         side effects)
Body:    raw bytes (sanity capped, e.g. 10 MB; oversized → 413, logged)
Returns: 200 { "key": "<sha256 hex>" }
```

Deliberately generic, not screenshot-specific — it's the REST twin of
`BlobStore.put()`, and the natural upload path for future recording video
chunks too.

## 3. Extension

### Settings (per-session, mutable mid-session)

`SessionManager` state gains `screenshotsEnabled: boolean` and
`screenshotIntervalMs: number` (default 30000), set at session start alongside
the name. A new `bb:session` action, `"update-capture"` (payload
`{ screenshotsEnabled, screenshotIntervalMs }`), lets `handleSessionCommand`
apply changes directly to session state while recording — no restart.

### Popup (`popup.html`/`popup.ts`)

- Start form: "Capture screenshots" checkbox + interval-seconds input,
  alongside the existing name field.
- While recording: same controls stay live; changing them fires
  `bb:session update-capture` immediately.
- "📷 Capture now" button, visible only while recording: triggers on-demand
  **full-page** capture of the current tab (periodic already covers the
  viewport-only case; on-demand's purpose is covering what periodic can't).

### Background — scheduling

**Mechanism (settled after evaluating alternatives — see rationale below):**
extends the existing `tabs.onActivated`/`windows.onFocusChanged` handlers
(already used for the dwell tracker) to message the newly-active tab's content
script `{ type: "bb:screenshot-tick-control", action: "start" }` and the
previously-active one `"stop"`; losing browser focus entirely sends `"stop"`
with no new `"start"` target.

On each `bb:screenshot-tick` message from the (necessarily active) tab: check
`recording && screenshotsEnabled && now - lastCaptureAt >= screenshotIntervalMs`
(a new small per-tab timestamp, same shape as the existing `DWELL_KEY`/
`NAV_URLS_KEY` maps). If all pass: `captureVisibleTab` → `POST /api/blobs` →
enqueue `PageScreenshotEvent` via the normal (already-durable) `EventQueue` —
recording status re-checked immediately before the network calls, matching
the existing "check happens last" pattern in `handlePageMessage`.

**On-demand (button click, always full-page):** background sends
`{ type: "bb:capture-fullpage" }` to the content script, which scrolls in
viewport-height steps (pausing briefly at each stop for reflow/lazy-load) and
signals background at each stop; background calls `captureVisibleTab` per
stop, collecting ordered tile bytes, uploads each via `POST /api/blobs`, then
sends one `PageScreenshotEvent` with all resulting keys
(`kind: "on_demand_fullpage"`).

**Scheduling mechanism rationale (why this, not simpler alternatives):**
- *Not* `browser.alarms`: production installs enforce a coarse minimum period
  (~30s in current Chrome; similar order of magnitude in Firefox) — too coarse
  for a "every 5 seconds" configured interval, which is a real requirement
  here.
- *Not* a single global `setInterval` inside `background.ts`: works today
  only because the WS heartbeat's own traffic happens to prevent MV3
  worker-suspension while a session is recording — coupling screenshot
  scheduling to that incidentally is fragile, even setting aside the
  backend-uptime assumption above (an idle-suspended worker drops a bare
  `setTimeout`/`setInterval` outright; `alarms` are the only thing that
  reliably wakes a terminated worker, and they're too coarse per above).
- *Not* every tab's content script ticking independently (message-passing
  itself is enough to wake a suspended worker, so this would work
  *correctness*-wise): doesn't scale — at 50–100 open tabs it's O(N) live
  timers and O(N) message sends per tick to filter down to the one signal
  that ever matters (whichever tab is active). The chosen design (background
  explicitly starts/stops exactly one tab's ticker, on focus change) gets the
  same fine-grained, worker-suspension-proof properties at O(1) cost
  regardless of tab count.

### Content script (`content.ts`) — two independent, small additions

1. **Tick loop:** starts/stops on the control message above, `setTimeout`-
   based, sends `bb:screenshot-tick` on each fire. No knowledge of
   interval/enabled state — pure heartbeat; background decides everything
   (matches the existing "content script doesn't gate, background does"
   split already used for `bb:page`/`bb:click`).
2. **Full-page scroll driver:** only runs on-demand, on request — scrolls,
   waits, signals "stopped at position N," repeats until `scrollHeight` is
   covered, then signals done. Independent of the tick loop; no interaction
   between the two.

### New REST client (first `fetch` usage in this extension)

A small module wrapping
`fetch(backendHttpUrl + "/api/blobs", { method: "POST", headers: { "X-BB-Secret": secret }, body })`.
`Settings.backendUrl` is currently a `ws://` URL; derive the HTTP equivalent
with a simple scheme swap (`ws→http`, `wss→https`) rather than adding a new
setting field.

## 4. Backend

### `BlobStore` — skip compression for images

`put()` currently always gzips (fine for HTML text; wasteful for already-
compressed JPEG bytes — CPU cost for ~0 size benefit, sometimes negative).
Add a `compress: bool = True` parameter; screenshot callers pass
`compress=False`. Since compressed and uncompressed variants of the same
content-hash key must never collide, store them under different suffixes —
`path_for(key, compressed) -> <key[:2]>/<key[2:]>.gz` (existing) or
`...bin` (new) — and have `get()` check `.gz` first, then `.bin`, so every
existing caller (always compressed) is unaffected.

### `PageScreenshotEvent` dispatch

Mirrors `add_page_dwell`'s "latest page_visit by url" resolution, then:

1. Read back the blob(s) for `blob_keys` (1 for viewport, N ordered tiles for
   full-page).
2. **Off the event loop** (`asyncio.to_thread` — this is also where the
   existing `gzip.compress` call for HTML moves to a thread, finally closing
   the roadmap's long-carried "backend `gzip.compress` on the event loop"
   item, since screenshots are the consumer that made it load-bearing):
   for full-page, stitch tiles into one image (Pillow, vertical paste loop);
   compute a perceptual hash of the final image — a dependency-free
   difference-hash (resize to 9×8 grayscale, threshold adjacent-pixel
   comparisons into a 64-bit hash), not a separate image-hashing library.
3. Compare against the phash of the most recent existing `screenshots` row
   for this `page_visit_id` via Hamming distance. If the distance is at or
   below `settings.screenshot_dedup_threshold`, discard as a near-duplicate —
   no new row (content-addressing already dedupes byte-identical captures;
   this catches *perceptually* similar ones, e.g. a static page with a
   blinking cursor). Otherwise store the (stitched, if applicable) image via
   `blobs.put(..., compress=False)` and insert the `screenshots` row.

### Config — tunable dedup threshold

```python
class Settings(BaseSettings):
    ...
    screenshot_dedup_threshold: int = 8  # max Hamming distance (of 64 bits)
```

Overridable via `BROWSERBUDDY_SCREENSHOT_DEDUP_THRESHOLD`, matching the
existing `data_dir`/`host`/`port` env-var pattern — no new config mechanism.
`0` disables dedup entirely; `64` collapses to "only ever keep the first
screenshot per page." A backend settings *page* (mentioned as a future want)
is out of scope here; env var is sufficient for now.

### New REST endpoint

```python
@app.post("/api/blobs")
async def upload_blob(request: Request) -> dict:
    # header auth via the existing verify_secret(); no client_id needed
    body = await request.body()
    key = await asyncio.to_thread(app.state.blobs.put, body, compress=False)
    return {"key": key}
```

### New dependency

Pillow — JPEG decode, tile stitching, and the grayscale resize the
difference-hash needs. No image-hashing library; the hash itself is plain
Python over pixel data Pillow already provides.

### CLI

`buddy screenshots <session> [--url URL]` — lists `captured_at`, `kind`,
`blob_key` for a session (optionally filtered to one page). Gives this slice
its own immediate consumer, matching the pattern every prior slice used
(`buddy links` for Slice A, the dwell suffix in `buddy search` for Slice B),
and directly answers "show me all screenshots from page Y" without waiting
for M4's `get_screenshots` agent tool (already named in the parent spec).

## Error handling

| Failure | Behavior |
|---|---|
| `captureVisibleTab` throws (protected page like `about:`/`chrome:`, rate-limited) | Log and skip this one capture — some pages are genuinely impossible to screenshot |
| `POST /api/blobs` fails (backend unreachable) | Log and drop this capture (best-effort — see the "backend assumed reachable" scope decision); no retry buffer |
| Oversized blob upload | Reject (>10 MB sanity cap), log |
| `page_screenshot` with no active session | Error frame via `NoActiveSessionError`, same path every other Tier-2 event uses |
| Stitching fails (corrupt/mismatched tile) | Log and discard that on-demand capture; text remains the primary record |

## Testing

- **Backend:** `BlobStore` compress-flag behavior (including the `.gz`/`.bin`
  suffix split); v6 migration preserves v1–v5 data; `add_page_screenshot`
  (latest-by-url resolution; dedup skip vs. insert using synthetic images at
  known Hamming distances; stitching output dimensions); REST endpoint auth +
  content-key contract + oversized-body rejection; app dispatch no-session
  error-frame path; config threshold override.
- **Extension:** pure-unit tests for the tick start/stop control logic, the
  interval-elapsed decision function, the full-page scroll-stop stepper (pure
  math over scrollHeight/viewportHeight, no browser API), and the
  `pageScreenshotEvent` conversion helper. The actual `captureVisibleTab`/
  focus-listener wiring is build + interactive-smoke verified, the same split
  Slice A/B used for their own browser-API-heavy pieces (`webNavigation`,
  `navigatesuccess`).

## Out of scope for Slice C

Screen/video recording (`getDisplayMedia`/`MediaRecorder` — documented future
option, no code or UI); the general per-capture-type toggle framework
(text/links/dwell togglable, deferred to its own future slice); domain/site-
level query indexing (cross-cutting, shares root cause with the existing URL-
canonicalization follow-up); periodic full-page capture (full-page stays
on-demand only); a backend settings UI (env var for now); OCR/embedding of
screenshot content (M4 territory); blob garbage collection (no deletion exists
anywhere in the blob store yet, not new to this slice); durable local
retry-buffering of failed uploads (dropped per the "backend assumed reachable"
scope decision — revisit if that assumption stops holding).
