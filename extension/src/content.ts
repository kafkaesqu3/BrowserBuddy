import { delayForStep, textChangedEnough } from "./recapture";
import { extractLinks, extractPage } from "./extract";

// Content script: runs only while a session is recording (registered
// dynamically by the background on session start). Extracts the page + its
// outbound-link catalogue once the page settles, keeps watching for SPA
// content changes (Navigation API + a decaying poll) for as long as the
// document lives, and reports every anchor click (including non-navigating
// ones the background's webNavigation cannot see: downloads, JS handlers,
// new-tab opens).
(() => {
  const runtime = (globalThis as { browser?: typeof browser }).browser?.runtime;
  let lastSentLength = 0;
  let step = 0;
  let timer: ReturnType<typeof setTimeout> | undefined;

  const send = (page = extractPage(document)) => {
    const links = extractLinks(document);
    lastSentLength = page.text.length;
    void runtime
      ?.sendMessage({ type: "bb:page", url: location.href, ...page, links })
      .catch(() => undefined); // background asleep/unreachable: drop silently
  };

  const scheduleRecheck = () => {
    clearTimeout(timer);
    timer = setTimeout(recheck, delayForStep(step));
  };

  // Re-extract and compare to the last *sent* length; resubmit (a fresh
  // page_capture, same shape a reload already produces) only on a material
  // change, resetting the poll schedule to its fastest step so bursty
  // updates keep getting caught quickly. Otherwise let the interval decay.
  const recheck = () => {
    const page = extractPage(document);
    if (textChangedEnough(lastSentLength, page.text.length)) {
      step = 0;
      send(page);
    } else {
      step += 1;
    }
    scheduleRecheck();
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

  const start = () => {
    setTimeout(send, 500);
    scheduleRecheck();
  };
  if (document.readyState === "complete") {
    start();
  } else {
    addEventListener("load", start, { once: true });
  }

  // Navigation API: an immediate recheck trigger for SPA route changes,
  // bypassing the wait for the next poll tick. Cast avoids depending on
  // Navigation API lib types (not universally present); absent on browsers
  // that don't support it — the decaying poll is the fallback.
  const nav = (
    globalThis as { navigation?: { addEventListener: (type: string, cb: () => void) => void } }
  ).navigation;
  nav?.addEventListener("navigatesuccess", () => {
    step = 0;
    clearTimeout(timer);
    timer = setTimeout(recheck, 500); // let the new route settle, like initial load
  });
})();
