import { expect, test } from "vitest";
import { MAX_HTML_CHARS, MAX_LINKS, linkClickEvent, pageCaptureEvent } from "@src/capture";

const msg = (over: Record<string, unknown> = {}) => ({
  type: "bb:page",
  url: "https://a.com/article",
  title: "An article",
  text: "body text",
  html: "<html><body>body text</body></html>",
  ...over,
});

test("converts a content-script message to a page_capture event", () => {
  const event = pageCaptureEvent(msg(), "Research", () => 1_000_000);
  expect(event).toEqual({
    type: "page_capture",
    url: "https://a.com/article",
    title: "An article",
    visited_at: 1_000_000,
    text: "body text",
    html: "<html><body>body text</body></html>",
    container: "Research",
    links: [],
  });
});

test("rejects messages without a usable url or text", () => {
  expect(pageCaptureEvent(msg({ url: "" }), null, () => 1)).toBeNull();
  expect(pageCaptureEvent(msg({ url: undefined }), null, () => 1)).toBeNull();
  expect(pageCaptureEvent(msg({ text: "", html: "" }), null, () => 1)).toBeNull();
});

test("oversized html is dropped but the text capture survives", () => {
  const event = pageCaptureEvent(
    msg({ html: "x".repeat(MAX_HTML_CHARS + 1) }),
    null,
    () => 1,
  );
  expect(event).not.toBeNull();
  expect(event!.html).toBeNull();
  expect(event!.text).toBe("body text");
});

test("defaults: missing title becomes empty string", () => {
  const event = pageCaptureEvent(msg({ title: undefined }), null, () => 1);
  expect(event!.title).toBe("");
});

test("pageCaptureEvent carries outbound links, capped at MAX_LINKS", () => {
  const many = Array.from({ length: MAX_LINKS + 5 }, (_, i) => ({
    href: `https://x.com/${i}`, text: "t", rel: "",
  }));
  const event = pageCaptureEvent(msg({ links: many }), null, () => 1);
  expect(event!.links).toHaveLength(MAX_LINKS);
  expect(event!.links[0]!.href).toBe("https://x.com/0");
});

test("pageCaptureEvent defaults links to [] when absent", () => {
  const event = pageCaptureEvent(msg(), null, () => 1);
  expect(event!.links).toEqual([]);
});

test("linkClickEvent converts a bb:click message", () => {
  const event = linkClickEvent(
    { type: "bb:click", page_url: "https://a.com", href: "https://x.com", text: "X" },
    () => 1_000,
  );
  expect(event).toEqual({
    type: "link_click", page_url: "https://a.com", href: "https://x.com",
    text: "X", clicked_at: 1_000,
  });
});

test("linkClickEvent rejects messages missing page_url or href", () => {
  expect(linkClickEvent({ page_url: "", href: "https://x.com" }, () => 1)).toBeNull();
  expect(linkClickEvent({ page_url: "https://a.com", href: undefined }, () => 1)).toBeNull();
});
