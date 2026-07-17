import { readFileSync } from "node:fs";
import { expect, test } from "vitest";

const firefoxOverlay = JSON.parse(
  readFileSync(new URL("../manifest.firefox.json", import.meta.url), "utf8"),
) as { content_security_policy?: { extension_pages?: string } };

test("Firefox CSP override exists and omits upgrade-insecure-requests", () => {
  // Firefox's MV3 DEFAULT extension CSP is "script-src 'self';
  // upgrade-insecure-requests", which silently rewrites the background
  // page's ws://127.0.0.1 to wss:// — the TLS handshake then hits the
  // plaintext backend as "Invalid HTTP request received" and pairing never
  // works. Overriding the CSP without that directive is the documented fix;
  // this test pins it so a manifest refactor can't regress localhost WS.
  const csp = firefoxOverlay.content_security_policy?.extension_pages;
  expect(csp).toBeDefined();
  expect(csp).toContain("script-src 'self'");
  expect(csp).not.toContain("upgrade-insecure-requests");
});
