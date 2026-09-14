import { expect, test, vi } from "vitest";
import { dataUrlToBytes, toHttpUrl, uploadBlob } from "@src/blobs";

test("toHttpUrl converts ws to http", () => {
  expect(toHttpUrl("ws://127.0.0.1:8765/ws")).toBe("http://127.0.0.1:8765/ws");
});

test("toHttpUrl converts wss to https", () => {
  expect(toHttpUrl("wss://example.com/ws")).toBe("https://example.com/ws");
});

test("toHttpUrl leaves a non-ws url unchanged", () => {
  expect(toHttpUrl("http://already-http.com")).toBe("http://already-http.com");
});

test("dataUrlToBytes decodes the base64 payload", () => {
  // "AQID" base64-decodes to bytes [1, 2, 3]
  const bytes = dataUrlToBytes("data:image/jpeg;base64,AQID");
  expect(Array.from(bytes)).toEqual([1, 2, 3]);
});

test("uploadBlob posts bytes with the secret header and returns the key", async () => {
  const fetchImpl = vi.fn(async () => ({
    ok: true,
    json: async () => ({ key: "abc123" }),
  })) as unknown as typeof fetch;
  const bytes = new Uint8Array([1, 2, 3]);
  const key = await uploadBlob("http://127.0.0.1:8765", "s3cret", bytes, fetchImpl);
  expect(key).toBe("abc123");
  const [url, init] = (fetchImpl as ReturnType<typeof vi.fn>).mock.calls[0] as [string, RequestInit];
  expect(url).toBe("http://127.0.0.1:8765/api/blobs");
  expect(init.method).toBe("POST");
  expect(init.body).toBe(bytes);
  expect((init.headers as Record<string, string>)["X-BB-Secret"]).toBe("s3cret");
});

test("uploadBlob throws on a non-ok response", async () => {
  const fetchImpl = vi.fn(async () => ({ ok: false, status: 401 })) as unknown as typeof fetch;
  await expect(uploadBlob("http://x", "s", new Uint8Array(), fetchImpl)).rejects.toThrow("401");
});
