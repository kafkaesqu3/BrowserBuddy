// SPA change-driven recapture: a decaying poll schedule plus a text-length
// delta threshold, so a route change under the same URL (or any DOM
// mutation that materially rewrites the page) gets re-extracted without
// re-triggering on trivial noise (a ticking view counter, a live
// timestamp).

/** Poll delays, in ms, stepped through on consecutive no-change ticks and
 * capped at the last entry. content.ts resets to step 0 whenever a change
 * is sent (bursty updates keep getting caught quickly). */
export const RECAPTURE_DELAYS_MS = [2000, 5000, 10000, 20000, 40000];

export function delayForStep(step: number): number {
  const idx = Math.min(Math.max(step, 0), RECAPTURE_DELAYS_MS.length - 1);
  return RECAPTURE_DELAYS_MS[idx]!;
}

/** True when the extracted text length changed enough to be worth
 * re-sending: more than 20% of the previous length, or more than 200
 * chars, whichever is larger. The 200-char floor catches a route change on
 * a short page; the 20% relative threshold ignores a ticking counter on a
 * long article. Symmetric — a shrinking page counts too. */
export function textChangedEnough(lastLength: number, newLength: number): boolean {
  const delta = Math.abs(newLength - lastLength);
  return delta > Math.max(0.2 * lastLength, 200);
}
