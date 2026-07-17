import type { EventPayload } from "./types";

/** Cap on inline raw HTML. Larger pages keep their extracted text; the raw
 * DOM is dropped (bounds WS frame size — the server caps frames at 16 MB,
 * and multi-MB outliers are rarely worth archiving). */
export const MAX_HTML_CHARS = 2_000_000;

export interface PageMessage {
  type?: string;
  url?: string;
  title?: string;
  text?: string;
  html?: string;
}

/** Convert a content-script "bb:page" message into a page_capture event.
 * Returns null for unusable messages (no url, or nothing captured). */
export function pageCaptureEvent(
  message: PageMessage,
  container: string | null,
  now: () => number = Date.now,
): (EventPayload & { type: "page_capture" }) | null {
  if (!message.url) return null;
  const text = message.text ?? "";
  let html = message.html ?? null;
  if (!text && !html) return null;
  if (html !== null && html.length > MAX_HTML_CHARS) {
    console.warn(
      "BrowserBuddy: raw HTML too large, keeping text only",
      message.url,
    );
    html = null;
  }
  return {
    type: "page_capture",
    url: message.url,
    title: message.title ?? "",
    visited_at: now(),
    text,
    html,
    container,
  };
}
