/**
 * Typed client for the local core service (PLAN.md §9, §10).
 *
 * §9 requires the per-launch token on every route. The token is delivered by the host —
 * the Tauri side passes it in, and a bare `uv run core.api.run` prints it for the browser
 * dev flow — and is read from the URL once so a refresh keeps working.
 */

import * as logger from "./logger";

export type EmbeddingInfo = {
  key: string;
  id: string;
  dim: number;
  loaded: boolean;
  size_gb: number;
};

export type AnswerLLMInfo = {
  key: string;
  label: string;
  size_gb: number;
  present: boolean;
  path: string;
  reasoning_enabled: boolean;
};

export type AnswerLLMOption = {
  key: string;
  label: string;
  size_gb: number;
  present: boolean;
};

export type Status = {
  app: string;
  version: string;
  phase: number;
  uptime_s: number;
  index: {
    sources: number | null;
    documents: number | null;
    chunks: number | null;
    vectors: number | null;
    jobs_pending: number | null;
    documents_by_status: Record<string, number>;
    embedding_model: string | null;
    embedding_dim: number | null;
    schema_version: number | null;
    vec_version: string | null;
  };
  index_error: string | null;
  index_path: string;
  models: {
    embedding: EmbeddingInfo;
    answer_llm: AnswerLLMInfo;
    answer_llm_options?: AnswerLLMOption[];
    reranker: { key: string; enabled: boolean };
  };
  llm_server: { running: boolean; pid?: number | null; model?: string | null; restarts?: number };
  queue: number;
  logs?: { dir: string; core: string; host: string; faulthandler: string };
  queue_paused_reason?: string | null;
  battery_low?: boolean;
  idle_timeout_minutes?: number | null;
  worker_state?: string;
  events_subscribers: number;
};

/**
 * Resolve the API origin at runtime.
 *
 * The same bundle runs in a browser during Phases 1-5 and inside the Tauri webview in
 * Phase 6, so the origin cannot be baked in at build time.
 *
 * * Normal host: the page is already served by the core service (browser proxy or the
 *   Tauri External-URL webview), so a relative URL hits the right place.
 * * Bundled fallback (the host's failed-start page): the page origin is Tauri's asset
 *   scheme, not the service, so the host injects `__STACKSEARCH_API_BASE__` and we use
 *   that absolute origin instead.
 */
function apiBase(): string {
  const injected = (window as unknown as { __STACKSEARCH_API_BASE__?: string })
    .__STACKSEARCH_API_BASE__;
  return injected ?? "";
}

let cachedToken: string | null = null;
let cachedStartupError: string | null = null;

/** Read the token from the injected global or `?token=`, once, and remember it. */
export function initToken(): string | null {
  if (cachedToken) return cachedToken;
  const params = new URLSearchParams(window.location.search);
  const injectedGlobal = window as unknown as {
    __STACKSEARCH_TOKEN__?: string;
    __STACKSEARCH_STARTUP_ERROR__?: string;
  };
  // The Rust host sets this when the core service failed to start, so the UI can show
  // the real reason instead of a blank window or 401 banners from a foreign service.
  cachedStartupError =
    params.get("startup_error") ?? (injectedGlobal.__STACKSEARCH_STARTUP_ERROR__ || null);
  const fromQuery = params.get("token");
  if (fromQuery) {
    cachedToken = fromQuery;
    return cachedToken;
  }
  // Tauri's Rust side injects the token on the window object.
  cachedToken = injectedGlobal.__STACKSEARCH_TOKEN__ ?? null;
  return cachedToken;
}

export function getToken(): string | null {
  return cachedToken ?? initToken();
}

/** The host-reported startup failure, or `null` when the service came up normally. */
export function getStartupError(): string | null {
  return cachedStartupError ?? new URLSearchParams(window.location.search).get("startup_error");
}

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const token = getToken();
  const headers = new Headers(init.headers);
  if (token) headers.set("X-Local-Token", token);
  if (init.body) headers.set("Content-Type", "application/json");

  const method = init.method ?? "GET";
  // Poll paths are logged at DEBUG so they can be filtered out of a crash timeline
  // without being dropped entirely.
  const pollPath = path === "/status" || path === "/health";
  const started = performance.now();
  const emit = pollPath ? logger.debug : logger.log;
  emit(logger.categories.api, "request", { method, path, body: init.body ?? undefined });

  let resp: Response;
  try {
    resp = await fetch(`${apiBase()}${path}`, { ...init, headers });
  } catch (e) {
    logger.error(logger.categories.api, "network-error", {
      method,
      path,
      duration_ms: Math.round(performance.now() - started),
      message: e instanceof Error ? e.message : String(e),
    });
    throw e;
  }

  emit(logger.categories.api, "response", {
    method,
    path,
    status: resp.status,
    duration_ms: Math.round(performance.now() - started),
  });

  if (resp.status === 401) {
    throw new ApiError(
      "Unauthorized. The per-launch token is required on every route (PLAN.md §9). " +
        "Pass it as ?token=... or an X-Local-Token header.",
      401,
    );
  }
  if (!resp.ok) {
    throw new ApiError(`${resp.status} ${resp.statusText} on ${path}`, resp.status);
  }
  return (await resp.json()) as T;
}

export function fetchStatus(): Promise<Status> {
  return request<Status>("/status");
}

export type SearchResult = {
  chunk_id: number;
  document_id: number;
  source_kind: string;
  title: string | null;
  author: string | null;
  date: string | null;
  snippet: string;
  uri: string;
  loc: Record<string, unknown>;
  score: number;
  signals: Record<string, number>;
};

export type SearchResponse = {
  results: SearchResult[];
  latency_ms: number;
  warnings?: string[];
};

export type QueryFilters = {
  source_kind?: string;
  author?: string;
  date_from?: string;
  date_to?: string;
};

export function postSearch(
  query: string,
  opts: { limit?: number; filters?: QueryFilters } = {},
): Promise<SearchResponse> {
  const { limit = 20, filters } = opts;
  return request<SearchResponse>("/search", {
    method: "POST",
    body: JSON.stringify({ query, limit, filters }),
  });
}

// --------------------------------------------------------------------------------
// Grounded answers (PLAN.md §10 POST /answer, TODO 4.2/4.3)
// --------------------------------------------------------------------------------

export type AnswerSource = {
  n: number;
  chunk_id: number | null;
  document_id: number | null;
  source_kind: string | null;
  title: string | null;
  author: string | null;
  date: string | null;
  uri: string | null;
  loc: Record<string, unknown>;
  text: string;
};

export type AnswerDone = {
  query: string;
  text: string;
  citations: number[];
  grounded: boolean;
  flags: string[];
  warnings: string[];
  weak_support: { n: number[]; supported: Record<number, boolean> }[];
  support_rate: number | null;
  sources: AnswerSource[];
  latency_ms: number;
  ttft_s: number | null;
};

export type AnswerCallbacks = {
  onSources: (sources: AnswerSource[], warnings: string[]) => void;
  onDelta: (text: string) => void;
  onDone: (done: AnswerDone) => void;
  onError: (message: string) => void;
};

/**
 * Stream a grounded answer. Frames arrive as `sources` → `delta`* → `done`
 * (or `error`). Returns an abort function; the caller holds it for the
 * lifetime of the stream (§6.7: the indexer stays paused until it closes).
 */
export function postAnswer(
  query: string,
  cb: AnswerCallbacks,
  filters?: QueryFilters,
): () => void {
  const token = getToken();
  const headers = new Headers();
  if (token) headers.set("X-Local-Token", token);
  headers.set("Content-Type", "application/json");

  const ctrl = new AbortController();
  let frames = 0;
  const started = performance.now();
  logger.log(logger.categories.action, "answer.begin", { query, filters });
  (async () => {
    let resp: Response;
    try {
      resp = await fetch(`${apiBase()}/answer`, {
        method: "POST",
        headers,
        body: JSON.stringify({ query, filters }),
        signal: ctrl.signal,
      });
    } catch (e) {
      if ((e as Error).name !== "AbortError") {
        logger.error(logger.categories.action, "answer.fetch-error", {
          query,
          message: String(e),
        });
        cb.onError(String(e));
      }
      return;
    }
    if (resp.status === 401) {
      cb.onError(
        "Unauthorized. The per-launch token is required on every route (PLAN.md §9).",
      );
      return;
    }
    if (!resp.ok || !resp.body) {
      cb.onError(`answer failed: ${resp.status} ${resp.statusText}`);
      return;
    }
    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buf = "";
    let pendingEvent: string | null = null;
    const handleFrame = (event: string, raw: string) => {
      try {
        const data = JSON.parse(raw) as Record<string, unknown>;
        frames += 1;
        if (event === "sources") {
          logger.log(logger.categories.action, "answer.sources", {
            query,
            count: ((data.sources as unknown[]) ?? []).length,
          });
          cb.onSources(
            (data.sources as AnswerSource[]) ?? [],
            (data.warnings as string[]) ?? [],
          );
        } else if (event === "delta") {
          cb.onDelta((data.text as string) ?? "");
        } else if (event === "done") {
          logger.log(logger.categories.action, "answer.done", {
            query,
            frames,
            duration_ms: Math.round(performance.now() - started),
            grounded: data.grounded,
            citations: data.citations,
          });
          cb.onDone(data as unknown as AnswerDone);
        } else if (event === "error") {
          logger.error(logger.categories.action, "answer.error", {
            query,
            message: data.error,
          });
          cb.onError((data.error as string) ?? "answer stream failed");
        }
      } catch {
        // A malformed frame must not kill the stream.
      }
    };
    try {
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });
        let idx: number;
        // Frames are separated by a blank line.
        while ((idx = buf.indexOf("\n\n")) >= 0) {
          const frame = buf.slice(0, idx);
          buf = buf.slice(idx + 2);
          let data = "";
          for (const line of frame.split("\n")) {
            if (line.startsWith("event:")) pendingEvent = line.slice(6).trim();
            else if (line.startsWith("data:")) data += line.slice(5).trim();
          }
          if (pendingEvent && data) handleFrame(pendingEvent, data);
          pendingEvent = null;
        }
      }
    } catch (e) {
      if ((e as Error).name !== "AbortError") {
        logger.error(logger.categories.action, "answer.read-error", {
          query,
          frames,
          message: String(e),
        });
        cb.onError(String(e));
      }
    }
  })();
  return () => ctrl.abort();
}

/**
 * Subscribe to the server-sent event stream (PLAN.md §10).
 *
 * `EventSource` cannot set request headers, which is why §13.1 has Rust proxy this
 * stream: the token has to travel with it. During browser development the token rides
 * in the query string instead.
 */
export function subscribeEvents(
  onEvent: (event: string, data: unknown) => void,
  onError?: () => void,
): () => void {
  const token = getToken();
  const url = token
    ? `${apiBase()}/events?token=${encodeURIComponent(token)}`
    : `${apiBase()}/events`;

  const source = new EventSource(url);
  const handler = (message: MessageEvent) => {
    try {
      onEvent(message.type, JSON.parse(message.data));
    } catch {
      // A malformed frame must not kill the stream; the next one may be fine.
    }
  };

  for (const name of ["heartbeat", "indexing", "status", "error"]) {
    source.addEventListener(name, handler as EventListener);
  }
  source.onerror = () => onError?.();

  return () => source.close();
}

// --------------------------------------------------------------------------------
// Sources + documents (PLAN.md §10, Phase 6 settings; TODO 6.4)
// --------------------------------------------------------------------------------

/** Source kinds the API accepts (PLAN.md §6.2). */
export const SOURCE_KINDS = [
  "files",
  "pdf",
  "email",
  "email_imap",
  "browser",
  "chat",
  "code",
] as const;
export type SourceKind = (typeof SOURCE_KINDS)[number];

export type Source = {
  id: number;
  kind: string;
  name: string;
  config: Record<string, unknown>;
  enabled: boolean;
  last_sync_at: string | null;
  documents: number;
  errors: number;
};

export async function listSources(): Promise<Source[]> {
  const body = await request<{ sources: Source[] }>("/sources");
  return body.sources;
}

export function createSource(input: {
  kind: string;
  name: string;
  config?: Record<string, unknown>;
  enabled?: boolean;
}): Promise<{ source: Source; job_id: number | null }> {
  return request("/sources", { method: "POST", body: JSON.stringify(input) });
}

export function patchSource(
  id: number,
  patch: { name?: string; config?: Record<string, unknown>; enabled?: boolean },
): Promise<{ source: Source }> {
  return request(`/sources/${id}`, { method: "PATCH", body: JSON.stringify(patch) });
}

export function deleteSource(
  id: number,
): Promise<{ forgotten: number; documents: number; chunks: number }> {
  return request(`/sources/${id}`, { method: "DELETE" });
}

export function reindexSource(id: number): Promise<{ job_id: number }> {
  return request(`/sources/${id}/reindex`, { method: "POST" });
}

export type DocumentMeta = {
  id: number;
  source_id: number;
  uri: string;
  title: string | null;
  author: string | null;
  mime: string | null;
  modified_at: string | null;
  status: string;
  error: string | null;
  chunks: number;
};

export function getDocument(id: number): Promise<DocumentMeta> {
  return request(`/documents/${id}`);
}

/** Switch the answer LLM between the pinned 4B and 8B (PLAN.md §4.1, §11.3). */
export function setAnswerModel(
  key: string,
): Promise<{ key: string; changed: boolean; running: boolean }> {
  return request("/models/answer", { method: "POST", body: JSON.stringify({ key }) });
}

/** Toggle the battery-pause signal (PLAN.md §6.6, §11.3). */
export function setBatteryLow(
  low: boolean,
): Promise<{ battery_low: boolean; paused_reason: string | null }> {
  return request("/policy/battery", { method: "POST", body: JSON.stringify({ low }) });
}

export function setIdleTimeout(
  minutes: number,
): Promise<{ minutes: number }> {
  return request("/policy/idle-timeout", { method: "POST", body: JSON.stringify({ minutes }) });
}

/**
 * Open a result's original source.
 *
 * Under Tauri the Rust `open_source` command handles the deep link (file at a
 * line, PDF at a page, `message://`, `https://`). In the browser dev host the
 * plain URI is opened in a new tab so the flow is testable without the shell
 * (PLAN.md §4 UI host row, §6.3).
 */
export async function openSource(
  uri: string,
  loc?: Record<string, unknown>,
): Promise<void> {
  const call = tauriInvoke<void>("open_source", { uri, loc: loc ?? {} });
  if (call) {
    await call;
    return;
  }
  window.open(uri, "_blank", "noopener");
}

// --------------------------------------------------------------------------------
// Tauri-only host commands (PLAN.md §11.2, §11.3)
//
// The bundle has no `@tauri-apps/api` dependency on purpose (§4: one build, two hosts),
// so host commands go through `window.__TAURI_INTERNALS__.invoke`, which Tauri injects
// into the webview. In a browser these helpers report that the feature is unavailable.
// --------------------------------------------------------------------------------

type TauriInternals = {
  invoke?: (cmd: string, args?: Record<string, unknown>) => Promise<unknown>;
};

function tauriInvoke<T>(cmd: string, args?: Record<string, unknown>): Promise<T> | null {
  const internals = (window as unknown as { __TAURI_INTERNALS__?: TauriInternals })
    .__TAURI_INTERNALS__;
  if (!internals?.invoke) return null;
  return internals.invoke(cmd, args) as Promise<T>;
}

/** True when the page is running inside the desktop shell rather than a browser. */
export function isDesktop(): boolean {
  // Probe the injected Tauri bridge directly. The old implementation invoked a fake
  // command and treated a returned promise as proof — but Tauri v2 rejects unregistered
  // commands ("not allowed by ACL"), and that rejected promise was never caught, so
  // opening Settings (the only caller) blanked the window via the global error handler.
  return (
    typeof (window as unknown as { __TAURI_INTERNALS__?: unknown })
      .__TAURI_INTERNALS__ !== "undefined"
  );
}

export function getHotkey(): Promise<string> | null {
  return tauriInvoke<string>("get_hotkey");
}

export async function setHotkey(spec: string): Promise<string> {
  const call = tauriInvoke<string>("set_hotkey", { spec });
  if (!call) throw new ApiError("the hotkey can only be changed in the desktop app", 0);
  return call;
}

/** Show/hide the desktop window, matching the global hotkey's toggle (PLAN.md §11.2). */
export function toggleWindow(): void {
  void tauriInvoke<void>("reveal_window");
}
