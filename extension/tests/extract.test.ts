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

test("never throws on a pathological document", () => {
  const result = extractPage(loadDocument("<body></body>"));
  expect(result.text).toBe("");
  expect(typeof result.html).toBe("string");
});
