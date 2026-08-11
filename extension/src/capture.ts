import type { EventPayload, OutboundLink } from "./types";

/** Cap on inline raw HTML. Larger pages keep their extracted text; the raw
 * DOM is dropped (bounds WS frame size — the server caps frames at 16 MB,
 * and multi-MB outliers are rarely worth archiving). */
export const MAX_HTML_CHARS = 2_000_000;

/** Cap on outbound links catalogued per page (bounds WS frame size alongside
 * the 2 MB HTML cap). */
export const MAX_LINKS = 1000;

export interface PageMessage {
  type?: string;
  url?: string;
  title?: string;
  text?: string;
  html?: string;
  links?: OutboundLink[];
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
    links: (message.links ?? []).slice(0, MAX_LINKS),
  };
}

export interface ClickMessage {
  type?: string;
  page_url?: string;
  href?: string;
  text?: string;
}

/** Convert a content-script "bb:click" message into a link_click event.
 * Returns null when the page or target href is missing. */
export function linkClickEvent(
  message: ClickMessage,
  now: () => number = Date.now,
): (EventPayload & { type: "link_click" }) | null {
  if (!message.page_url || !message.href) return null;
  return {
    type: "link_click",
    page_url: message.page_url,
    href: message.href,
    text: message.text ?? "",
    clicked_at: now(),
  };
}

/** Build a page_dwell event from a locally-computed dwell increment (there is
 * no incoming content-script message to convert — background.ts derives the
 * increment itself from the Tier-1 tab-active-time tracker; see tabs.ts). */
export function pageDwellEvent(
  url: string,
  dwellMs: number,
  now: () => number = Date.now,
): EventPayload & { type: "page_dwell" } {
  return { type: "page_dwell", url, dwell_ms: Math.max(0, dwellMs), occurred_at: now() };
}
