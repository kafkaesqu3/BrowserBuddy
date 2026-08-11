// Ingest protocol v1. Must stay in sync with backend models.py.
export const PROTOCOL_VERSION = 1;

export interface HistoryVisit {
  url: string;
  title: string;
  visit_time: number; // ms since epoch, UTC
}

export interface TabRecord {
  tab_id: number;
  window_id: number;
  url: string;
  title: string;
  group_id: number | null;
  container: string | null; // Firefox-only enrichment; null elsewhere
  opened_at: number | null;
  last_activated_at: number | null;
  active_ms: number;
}

export interface OutboundLink {
  href: string;
  text: string;
  rel: string;
  // True when the anchor sits inside the page's main readable content
  // (per Readability) rather than site chrome — nav, header, footer, sidebar,
  // ads. The full catalogue is still captured; this only flags which links are
  // article content so the backend digest can surface the notable ones.
  in_main: boolean;
}

export interface PageCapture {
  url: string;
  title: string;
  visited_at: number;
  text: string;
  html: string | null;
  container: string | null;
  links: OutboundLink[];
}

export type EventPayload =
  | { type: "tab_state"; tabs: TabRecord[] }
  | { type: "history_visits"; visits: HistoryVisit[] }
  | { type: "session_start"; name: string; started_at: number }
  | { type: "session_end"; ended_at: number }
  | ({ type: "page_capture" } & PageCapture)
  | {
      type: "link_click";
      page_url: string;
      href: string;
      text: string;
      clicked_at: number;
    }
  | {
      type: "nav_edge";
      from_url: string | null;
      to_url: string;
      transition: string;
      qualifiers: string;
      query: string | null;
      occurred_at: number;
    }
  | {
      type: "page_dwell";
      url: string;
      dwell_ms: number;
      occurred_at: number;
    };

export interface EventEnvelope {
  v: number;
  seq: number;
  event: EventPayload;
}

export type ServerFrame =
  | { type: "auth_ok"; last_seq: number }
  | { type: "ack"; seq: number }
  | { type: "error"; detail: string }
  | { type: "pong" };

// Minimal facade over browser.storage.{local,session} so modules are testable.
export interface StorageArea {
  get(keys: string | string[]): Promise<Record<string, unknown>>;
  set(items: Record<string, unknown>): Promise<void>;
  remove(keys: string | string[]): Promise<void>;
}
