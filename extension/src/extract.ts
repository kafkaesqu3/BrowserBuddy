import { Readability } from "@mozilla/readability";

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
    text = doc.body?.textContent?.replace(/\s+/g, " ").trim() ?? "";
  }
  return { title, text, html };
}
