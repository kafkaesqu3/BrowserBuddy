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
}

export interface PageCapture {
  url: string;
  title: string;
  visited_at: number;
  text: string;
  html: string | null;
  container: string | null;
}

export type EventPayload =
  | { type: "tab_state"; tabs: TabRecord[] }
  | { type: "history_visits"; visits: HistoryVisit[] }
  | { type: "session_start"; name: string; started_at: number }
  | { type: "session_end"; ended_at: number }
  | ({ type: "page_capture" } & PageCapture);

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
