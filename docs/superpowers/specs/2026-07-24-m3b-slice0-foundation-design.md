# M3b Slice 0 — Foundation & smoke debt

**Parent spec:** `docs/superpowers/specs/2026-07-14-browserbuddy-design.md`
**Roadmap:** `docs/superpowers/ROADMAP.md` (M3b)
**Date:** 2026-07-24

M3b is too large for one spec→plan cycle, so it is built in slices:
**0 → (A link graph, B dwell+SPA in either order) → C screenshots**. This
document covers **Slice 0**: three present-need engineering/usability fixes that
carry no new capture surface. The REST blob endpoint and moving `gzip.compress`
off the event loop were originally grouped here but are deferred to **Slice C**,
built alongside their only consumer (screenshots) — they have no consumer today.

Three independent changes, each TDD'd and committed separately, smallest-first.

---

## 1. Popup "⚙ Settings" link

**Problem (M3a smoke, plan follow-up §6):** the toolbar popup is the primary
surface, but the shared-secret field lives only on the options page, reachable
via `about:addons → Preferences`. First-time pairing is undiscoverable.

**Change:** add a `⚙ Settings` link to the popup footer (beside the connection
line) that calls `browser.runtime.openOptionsPage()`. UI only —
`extension/src/popup/popup.html` + `popup.ts`. No background or protocol change.

## 2. Dangling-session reconcile (client-side durability)

**Root cause:** `handleSessionCommand`'s stop case (`background.ts:164`) calls
`sessionManager.stop()`, which removes the `bb:session` key and *then* returns
the `session_end` event to enqueue. If `queue.enqueue` throws, the event is lost
and the backend session stays open until the next `session_start` implicitly
closes it.

**Change:** clear local session state only *after* the event is durably
enqueued.
- New `SessionManager.stop(commit: (event) => Promise<void>): Promise<void>`:
  compute `session_end` from current state, `await commit(event)`, then remove
  the key. If `commit` throws, state is preserved so a later stop / wake-
  reconcile retries.
- The background passes an enqueue-and-notify callback as `commit`.
- Backend `end_session` is already idempotent, so a duplicate `session_end`
  after a partial failure is a harmless no-op ack.

**Not covered (documented, M4):** a session orphaned by uninstall mid-recording
can never be closed by the client — M4 digests must tolerate open-ended
sessions regardless. No protocol change here.

## 3. EventQueue scaling (per-event storage keys)

**Root cause:** one `bb:queue` array holds every envelope; `enqueue`/`ack`/
`drop` read-modify-write the whole array (`queue.ts`), so each op moves the full
queue byte-weight — including page-capture HTML bodies up to 2 MB (present since
M3a), not just at future screenshot volume.

**Change:** split the queue into per-event keys.
- `bb:seq` — monotonic counter (unchanged).
- `bb:queue:index` — lightweight ordered array of `{ seq, type }` (no bodies).
- `bb:q:<seq>` — one `EventEnvelope` body per key.
- `enqueue`: assign seq, write `bb:q:<seq>` + append to index + bump `bb:seq`.
  No other body is touched. `tab_state` coalescing and the `MAX_QUEUE` cap
  operate on the index (removing superseded/oldest body keys via
  `storage.remove`).
- `pending`: read the index, fetch the `bb:q:<seq>` bodies, sort by seq.
- `ack(seq)`: remove body keys for `index` entries with `entry.seq <= seq`;
  keep the rest. `drop(seq)`: remove that one body key + index entry.
- **Migration:** on construction, if a legacy `bb:queue` array is present,
  convert it to the per-event layout once (survives an extension update that
  ships with a queued backlog).

**Behavior preserved:** monotonic seq across worker death, `tab_state`
coalescing, oldest-drop cap, `ack` prunes `<= seq`, `drop` removes exactly one,
mutex-serialized RMW. The public `EventQueue` API is unchanged, so existing
behavior tests hold. The one cap test that seeds the old `bb:queue` layout
directly is rewritten to the new layout and doubles as the migration test.

---

## Out of scope for Slice 0

REST blob endpoint, `gzip.compress`-to-thread (→ Slice C); idle-API active-time
honesty, SPA recapture, dwell, link graph, screenshots (→ Slices A/B/C);
sensitive-content firewall (cross-cutting, pre-multi-user).
