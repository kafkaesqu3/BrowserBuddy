// Firefox driver: launches Firefox via geckodriver, installs the unpacked
// BrowserBuddy extension as a temporary (unsigned) add-on, pins its
// moz-extension UUID, and buffers console/JS-error output via WebDriver BiDi.
import webdriver from "selenium-webdriver";
import firefox from "selenium-webdriver/firefox.js";
import createLogInspector from "selenium-webdriver/bidi/logInspector.js";

import {
  EXTENSION_DIR,
  EXTENSION_ID,
  EXTENSION_UUID,
  HEADLESS,
} from "./config.mjs";

const { Builder } = webdriver;

export class FirefoxSession {
  constructor() {
    this.driver = null;
    this.logInspector = null;
    /** @type {{level: string, type: string, text: string, timestamp: number}[]} */
    this.logs = [];
    this.logCaptureError = null;
  }

  get started() {
    return this.driver !== null;
  }

  async start() {
    if (this.driver) return;

    const options = new firefox.Options();
    // Deterministic moz-extension origin for the popup/options URLs.
    options.setPreference(
      "extensions.webextensions.uuids",
      JSON.stringify({ [EXTENSION_ID]: EXTENSION_UUID }),
    );
    // The popup requests the <all_urls> optional host permission on session
    // start via a user-gesture doorhanger. Auto-grant optional permission
    // requests so automation isn't blocked waiting on that prompt.
    options.setPreference("extensions.webextOptionalPermissionPrompts", false);
    // Enable a WebDriver BiDi session so we can subscribe to log events.
    options.enableBidi();
    if (HEADLESS) options.addArguments("-headless");

    // Recent Firefox blocks WebDriver navigation to privileged URLs (including
    // the extension's own moz-extension:// pages). geckodriver's
    // --allow-system-access flag lifts that so we can open the popup/options.
    const service = new firefox.ServiceBuilder().addArguments("--allow-system-access");

    this.driver = await new Builder()
      .forBrowser("firefox")
      .setFirefoxOptions(options)
      .setFirefoxService(service)
      .build();

    // Temporary add-on install is the supported path for unsigned extensions.
    await this.driver.installAddon(EXTENSION_DIR, /* temporary */ true);

    await this.#startLogCapture();
  }

  async #startLogCapture() {
    try {
      this.logInspector = await createLogInspector(this.driver);
      await this.logInspector.onConsoleEntry((entry) => {
        this.logs.push({
          level: entry.level ?? "info",
          type: "console",
          text: entry.text ?? "",
          timestamp: entry.timestamp ?? 0,
        });
      });
      await this.logInspector.onJavascriptException((entry) => {
        this.logs.push({
          level: "error",
          type: "exception",
          text: entry.text ?? entry.exceptionDetails?.text ?? String(entry),
          timestamp: entry.timestamp ?? 0,
        });
      });
    } catch (err) {
      // BiDi log capture is best-effort: if it fails to initialise, the server
      // still drives the browser — get_logs just reports the failure.
      this.logCaptureError = err instanceof Error ? err.message : String(err);
    }
  }

  getLogs() {
    return { entries: this.logs.slice(), captureError: this.logCaptureError };
  }

  clearLogs() {
    this.logs.length = 0;
  }

  async stop() {
    if (this.logInspector) {
      try {
        await this.logInspector.close();
      } catch {
        // ignore — we are shutting down anyway
      }
      this.logInspector = null;
    }
    if (this.driver) {
      try {
        await this.driver.quit();
      } catch {
        // ignore — best-effort teardown
      }
      this.driver = null;
    }
  }
}
