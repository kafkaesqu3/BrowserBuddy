import type { HistoryVisit, StorageArea } from "./types";

const WATERMARK_KEY = "bb:history_watermark";
const MAX_RESULTS = 5000;

export interface HistoryItemLike {
  url?: string;
  title?: string;
  lastVisitTime?: number;
}

export interface HistoryApi {
  search(query: {
    text: string;
    startTime: number;
    maxResults: number;
  }): Promise<HistoryItemLike[]>;
}

export function visitFromHistoryItem(item: HistoryItemLike): HistoryVisit | null {
  if (!item.url || item.lastVisitTime === undefined) return null;
  return {
    url: item.url,
    title: item.title ?? "",
    visit_time: Math.floor(item.lastVisitTime),
  };
}

/** Catch up on visits recorded while the extension was asleep or disconnected.
 * Live visits arrive via history.onVisited; this covers the gaps. Granularity
 * limit: history.search returns one item per URL with only its last visit
 * time, so multiple missed visits to the same URL collapse to one — acceptable
 * because places.sqlite backfill is the authoritative record. The backend
 * dedupes on (url, visit_time), so overlap is harmless. The one-time deep
 * backfill is `buddy import-history` (places.sqlite), not this. */
export async function gapFillVisits(
  historyApi: HistoryApi,
  storage: StorageArea,
  now: () => number = Date.now,
): Promise<HistoryVisit[]> {
  const stored = (await storage.get(WATERMARK_KEY))[WATERMARK_KEY] as
    | number
    | undefined;
  const startTime = stored ?? now();
  const items =
    stored === undefined
      ? []
      : await historyApi.search({ text: "", startTime, maxResults: MAX_RESULTS });
  if (items.length >= MAX_RESULTS) {
    console.warn(
      "BrowserBuddy: gap-fill hit maxResults; some visits may be missing until import-history runs",
    );
  }
  const visits = items
    .map(visitFromHistoryItem)
    .filter((v): v is HistoryVisit => v !== null);
  await storage.set({ [WATERMARK_KEY]: now() });
  return visits;
}
