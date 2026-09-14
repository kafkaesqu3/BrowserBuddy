/** Pure scheduling/geometry helpers for screenshot capture (Slice C). No
 * browser API access — background.ts wires these into captureVisibleTab
 * calls and tabs.sendMessage scroll-step requests. */

/** Number of viewport-height steps needed to cover a page's full scroll
 * height. Always at least 1 — a page shorter than one viewport still gets
 * one capture. */
export function scrollStepCount(scrollHeight: number, viewportHeight: number): number {
  if (viewportHeight <= 0) return 1;
  return Math.max(1, Math.ceil(scrollHeight / viewportHeight));
}

/** Y-offset (px) to scroll to for a given full-page tile step index. */
export function scrollOffsetForStep(step: number, viewportHeight: number): number {
  return step * viewportHeight;
}

/** True once at least intervalMs has passed since the last capture. */
export function intervalElapsed(now: number, lastCapturedAt: number, intervalMs: number): boolean {
  return now - lastCapturedAt >= intervalMs;
}
