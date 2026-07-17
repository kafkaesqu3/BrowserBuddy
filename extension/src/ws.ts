import type { EventQueue } from "./queue";
import type { ServerFrame } from "./types";

export type WsStatus = "disconnected" | "connecting" | "authed";

/** The subset of WebSocket we use (injectable for tests). */
export interface WsLike {
  send(data: string): void;
  close(): void;
  onopen: (() => void) | null;
  onmessage: ((ev: { data: string }) => void) | null;
  onclose: (() => void) | null;
}

export interface WsClientOptions {
  url: string;
  secret: string;
  clientId: string;
  queue: EventQueue;
  makeSocket: (url: string) => WsLike;
  heartbeatMs?: number;
  reconnectBaseMs?: number;
  reconnectMaxMs?: number;
  onStatus?: (status: WsStatus) => void;
}

export class WsClient {
  status: WsStatus = "disconnected";

  private socket: WsLike | null = null;
  // FIFO of what we've sent and not yet had answered. The server replies
  // strictly in order (ack/error per envelope, pong per ping), so the head
  // of this list is always what the next reply refers to.
  private inflight: (number | "ping")[] = [];
  private sentUpTo = 0;
  private attempts = 0;
  private stopped = false;
  private heartbeat: ReturnType<typeof setInterval> | undefined;
  private reconnectTimer: ReturnType<typeof setTimeout> | undefined;

  constructor(private opts: WsClientOptions) {}

  connect(): void {
    clearTimeout(this.reconnectTimer);
    if (this.socket || this.stopped) return;
    this.setStatus("connecting");
    let socket: WsLike;
    try {
      // e.g. `new WebSocket(url)` throws synchronously on a malformed
      // user-edited URL; treat it like a connection failure so status never
      // wedges at "connecting" and the backoff retry still runs.
      socket = this.opts.makeSocket(this.opts.url);
    } catch (err) {
      console.warn("BrowserBuddy: cannot open WebSocket (bad URL?)", err);
      this.handleClose(); // status disconnected + backoff retry
      return;
    }
    this.socket = socket;
    socket.onopen = () => {
      socket.send(
        JSON.stringify({ type: "auth", secret: this.opts.secret, client_id: this.opts.clientId }),
      );
    };
    socket.onmessage = (ev) => void this.handleFrame(JSON.parse(ev.data) as ServerFrame);
    socket.onclose = () => this.handleClose();
  }

  /** Stop for good (settings changed / extension shutdown). */
  disconnect(): void {
    this.stopped = true;
    clearTimeout(this.reconnectTimer);
    this.teardownSocket();
    this.setStatus("disconnected");
  }

  async notifyEnqueued(): Promise<void> {
    if (this.status === "authed") await this.drain();
  }

  private async handleFrame(frame: ServerFrame): Promise<void> {
    switch (frame.type) {
      case "auth_ok": {
        this.attempts = 0;
        this.inflight = [];
        this.sentUpTo = frame.last_seq;
        await this.opts.queue.ack(frame.last_seq);
        this.setStatus("authed");
        this.startHeartbeat();
        await this.drain();
        break;
      }
      case "ack": {
        this.inflight.shift();
        await this.opts.queue.ack(frame.seq);
        break;
      }
      case "error": {
        const head = this.inflight.shift();
        console.warn("BrowserBuddy: server rejected event", head, frame.detail);
        if (typeof head === "number") await this.opts.queue.drop(head);
        break;
      }
      case "pong": {
        this.inflight.shift();
        break;
      }
      default: {
        // frame is `never` here per ServerFrame, but the wire can carry
        // anything; log and ignore without touching the inflight FIFO.
        console.warn("BrowserBuddy: unknown server frame", frame satisfies never);
        break;
      }
    }
  }

  private async drain(): Promise<void> {
    const socket = this.socket;
    if (!socket || this.status !== "authed") return;
    const pending = await this.opts.queue.pending();
    // Revalidate: a reconnect may have replaced the socket while we awaited.
    // Sending on the dead socket would push a phantom inflight entry and bump
    // sentUpTo, making the new connection skip the event and misattribute the
    // next server error frame.
    if (this.socket !== socket || this.status !== "authed") return;
    for (const envelope of pending) {
      if (envelope.seq <= this.sentUpTo) continue;
      socket.send(JSON.stringify(envelope));
      this.inflight.push(envelope.seq);
      this.sentUpTo = envelope.seq;
    }
  }

  private startHeartbeat(): void {
    clearInterval(this.heartbeat);
    // setInterval, not alarms: sub-30s periods aren't allowed for alarms, and
    // the interval only needs to live as long as the socket does — WS traffic
    // is itself what keeps the MV3 worker alive.
    this.heartbeat = setInterval(() => {
      if (!this.socket) return; // never push a marker without a matching send
      this.socket.send(JSON.stringify({ type: "ping" }));
      this.inflight.push("ping");
    }, this.opts.heartbeatMs ?? 20_000);
  }

  private handleClose(): void {
    this.teardownSocket();
    this.setStatus("disconnected");
    if (this.stopped) return;
    const base = this.opts.reconnectBaseMs ?? 1000;
    const max = this.opts.reconnectMaxMs ?? 60_000;
    const delay = Math.min(base * 2 ** this.attempts, max);
    this.attempts += 1;
    this.reconnectTimer = setTimeout(() => this.connect(), delay);
  }

  private teardownSocket(): void {
    clearInterval(this.heartbeat);
    if (this.socket) {
      this.socket.onclose = null; // avoid re-entrant handleClose from close()
      this.socket.onmessage = null; // late frames must not touch successor state
      this.socket.onopen = null;
      this.socket.close();
      this.socket = null;
    }
    this.inflight = [];
  }

  private setStatus(status: WsStatus): void {
    this.status = status;
    this.opts.onStatus?.(status);
  }
}
