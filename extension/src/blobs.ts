/** REST client for the backend's generic content-addressed blob store — the
 * out-of-band upload path for binary screenshot bytes (WS frames stay
 * JSON-only; see capture.ts's pageScreenshotEvent, which only ever carries
 * the resulting key, never raw bytes). Best-effort: callers log-and-drop on
 * rejection (see background.ts) — no retry buffer, per the "backend
 * assumed reachable" scope decision in the design spec. */

/** Settings.backendUrl is a ws(s):// URL (the WS ingest endpoint); the REST
 * blob endpoint lives on the same host/port over plain HTTP(S). */
export function toHttpUrl(wsUrl: string): string {
  if (wsUrl.startsWith("wss://")) return `https://${wsUrl.slice(6)}`;
  if (wsUrl.startsWith("ws://")) return `http://${wsUrl.slice(5)}`;
  return wsUrl;
}

/** Decode a data: URL (as returned by tabs.captureVisibleTab) into raw bytes. */
export function dataUrlToBytes(dataUrl: string): Uint8Array {
  const base64 = dataUrl.slice(dataUrl.indexOf(",") + 1);
  const binary = atob(base64);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
  return bytes;
}

/** Upload bytes, returning the backend's content-addressed key. */
export async function uploadBlob(
  httpUrl: string,
  secret: string,
  bytes: Uint8Array,
  fetchImpl: typeof fetch = fetch,
): Promise<string> {
  const resp = await fetchImpl(`${httpUrl}/api/blobs`, {
    method: "POST",
    headers: { "X-BB-Secret": secret },
    body: bytes as BodyInit,
  });
  if (!resp.ok) throw new Error(`blob upload failed: ${resp.status}`);
  const json = (await resp.json()) as { key: string };
  return json.key;
}
