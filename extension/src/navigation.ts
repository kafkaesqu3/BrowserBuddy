import type { EventPayload } from "./types";

// Known search engines → the query-string parameter carrying the user's query.
// Provenance is derived from the referring URL only — never by scraping result
// pages (design spec).
const SEARCH_ENGINES: Record<string, string> = {
  "www.google.com": "q",
  "google.com": "q",
  "www.bing.com": "q",
  "duckduckgo.com": "q",
  "search.brave.com": "q",
  "www.ecosia.org": "q",
  "www.startpage.com": "query",
  "search.yahoo.com": "p",
  "yandex.com": "text",
  "www.yandex.com": "text",
};

/** The search query that led to a page, or null when `fromUrl` is not a known
 * search engine (or is malformed). Never throws. */
export function searchProvenance(fromUrl: string | null): string | null {
  if (!fromUrl) return null;
  let url: URL;
  try {
    url = new URL(fromUrl);
  } catch {
    return null;
  }
  const param = SEARCH_ENGINES[url.hostname];
  if (!param) return null;
  const query = url.searchParams.get(param);
  return query && query.trim() ? query : null;
}

export interface NavEdgeInput {
  fromUrl: string | null;
  toUrl: string;
  transition?: string;
  qualifiers?: string[];
  occurredAt: number;
}

/** Build a nav_edge event, or null for a missing target or a self-edge
 * (reload / same-URL commit — not a navigation of interest). */
export function navEdgeEvent(
  input: NavEdgeInput,
): (EventPayload & { type: "nav_edge" }) | null {
  if (!input.toUrl || input.fromUrl === input.toUrl) return null;
  return {
    type: "nav_edge",
    from_url: input.fromUrl,
    to_url: input.toUrl,
    transition: input.transition ?? "",
    qualifiers: (input.qualifiers ?? []).join(","),
    query: searchProvenance(input.fromUrl),
    occurred_at: input.occurredAt,
  };
}
