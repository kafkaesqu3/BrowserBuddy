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

  // --- Screenshots (Slice C) ------------------------------------------
  // Tick loop: a fixed-cadence heartbeat while this tab is the one
  // background has designated "active." This script has no idea whether
  // screenshots are enabled or what interval is configured — background
  // decides everything (same split as the bb:page/bb:click messages
  // above). Content scripts stay alive for the life of the document,
  // unlike the ephemeral background worker, which is exactly why the
  // cadence lives here rather than a background timer.
  const SCREENSHOT_TICK_MS = 2000;
  let screenshotTimer: ReturnType<typeof setTimeout> | undefined;

  const screenshotTick = (): void => {
    void runtime?.sendMessage({ type: "bb:screenshot-tick" }).catch(() => undefined);
    screenshotTimer = setTimeout(screenshotTick, SCREENSHOT_TICK_MS);
  };

  // Full-page on-demand capture driver: background asks this script to
  // scroll to each viewport-height step and reports back the page's
  // dimensions so background knows how many steps remain; background alone
  // calls captureVisibleTab (content scripts can't).
  runtime?.onMessage.addListener((message, _sender, sendResponse) => {
    const msg = message as { type?: string; action?: "start" | "stop"; index?: number };
    if (msg.type === "bb:screenshot-tick-control") {
      clearTimeout(screenshotTimer);
      if (msg.action === "start") screenshotTimer = setTimeout(screenshotTick, SCREENSHOT_TICK_MS);
      return false;
    }
    if (msg.type === "bb:scroll-step") {
      if (msg.index === -1) {
        window.scrollTo(0, 0); // restore scroll position once the tile sequence is done
        sendResponse({});
        return true;
      }
      window.scrollTo(0, (msg.index ?? 0) * window.innerHeight);
      setTimeout(() => {
        // Let reflow/lazy-loaded images settle before background captures
        // this tile.
        sendResponse({
          scrollHeight: document.documentElement.scrollHeight,
          viewportHeight: window.innerHeight,
        });
      }, 300);
      return true; // async reply
    }
    return false;
  });
})();
