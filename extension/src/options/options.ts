import { loadSettings, saveSettings } from "@src/settings";
import type { StorageArea } from "@src/types";

const local = browser.storage.local as unknown as StorageArea;
const urlInput = document.querySelector<HTMLInputElement>("#url")!;
const secretInput = document.querySelector<HTMLInputElement>("#secret")!;
const statusEl = document.querySelector<HTMLElement>("#status")!;

async function init(): Promise<void> {
  const settings = await loadSettings(local);
  urlInput.value = settings.backendUrl;
  secretInput.value = settings.secret;
}

document.querySelector("#save")!.addEventListener("click", () => {
  saveSettings(local, {
    backendUrl: urlInput.value.trim(),
    secret: secretInput.value.trim(),
  })
    .then(() => {
      statusEl.textContent = "Saved. Reconnecting…";
      statusEl.className = "";
    })
    .catch((e) => {
      console.warn("BrowserBuddy: save failed", e);
      statusEl.textContent = "Save failed — check the console.";
      statusEl.className = "disconnected";
    });
});

async function refreshStatus(): Promise<void> {
  try {
    const reply = (await browser.runtime.sendMessage("bb:status")) as
      | { status?: string }
      | undefined;
    const status = reply?.status ?? "unknown";
    statusEl.textContent = `Status: ${status}`;
    statusEl.className = status;
  } catch {
    statusEl.textContent = "Status: background unavailable";
    statusEl.className = "";
  }
}

void init();
void refreshStatus();
setInterval(() => void refreshStatus(), 2000);
