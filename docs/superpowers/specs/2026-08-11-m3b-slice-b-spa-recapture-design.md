# M3b Slice B — SPA change-driven recapture & minimal dwell

**Parent spec:** `docs/superpowers/specs/2026-07-14-browserbuddy-design.md`
**Roadmap:** `docs/superpowers/ROADMAP.md` (M3b, slice `0 → (A, B) → C`)
**Date:** 2026-08-11

Slice B closes the biggest remaining Tier-2 capture gap: `content.ts` today
extracts a page exactly once (~500ms after load) and never looks again, so a
single-page app that swaps in an entirely different article under the same
URL is captured only in its initial (often near-empty) state. Slice B adds
change-driven recapture, plus a minimal per-page dwell figure that reuses
existing tab-tracking machinery rather than building new scroll/visibility/
idle infrastructure.

**Scope note (trimmed from the original roadmap sketch):** the roadmap listed
full scroll/visibility/focus dwell tracking and idle-API integration as part
of Slice B. Neither is required by anything currently planned — Slice C's
"dwell-gated screenshots" only needs a coarse "how long has the user been on
this page" signal, and M4's link-scoring formula doesn't reference dwell at
all. This slice ships a crude, focus-gated dwell figure (reusing the Tier-1
`tabs.ts` active-time tracker) sufficient to unblock Slice C later; true
idle-aware, scroll-informed dwell is deferred indefinitely, to be picked up
only if a concrete consumer needs the precision.

---

## 1. Data model — schema v5 (new migration, additive)

```sql
ALTER TABLE page_visits ADD COLUMN dwell_ms INTEGER NOT NULL DEFAULT 0;
```

`dwell_ms` accumulates via additive updates (see §4) rather than being set
once. **Correction (found during code review, post-implementation):** this
does not mean multiple increments land safely per page visit — there is no
periodic/heartbeat flush, so in practice each page gets exactly *one* flush,
at the next capture/tab-close/session-stop boundary. "Additive" only means
a *second* page reusing the same URL later in the session won't clobber an
earlier flush's value; it does not bound the loss from a single missed
flush. See the Error handling section below for the corrected claim.

## 2. Protocol (additive; stays v1)

- **New `PageDwellEvent`** — `{ type: "page_dwell", url: str, dwell_ms: int,
  occurred_at: int }`. Requires an active session; with none, the backend
  raises `LookupError` → error frame (same path as `page_capture` /
  `nav_edge` / `link_click`), connection survives.

`page_capture` itself is unchanged — SPA recapture reuses the existing event
type; a resubmission is just another `page_capture` at the current URL with a
new `visited_at`, identical in shape to what a page reload already produces.

## 3. Extension capture

- **`content.ts`** — replace the one-shot `send()` with a persistent
  per-document watcher (the script stays alive across SPA route changes the
  same way its existing click listener already does):
  - Register `navigation.navigatesuccess` (Navigation API) as an immediate
    recheck trigger.
  - Run a decaying poll as a fallback for DOM mutations that don't fire a
    Navigation API event: intervals `2s → 5s → 10s → 20s → 40s`, capped at
    40s, repeating at the cap until a change is detected.
  - On each trigger: re-run the existing Readability extraction and compare
    the new text length to the last-*sent* length. Resubmit (a fresh
    `bb:page` message) only if `abs(delta) > max(0.2 * lastLength, 200)`
    chars — big enough to skip a ticking view-counter or a one-line
    timestamp update, small enough to catch a genuine route change on a
    short page. On resubmit: reset the poll schedule to `2s` (bursty content
    keeps getting caught quickly) and update the last-sent-length baseline.
    Otherwise let the interval decay to the next step.
  - No new pause/stop gating is needed: `background.ts` already drops every
    `bb:page`/`bb:click` message unless `state.status === "recording"`
    (existing pattern, verified by TC9 in the Slice A smoke run), so a
    recapture firing during a paused session is silently discarded exactly
    like today's click listener.
- **`background.ts`** — minimal dwell:
  - When a `page_capture` is built for a tab, snapshot that tab's current
    `active_ms` (already tracked by `tabs.ts`, Tier-1: focus-based, handles
    reconnect/refresh/gap-filling).
  - At the next boundary for that tab — the next `page_capture`, tab close,
    or session stop — compute `delta = current_active_ms - snapshot`, clamp
    to `≥ 0` (same clamp `tabs.ts` already applies for wall-clock
    regressions), and if `delta > 0` send
    `page_dwell { url: <the page being left>, dwell_ms: delta }`.
  - This requires no new content-script listeners, no idle API, and no
    scroll signal — it is exactly the existing Tier-1 clock, sliced at page
    boundaries.
  - **Deliberately not a boundary: `nav_edge` and pause.** A `nav_edge`
    almost always precedes a `page_capture` for the new page (which already
    rebases), so treating it as a separate boundary would only matter for an
    intermediate page that's never captured (a PDF, `about:`, an empty
    extraction) — that page's focused time is folded into whichever URL was
    last captured instead. Pause is not a boundary either, and this one has
    real leakage: the underlying Tier-1 `active_ms` clock is ambient and
    session-independent, so it keeps ticking through a pause; nothing
    freezes or carries the baseline forward, so time spent focused while
    "paused" is folded into the next flush on resume. A correct pause
    boundary would need the tracker itself frozen (or the baseline
    explicitly carried across the pause window), not just an extra rebase
    call — out of scope for this crude, focus-gated figure. Both gaps are
    accepted for the same reason: nothing currently planned needs dwell
    precision this fine (see the scope note at the top of this document).
- **`capture.ts` / `types.ts`** — add `PageDwell` to `EventPayload`; a small
  pure conversion helper (`pageDwellEvent`) mirroring `linkClickEvent`.

## 4. Backend, CLI, testing

- **`models.py`** — `PageDwellEvent`; widen the envelope union.
- **`storage.py`** — `add_page_dwell(session_id, url, dwell_ms)`: find the
  latest `page_visit` for `(session_id, url)` (same "latest by url" lookup
  `mark_link_clicked` already uses) and
  `UPDATE page_visits SET dwell_ms = dwell_ms + ? WHERE id = ?`. Known
  limitation, carried forward from the identical tradeoff in link-click
  matching: if the same URL is open in two tabs within one session
  simultaneously, dwell increments are attributed to whichever `page_visit`
  row is "latest," not necessarily the tab that earned them. Accepted;
  matches existing precedent.
- **`app.py`** — dispatch `PageDwellEvent`, gated on an active session (else
  `LookupError` → error frame, existing pattern).
- **CLI** — `buddy search`'s existing "session pages" block gains a dwell
  suffix (e.g. `dwell=42s`) formatted via the existing `_fmt_time`-style
  helpers; it's the one place a bare list of individual `page_visit` rows
  already renders.
- **Testing** — backend TDD (v5 migration preserves v1–v4 data intact;
  `add_page_dwell` storage test incl. the "latest by url" resolution and the
  additive-update behavior; model validation; app dispatch, incl. the
  no-active-session error-frame path). Extension TDD for the pure units (the
  text-length-delta decision function, the decay-schedule stepper,
  `pageDwellEvent` conversion); the `navigatesuccess` listener and poll wiring
  are verified by build + the interactive smoke (same split Slice A used for
  `webNavigation`).

## Error handling

- Text-length comparison never throws (reuses the existing guarded
  extraction path).
- `page_dwell` with no active session → error frame (existing pattern),
  connection survives.
- Negative/zero deltas from clock anomalies clamp to 0 before sending —
  mirrors the existing clamp in `tabs.ts`.
- **A tab closed or the browser killed mid-flush can lose that page's
  entire dwell figure, not just a bounded "recent increment"** — dwell
  flushes exactly once per page visit (at the next capture/tab-close/
  session-stop boundary), so there is no periodic flush to fall back on if
  the one flush is lost. The actual risk window is narrow — every event
  listener in `background.ts` fires an async chain from a synchronous
  callback it doesn't return to the browser (`logged(promise)`, never
  `return promise`), so this loss window exists for every event type, not
  just dwell, and only manifests if the whole extension process (not just
  the tab) is torn down mid-write (e.g. closing the last open browser
  window on Chrome MV3). Documented, not fixed, in this slice — see the
  roadmap's carried-engineering-items list for the considered follow-up
  (piggyback a periodic dwell flush onto the existing `bb-sync` alarm,
  which would make the "increments" framing above actually true).

## Out of scope for Slice B

Idle-API integration and scroll/visibility-based dwell precision (both
explicitly deferred — no current consumer needs them; revisit if one
emerges). MutationObserver-based instant change detection (poll +
`navigatesuccess` only, for this slice). Dwell-gated screenshots (Slice C,
which can consume this slice's `dwell_ms` once it exists). Embeddings,
digests, and the recall agent that will eventually rank on dwell (M4).
