import { expect, test } from "vitest";
import { EventQueue } from "@src/queue";
import { makeContainerResolver, pushTabSnapshot, pushVisit } from "@src/sync";
import { TabTracker } from "@src/tabs";
import { fakeStorageArea } from "./fakes";

function deps() {
  const queue = new EventQueue(fakeStorageArea());
  const tracker = new TabTracker(fakeStorageArea(), () => 1_000_000);
  const notified: number[] = [];
  return {
    queue,
    tracker,
    notified,
    client: { notifyEnqueued: async () => void notified.push(1) },
    containerName: async () => null,
  };
}

test("pushTabSnapshot enqueues a tab_state and notifies the client", async () => {
  const d = deps();
  await pushTabSnapshot({
    ...d,
    tabsQuery: async () => [{ id: 1, windowId: 1, url: "https://a.com", title: "A" }],
  });
  const pending = await d.queue.pending();
  expect(pending).toHaveLength(1);
  expect(pending[0]!.event.type).toBe("tab_state");
  expect(d.notified).toHaveLength(1);
});

test("pushVisit enqueues a history_visits event and skips unusable items", async () => {
  const d = deps();
  await pushVisit(d, { url: "https://a.com", title: "A", lastVisitTime: 1720000000000 });
  await pushVisit(d, { url: "https://no-time.com" }); // skipped
  const pending = await d.queue.pending();
  expect(pending).toHaveLength(1);
  expect(pending[0]!.event).toEqual({
    type: "history_visits",
    visits: [{ url: "https://a.com", title: "A", visit_time: 1720000000000 }],
  });
});

test("makeContainerResolver: names containers, nulls defaults, tolerates absence", async () => {
  const withApi = makeContainerResolver({
    get: async (id: string) => ({ name: id === "firefox-container-1" ? "Research" : "Other" }),
  });
  expect(await withApi("firefox-container-1")).toBe("Research");
  expect(await withApi("firefox-default")).toBeNull();
  expect(await withApi(undefined)).toBeNull();

  const noApi = makeContainerResolver(undefined); // Chrome
  expect(await noApi("anything")).toBeNull();

  const broken = makeContainerResolver({
    get: async () => {
      throw new Error("no such container");
    },
  });
  expect(await broken("firefox-container-9")).toBeNull();
});
