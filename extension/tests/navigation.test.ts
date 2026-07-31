import { expect, test } from "vitest";
import { navEdgeEvent, searchProvenance } from "@src/navigation";

test("searchProvenance extracts the query for known engines", () => {
  expect(searchProvenance("https://www.google.com/search?q=vector+db")).toBe("vector db");
  expect(searchProvenance("https://duckduckgo.com/?q=hnsw")).toBe("hnsw");
  expect(searchProvenance("https://search.yahoo.com/search?p=faiss")).toBe("faiss");
  expect(searchProvenance("https://yandex.com/search/?text=annoy")).toBe("annoy");
});

test("searchProvenance returns null for non-search or malformed urls", () => {
  expect(searchProvenance("https://arxiv.org/abs/1")).toBeNull();
  expect(searchProvenance("https://www.google.com/maps")).toBeNull(); // no q
  expect(searchProvenance("not a url")).toBeNull();
  expect(searchProvenance(null)).toBeNull();
});

test("navEdgeEvent builds an event with joined qualifiers and derived query", () => {
  const event = navEdgeEvent({
    fromUrl: "https://www.bing.com/search?q=rag",
    toUrl: "https://a.com",
    transition: "link",
    qualifiers: ["client_redirect", "from_address_bar"],
    occurredAt: 42,
  });
  expect(event).toEqual({
    type: "nav_edge",
    from_url: "https://www.bing.com/search?q=rag",
    to_url: "https://a.com",
    transition: "link",
    qualifiers: "client_redirect,from_address_bar",
    query: "rag",
    occurred_at: 42,
  });
});

test("navEdgeEvent drops self-edges and missing targets", () => {
  expect(navEdgeEvent({ fromUrl: "https://a.com", toUrl: "https://a.com", occurredAt: 1 })).toBeNull();
  expect(navEdgeEvent({ fromUrl: null, toUrl: "", occurredAt: 1 })).toBeNull();
});
