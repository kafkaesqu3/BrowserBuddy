import { extractPage } from "./extract";

// Content script: runs only while a session is recording (registered
// dynamically by the background on session start). Extracts once the page
// settles and reports home. SPA recapture lands in M3b.
(() => {
  const send = () => {
    const page = extractPage(document);
    void (globalThis as { browser?: typeof browser }).browser
      ?.runtime.sendMessage({ type: "bb:page", url: location.href, ...page })
      .catch(() => undefined); // background asleep/unreachable: drop silently
  };
  if (document.readyState === "complete") {
    setTimeout(send, 500);
  } else {
    addEventListener("load", () => setTimeout(send, 500), { once: true });
  }
})();
