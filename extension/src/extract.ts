import { Readability } from "@mozilla/readability";
import type { OutboundLink } from "./types";

export interface ExtractedPage {
  title: string;
  text: string;
  html: string;
}

/** Extract readable text + raw DOM HTML from a live document.
 *
 * Readability mutates its input, so it runs on a clone. The raw HTML is the
 * RENDERED DOM (post-JS), which is the point of extracting in the content
 * script: the backend can re-extract later, but only from what the page
 * actually showed (spec: raw HTML stored content-addressed for
 * reprocessability). Never throws — a capture failure must not break pages.
 */
export function extractPage(doc: Document): ExtractedPage {
  const html = doc.documentElement?.outerHTML ?? "";
  const title = doc.title ?? "";
  let text: string;
  try {
    const article = new Readability(doc.cloneNode(true) as Document).parse();
    text = article?.textContent?.trim() ?? "";
  } catch {
    text = "";
  }
  if (!text) {
    const body = doc.body?.cloneNode(true) as HTMLElement | undefined;
    body?.querySelectorAll("script, style, noscript, template").forEach((el) => el.remove());
    text = body?.textContent?.replace(/\s+/g, " ").trim() ?? "";
  }
  return { title, text, html };
}

/** Catalogue a document's outbound links: absolute http(s) hrefs only, deduped,
 * with collapsed/capped anchor text. The content script runs this on every
 * captured page. Never throws. */
export function extractLinks(doc: Document): OutboundLink[] {
  const seen = new Set<string>();
  const out: OutboundLink[] = [];
  for (const anchor of Array.from(doc.links) as HTMLAnchorElement[]) {
    const href = anchor.href; // resolved to absolute by the DOM
    if (!/^https?:/i.test(href) || seen.has(href)) continue;
    seen.add(href);
    out.push({
      href,
      text: (anchor.textContent ?? "").replace(/\s+/g, " ").trim().slice(0, 300),
      rel: anchor.rel ?? "",
    });
  }
  return out;
}
