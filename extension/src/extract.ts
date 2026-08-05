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

/** The set of absolute http(s) hrefs that live inside the page's main readable
 * content, as identified by Readability. Used to flag which catalogued links
 * are article content vs. site chrome (nav/header/footer/sidebar/ads).
 *
 * Runs on a clone (Readability mutates) and reads hrefs from the parsed article
 * HTML, which Readability has already resolved to absolute URLs — so they match
 * the live DOM's `anchor.href`. Returns an empty set for non-article pages or on
 * any failure; extractLinks treats "empty" as "cannot tell" (see below). */
function readableHrefs(doc: Document): Set<string> {
  const set = new Set<string>();
  try {
    const article = new Readability(doc.cloneNode(true) as Document).parse();
    if (!article?.content) return set;
    const parsed = new DOMParser().parseFromString(article.content, "text/html");
    for (const anchor of Array.from(parsed.links) as HTMLAnchorElement[]) {
      if (/^https?:/i.test(anchor.href)) set.add(anchor.href);
    }
  } catch {
    // Non-article page or parser failure: leave empty.
  }
  return set;
}

/** Catalogue a document's outbound links: absolute http(s) hrefs only, deduped,
 * with collapsed/capped anchor text. Each link is flagged `in_main` when it sits
 * inside the main readable content. The content script runs this on every
 * captured page. Never throws.
 *
 * When Readability finds no article (empty href set), every link is flagged
 * `in_main` — with no detectable main region we cannot single out chrome, so we
 * fall back to treating all links as content rather than hiding them all. */
export function extractLinks(doc: Document): OutboundLink[] {
  const mainHrefs = readableHrefs(doc);
  const noMainRegion = mainHrefs.size === 0;
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
      in_main: noMainRegion || mainHrefs.has(href),
    });
  }
  return out;
}
