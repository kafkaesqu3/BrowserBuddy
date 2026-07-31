// @vitest-environment happy-dom
import { expect, test } from "vitest";
import { extractPage } from "@src/extract";

function loadDocument(html: string, url = "https://example.com/post"): Document {
  const doc = document.implementation.createHTMLDocument();
  doc.documentElement.innerHTML = html;
  // Readability reads the document URI for relative links; happy-dom lets us fake it.
  Object.defineProperty(doc, "URL", { value: url });
  Object.defineProperty(doc, "documentURI", { value: url });
  return doc;
}

const ARTICLE = `
  <head><title>Test Article</title></head>
  <body>
    <nav>Home | About | Contact</nav>
    <article>
      <h1>Understanding Vector Databases</h1>
      ${"<p>Vector databases store embeddings for similarity search. This paragraph repeats to give Readability enough content to work with confidently.</p>".repeat(8)}
    </article>
    <footer>Copyright</footer>
  </body>`;

test("extracts readable text and raw html from an article page", () => {
  const result = extractPage(loadDocument(ARTICLE));
  expect(result.title).toBe("Test Article");
  expect(result.text).toContain("similarity search");
  expect(result.text).not.toContain("Home | About"); // nav stripped by Readability
  expect(result.html).toContain("<article>"); // raw DOM, not the readable version
});

test("falls back to body text when Readability finds no article", () => {
  const result = extractPage(loadDocument("<body><div>just a fragment</div></body>"));
  expect(result.text).toContain("just a fragment");
  expect(result.html).toContain("just a fragment");
});

test("fallback text excludes script, style, and template content", () => {
  // The div is `hidden` so Readability sees zero visible text and returns
  // null, forcing the fallback path. Without it, Readability grabs the div
  // itself (stripping scripts on the way) and the fallback never runs.
  const result = extractPage(
    loadDocument(
      '<body><div hidden>Dashboard</div><script id="__NEXT_DATA__">{"secret":1}</script>' +
        "<style>.x{color:red}</style><noscript>enable js</noscript></body>",
    ),
  );
  expect(result.text).toBe("Dashboard");
  expect(result.html).toContain("__NEXT_DATA__"); // raw HTML keeps everything
});

test("never throws on a pathological document", () => {
  const result = extractPage(loadDocument("<body></body>"));
  expect(result.text).toBe("");
  expect(typeof result.html).toBe("string");
});

test("extractLinks returns deduped absolute http(s) links with text and rel", async () => {
  const { extractLinks } = await import("@src/extract");
  const doc = loadDocument(`
    <body>
      <a href="https://x.com/a">  First  </a>
      <a href="https://x.com/a">dup</a>
      <a href="https://y.com" rel="nofollow noopener">Y</a>
      <a href="mailto:hi@z.com">mail</a>
      <a>no href</a>
    </body>`);
  const links = extractLinks(doc);
  expect(links.map((l) => l.href)).toEqual(["https://x.com/a", "https://y.com/"]);
  expect(links[0]!.text).toBe("First"); // whitespace collapsed + trimmed
  expect(links[1]!.rel).toContain("nofollow");
});

test("extractLinks caps anchor text length", async () => {
  const { extractLinks } = await import("@src/extract");
  const doc = loadDocument(`<body><a href="https://x.com">${"z".repeat(500)}</a></body>`);
  expect(extractLinks(doc)[0]!.text.length).toBe(300);
});
