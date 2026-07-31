import { readFileSync } from "node:fs";
import { expect, test } from "vitest";

const firefoxOverlay = JSON.parse(
  readFileSync(new URL("../manifest.firefox.json", import.meta.url), "utf8"),
) as { content_security_policy?: { extension_pages?: string } };

const baseManifest = JSON.parse(
  readFileSync(new URL("../manifest.base.json", import.meta.url), "utf8"),
) as { host_permissions?: string[]; optional_host_permissions?: string[] };

const chromeOverlay = JSON.parse(
  readFileSync(new URL("../manifest.chrome.json", import.meta.url), "utf8"),
) as { permissions?: string[] };

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

test("<all_urls> is a runtime-grantable optional host permission, not install-time", () => {
  // The popup requests <all_urls> at the Start gesture via
  // permissions.request(). In MV3, permissions.request() can only grant
  // origins declared in optional_host_permissions — an origin sitting in
  // host_permissions is NOT runtime-requestable, so the request fails and
  // session_start is never enqueued (tabs still sync; sessions never do).
  expect(baseManifest.optional_host_permissions).toEqual(["<all_urls>"]);
  expect(baseManifest.host_permissions).toBeUndefined();
});

test("webNavigation is granted on both targets (nav-edge capture)", () => {
  const ff = firefoxOverlay as unknown as { permissions?: string[] };
  expect(ff.permissions).toContain("webNavigation");
  expect(chromeOverlay.permissions).toContain("webNavigation");
});
