interface SessionView {
  name: string;
  startedAt: number;
  status: "recording" | "paused";
}
interface SessionReply {
  session: SessionView | null;
  connection: string;
}

const el = <T extends HTMLElement>(id: string): T =>
  document.querySelector<T>(`#${id}`)!;

const idle = el<HTMLDivElement>("idle");
const active = el<HTMLDivElement>("active");
const nameInput = el<HTMLInputElement>("name");
const sessionEl = el<HTMLDivElement>("session");
const pauseBtn = el<HTMLButtonElement>("pause");
const resumeBtn = el<HTMLButtonElement>("resume");
const connectionEl = el<HTMLDivElement>("connection");
const screenshotsEnabledInput = el<HTMLInputElement>("screenshots-enabled");
const screenshotIntervalInput = el<HTMLInputElement>("screenshot-interval");
const captureNowBtn = el<HTMLButtonElement>("capture-now");

function captureOptions(): { screenshotsEnabled: boolean; screenshotIntervalMs: number } {
  const seconds = Math.max(1, Number(screenshotIntervalInput.value) || 30);
  return {
    screenshotsEnabled: screenshotsEnabledInput.checked,
    screenshotIntervalMs: seconds * 1000,
  };
}

function render(reply: SessionReply): void {
  const s = reply.session;
  idle.classList.toggle("hidden", s !== null);
  active.classList.toggle("hidden", s === null);
  if (s) {
    sessionEl.textContent =
      s.status === "recording" ? `● Recording: ${s.name}` : `❚❚ Paused: ${s.name}`;
    sessionEl.className = s.status;
    pauseBtn.classList.toggle("hidden", s.status !== "recording");
    resumeBtn.classList.toggle("hidden", s.status !== "paused");
  }
  connectionEl.textContent = `Backend: ${reply.connection}`;
}

async function command(action: string, name?: string): Promise<void> {
  try {
    const reply = (await browser.runtime.sendMessage({
      type: "bb:session",
      action,
      name,
      ...captureOptions(),
    })) as SessionReply;
    render(reply);
  } catch {
    connectionEl.textContent = "Backend: background unavailable";
  }
}

el<HTMLButtonElement>("start").addEventListener("click", () => {
  void (async () => {
    // Firefox MV3: host permissions are optional; capture needs them. Must
    // be requested from this user gesture — the background can't ask.
    const granted = await browser.permissions.request({ origins: ["<all_urls>"] });
    if (!granted) {
      connectionEl.textContent = "Capture needs page access to record sessions.";
      return;
    }
    await command("start", nameInput.value.trim() || "untitled session");
  })();
});
el<HTMLButtonElement>("pause").addEventListener("click", () => void command("pause"));
el<HTMLButtonElement>("resume").addEventListener("click", () => void command("resume"));
el<HTMLButtonElement>("stop").addEventListener("click", () => void command("stop"));

void command("status");
setInterval(() => void command("status"), 2000);

el<HTMLAnchorElement>("settings").addEventListener("click", (e) => {
  // The shared-secret field lives on the options page; the popup is the
  // primary surface, so first-time pairing must be reachable from here.
  e.preventDefault();
  void browser.runtime.openOptionsPage();
});

// Toggling/changing screenshot settings while a session may already be
// recording applies immediately — the "start" fields double as the live
// controls (a no-op if idle, since update-capture requires an active
// session; see sessions.ts).
screenshotsEnabledInput.addEventListener("change", () => void command("update-capture"));
screenshotIntervalInput.addEventListener("change", () => void command("update-capture"));

captureNowBtn.addEventListener("click", () => {
  void browser.runtime.sendMessage({ type: "bb:capture-fullpage" });
});
