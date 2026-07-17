import { expect, test } from "vitest";
import { gapFillVisits, visitFromHistoryItem } from "@src/history";
import { fakeStorageArea } from "./fakes";

test("visitFromHistoryItem converts and rejects unusable items", () => {
  expect(
    visitFromHistoryItem({
      url: "https://a.com",
      title: "A",
      lastVisitTime: 1720000000000.7,
    }),
  ).toEqual({ url: "https://a.com", title: "A", visit_time: 1720000000000 });
  expect(visitFromHistoryItem({ url: "https://a.com" })).toBeNull(); // no time
  expect(visitFromHistoryItem({ lastVisitTime: 1 })).toBeNull(); // no url
  expect(visitFromHistoryItem({ url: "https://a.com", lastVisitTime: 1 })!.title).toBe("");
});

test("gapFillVisits searches since watermark and advances it", async () => {
  const storage = fakeStorageArea();
  await storage.set({ "bb:history_watermark": 500 });
  const calls: unknown[] = [];
  const historyApi = {
    async search(q: { text: string; startTime: number; maxResults: number }) {
      calls.push(q);
      return [
        { url: "https://a.com", title: "A", lastVisitTime: 600 },
        { url: "https://skipme.com" }, // no lastVisitTime → skipped
      ];
    },
  };
  const visits = await gapFillVisits(historyApi, storage, () => 900);
  expect(visits).toEqual([{ url: "https://a.com", title: "A", visit_time: 600 }]);
  expect(calls[0]).toMatchObject({ text: "", startTime: 500 });
  expect((await storage.get("bb:history_watermark"))["bb:history_watermark"]).toBe(900);
});

test("first run uses now as watermark start (no historical flood)", async () => {
  const storage = fakeStorageArea();
  const historyApi = { async search() { return []; } };
  await gapFillVisits(historyApi, storage, () => 12345);
  expect((await storage.get("bb:history_watermark"))["bb:history_watermark"]).toBe(12345);
});
