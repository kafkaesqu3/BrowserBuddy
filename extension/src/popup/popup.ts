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
