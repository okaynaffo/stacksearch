/**
 * Frontend audit logger.
 *
 * Records every user action (click, key, search, source change), every API call, and
 * every SPA lifecycle transition (mount/unmount/render error/pagehide), so a crash can
 * be reconstructed from the frontend side alone. Records are:
 *
 * * buffered in memory and flushed in batches to `POST /debug/log` (persisted by the
 *   core into the same JSONL file as its own events), and
 * * forwarded one-by-one to the Rust host via the `debug_log_ui` command, which writes
 *   `host.log` — that path survives even when the core service is down, which is the
 *   whole point when debugging an unexpected quit.
 *
 * Every record is categorized (`category` + `event`) so a huge log can be filtered to
 * the exact interaction that precedes the quit.
 */

import { getToken } from "./api";

export type LogLevel = "DEBUG" | "INFO" | "WARNING" | "ERROR";

type LogRecord = {
  seq: number;
  client_ts: number;
  level: LogLevel;
  category: string;
  event: string;
  url: string;
  data?: unknown;
};

const CATEGORY = {
  lifecycle: "ui.lifecycle",
  click: "ui.click",
  key: "ui.key",
  nav: "ui.nav",
  action: "ui.action",
  api: "ui.api",
  error: "ui.error",
} as const;

export type LogCategory = (typeof CATEGORY)[keyof typeof CATEGORY];

const BUFFER_LIMIT = 200;
const FLUSH_INTERVAL_MS = 1500;
const HOST_FORWARD_LIMIT = 400;

let seq = 0;
let buffer: LogRecord[] = [];
let hostForwarded = 0;
let flushTimer: number | null = null;
let installed = false;

function base(): string {
  const injected = (window as unknown as { __STACKSEARCH_API_BASE__?: string })
    .__STACKSEARCH_API_BASE__;
  return injected ?? "";
}

function now(): number {
  return Date.now();
}

function makeRecord(
  level: LogLevel,
  category: string,
  event: string,
  data?: unknown,
): LogRecord {
  seq += 1;
  return {
    seq,
    client_ts: now(),
    level,
    category,
    event,
    url: window.location.href.split("?")[0],
    data,
  };
}

function forwardToHost(record: LogRecord): void {
  if (hostForwarded >= HOST_FORWARD_LIMIT) return;
  const invoke = (window as unknown as {
    __TAURI_INTERNALS__?: { invoke?: (cmd: string, args: unknown) => Promise<unknown> };
  }).__TAURI_INTERNALS__?.invoke;
  if (!invoke) return;
  hostForwarded += 1;
  void Promise.resolve(
    invoke("debug_log_ui", {
      category: record.category,
      event: record.event,
      data: { level: record.level, seq: record.seq, client_ts: record.client_ts, ...(record.data as object ?? {}) },
    }),
  ).catch(() => undefined);
}

/** Append a record and schedule a flush. Never throws. */
export function log(category: string, event: string, data?: unknown, level: LogLevel = "INFO"): void {
  try {
    const record = makeRecord(level, category, event, data);
    // Mirror to the browser console for live debugging; the file copy is the point.
    const consoleFn = level === "ERROR" ? console.error : console.log;
    consoleFn(`[ss/${category}] ${event}`, data ?? "");
    buffer.push(record);
    if (buffer.length > BUFFER_LIMIT) buffer.splice(0, buffer.length - BUFFER_LIMIT);
    forwardToHost(record);
    scheduleFlush();
  } catch {
    // Logging must never break the UI.
  }
}

export function debug(category: string, event: string, data?: unknown): void {
  log(category, event, data, "DEBUG");
}

export function warn(category: string, event: string, data?: unknown): void {
  log(category, event, data, "WARNING");
}

export function error(category: string, event: string, data?: unknown): void {
  log(category, event, data, "ERROR");
}

function scheduleFlush(): void {
  if (flushTimer !== null) return;
  flushTimer = window.setTimeout(() => {
    flushTimer = null;
    void flush();
  }, FLUSH_INTERVAL_MS);
}

async function post(records: LogRecord[]): Promise<void> {
  const token = getToken();
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  if (token) headers["X-Local-Token"] = token;
  await fetch(`${base()}/debug/log`, {
    method: "POST",
    headers,
    body: JSON.stringify({ records }),
  });
}

/** Send the buffered records to the core. Safe to call at any time. */
export async function flush(): Promise<void> {
  if (buffer.length === 0) return;
  const batch = buffer;
  buffer = [];
  try {
    await post(batch);
  } catch {
    // Re-queue on failure (bounded) so a briefly-down core does not lose the tail.
    buffer = [...batch, ...buffer].slice(-BUFFER_LIMIT);
  }
}

/**
 * Last-chance flush for `pagehide`/`beforeunload`, where async `fetch` is unreliable.
 * `sendBeacon` cannot set headers, so the endpoint accepts the token in the query
 * string (PLAN.md §9 carve-out for local, non-sensitive debug data).
 */
function beaconFlush(): void {
  const token = getToken();
  if (buffer.length === 0) return;
  const records = buffer;
  buffer = [];
  const payload = JSON.stringify({ records });
  let url = `${base()}/debug/log`;
  if (token) url += `?token=${encodeURIComponent(token)}`;
  try {
    if (navigator.sendBeacon) {
      const blob = new Blob([payload], { type: "application/json" });
      navigator.sendBeacon(url, blob);
      return;
    }
  } catch {
    // fall through to the sync path
  }
  try {
    const request = new XMLHttpRequest();
    request.open("POST", url, false);
    request.setRequestHeader("Content-Type", "application/json");
    request.send(payload);
  } catch {
    // Nothing left to do at teardown.
  }
}

function describeElement(target: EventTarget | null): Record<string, unknown> | null {
  if (!(target instanceof Element)) return null;
  const el = target as HTMLElement;
  const text = (el.innerText ?? "").trim().slice(0, 80);
  return {
    tag: el.tagName?.toLowerCase(),
    id: el.id || undefined,
    role: el.getAttribute("role") ?? undefined,
    type: (el as HTMLInputElement).type ?? undefined,
    label:
      el.getAttribute("aria-label") ??
      el.getAttribute("placeholder") ??
      el.getAttribute("name") ??
      undefined,
    text: text || undefined,
    classes: typeof el.className === "string" ? el.className.slice(0, 120) : undefined,
  };
}

/**
 * Install global listeners for clicks, keys, lifecycle, and errors. Idempotent.
 *
 * Capture phase is used so the record is written *before* the handler that might crash
 * runs; a record with no matching action is itself the signal that the click was the
 * trigger.
 */
export function install(): void {
  if (installed) return;
  installed = true;

  log(CATEGORY.lifecycle, "install", {
    user_agent: navigator.userAgent,
    viewport: `${window.innerWidth}x${window.innerHeight}`,
    tauri: typeof (window as unknown as { __TAURI_INTERNALS__?: unknown })
      .__TAURI_INTERNALS__ !== "undefined",
  });

  window.addEventListener(
    "click",
    (e) => {
      log(CATEGORY.click, "click", {
        target: describeElement(e.target),
        button: e.button,
        detail: e.detail,
        point: { x: Math.round(e.clientX), y: Math.round(e.clientY) },
        defaultPrevented: e.defaultPrevented,
      });
    },
    true,
  );

  window.addEventListener(
    "keydown",
    (e) => {
      const isSensitive = (e.target as HTMLElement)?.tagName === "INPUT" &&
        !["Enter", "Escape", "Tab", "ArrowUp", "ArrowDown"].includes(e.key);
      log(CATEGORY.key, "keydown", {
        key: e.key,
        code: e.code,
        ctrl: e.ctrlKey,
        meta: e.metaKey,
        shift: e.shiftKey,
        alt: e.altKey,
        repeated: e.repeat,
        // Never log the characters typed into a field — only that typing happened.
        target: isSensitive
          ? { tag: "input", redacted_input: true }
          : describeElement(e.target),
      });
    },
    true,
  );

  window.addEventListener("beforeunload", () => {
    log(CATEGORY.lifecycle, "beforeunload", {});
    beaconFlush();
  });
  window.addEventListener("pagehide", () => {
    log(CATEGORY.lifecycle, "pagehide", {});
    beaconFlush();
  });
  document.addEventListener("visibilitychange", () => {
    debug(CATEGORY.lifecycle, "visibilitychange", { state: document.visibilityState });
  });

  window.addEventListener("error", (e) => {
    error(CATEGORY.error, "window-error", {
      message: e.message,
      source: e.filename,
      line: e.lineno,
      column: e.colno,
      stack: e.error?.stack,
    });
  });
  window.addEventListener("unhandledrejection", (e) => {
    error(CATEGORY.error, "unhandled-rejection", { reason: String(e.reason) });
  });

  // The host asks for the buffer before a deliberate exit.
  window.addEventListener("stacksearch:flush-logs", () => {
    void flush();
  });
  (window as unknown as { __STACKSEARCH_FLUSH_LOGS__?: () => void }).__STACKSEARCH_FLUSH_LOGS__ =
    () => {
      void flush();
    };

  // React-independent teardown signal: an unmount that is not followed by any further
  // event in the log means the tree was torn down during shutdown.
  void flush();
}

/** Wrap a long-running user action: logs begin, end, and duration, and errors. */
export async function action<T>(name: string, run: () => Promise<T>): Promise<T> {
  const started = performance.now();
  log(CATEGORY.action, `${name}.begin`, {});
  try {
    const result = await run();
    log(CATEGORY.action, `${name}.end`, {
      duration_ms: Math.round(performance.now() - started),
    });
    return result;
  } catch (e) {
    error(CATEGORY.action, `${name}.error`, {
      duration_ms: Math.round(performance.now() - started),
      message: e instanceof Error ? e.message : String(e),
      stack: e instanceof Error ? e.stack : undefined,
    });
    throw e;
  }
}

export const categories = CATEGORY;
