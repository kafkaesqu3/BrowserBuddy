import type { EventPayload, StorageArea } from "./types";

export interface CaptureOptions {
  screenshotsEnabled: boolean;
  screenshotIntervalMs: number;
}

const DEFAULT_CAPTURE: CaptureOptions = { screenshotsEnabled: false, screenshotIntervalMs: 30_000 };

export interface SessionState extends CaptureOptions {
  name: string;
  startedAt: number;
  status: "recording" | "paused";
}

const KEY = "bb:session";

/** Explicit research-session state. Persisted in storage.local so a session
 * survives worker death AND browser restart (deliberate: the user said
 * "record" — only the user saying stop ends it). Pause/resume are
 * client-side only: a paused session sends no page captures; the backend
 * session stays open. Mutations are mutex-serialized like the other
 * storage-backed modules. */
export class SessionManager {
  private mutex: Promise<unknown> = Promise.resolve();

  constructor(
    private local: StorageArea,
    private now: () => number,
  ) {}

  private serialize<T>(fn: () => Promise<T>): Promise<T> {
    const next = this.mutex.then(fn, fn);
    this.mutex = next.catch(() => undefined);
    return next;
  }

  async current(): Promise<SessionState | null> {
    const stored = (await this.local.get(KEY))[KEY] as SessionState | undefined;
    return stored ?? null;
  }

  start(name: string, capture: CaptureOptions = DEFAULT_CAPTURE): Promise<{ event: EventPayload }> {
    return this.serialize(async () => {
      const startedAt = this.now();
      const state: SessionState = { name, startedAt, status: "recording", ...capture };
      await this.local.set({ [KEY]: state });
      // Starting over an active session is fine: the backend implicitly
      // closes the previous session at the new start time.
      return { event: { type: "session_start", name, started_at: startedAt } };
    });
  }

  /** Change the screenshot toggle/interval for the active session, mid-
   * recording — independently of pause/resume, and a no-op with no active
   * session. */
  updateCapture(capture: CaptureOptions): Promise<void> {
    return this.serialize(async () => {
      const state = await this.current();
      if (state) await this.local.set({ [KEY]: { ...state, ...capture } });
    });
  }

  pause(): Promise<void> {
    return this.serialize(async () => {
      const state = await this.current();
      if (state?.status === "recording") {
        await this.local.set({ [KEY]: { ...state, status: "paused" } });
      }
    });
  }

  resume(): Promise<void> {
    return this.serialize(async () => {
      const state = await this.current();
      if (state?.status === "paused") {
        await this.local.set({ [KEY]: { ...state, status: "recording" } });
      }
    });
  }

  /** Stop the active session. Durability-first: `commit` (which enqueues the
   * session_end event) runs BEFORE local state is cleared, so a throwing
   * commit leaves the session intact for a retry — otherwise a failed enqueue
   * would strand the backend session open. Returns false when none is active
   * (idempotent). Backend end_session is idempotent, so a duplicate
   * session_end after a partial failure is a harmless no-op. The `commit`
   * callback MUST reject on failure — a swallowed error would clear state on a
   * lost event and defeat this guarantee. */
  stop(commit: (event: EventPayload) => Promise<void>): Promise<boolean> {
    return this.serialize(async () => {
      const state = await this.current();
      if (!state) return false;
      await commit({ type: "session_end", ended_at: this.now() });
      await this.local.remove(KEY);
      return true;
    });
  }
}
