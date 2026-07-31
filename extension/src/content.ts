import { extractLinks, extractPage } from "./extract";

// Content script: runs only while a session is recording (registered
// dynamically by the background on session start). Extracts the page + its
// outbound-link catalogue once the page settles, and reports every anchor
// click (including non-navigating ones the background's webNavigation cannot
// see: downloads, JS handlers, new-tab opens).
(() => {
  const runtime = (globalThis as { browser?: typeof browser }).browser?.runtime;

  const send = () => {
    const page = extractPage(document);
    const links = extractLinks(document);
    void runtime
      ?.sendMessage({ type: "bb:page", url: location.href, ...page, links })
      .catch(() => undefined); // background asleep/unreachable: drop silently
  };

  const onClick = (event: MouseEvent) => {
    const target = event.target as Element | null;
    const anchor = target?.closest?.("a[href]") as HTMLAnchorElement | null;
    if (!anchor || !/^https?:/i.test(anchor.href)) return;
    void runtime
      ?.sendMessage({
        type: "bb:click",
        page_url: location.href,
        href: anchor.href,
        text: (anchor.textContent ?? "").replace(/\s+/g, " ").trim().slice(0, 300),
      })
      .catch(() => undefined);
  };
  // Capture phase so we still see the click if a handler calls stopPropagation;
  // auxclick covers middle-click "open in new tab".
  addEventListener("click", onClick, true);
  addEventListener("auxclick", onClick, true);

  if (document.readyState === "complete") {
    setTimeout(send, 500);
  } else {
    addEventListener("load", () => setTimeout(send, 500), { once: true });
  }
})();
