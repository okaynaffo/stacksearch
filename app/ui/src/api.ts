/**
 * Typed client for the local core service (PLAN.md §9, §10).
 *
 * §9 requires the per-launch token on every route. The token is delivered by the host —
 * the Tauri side passes it in, and a bare `uv run core.api.run` prints it for the browser
 * dev flow — and is read from the URL once so a refresh keeps working.
 */

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
    reranker: { key: string; enabled: boolean };
  };
  llm_server: { running: boolean; pid?: number | null; model?: string | null; restarts?: number };
  queue: number;
  events_subscribers: number;
};

/**
 * Resolve the API origin at runtime.
 *
 * The same bundle runs in a browser during Phases 1-5 and inside the Tauri webview in
 * Phase 6, so the origin cannot be baked in at build time. Relative URLs work in both
 * cases because in the shipped app the UI is served by the core service itself.
 */
const API_BASE = "";

let cachedToken: string | null = null;

/** Read the token from `?token=` once and remember it. */
export function initToken(): string | null {
  if (cachedToken) return cachedToken;
  const fromQuery = new URLSearchParams(window.location.search).get("token");
  if (fromQuery) {
    cachedToken = fromQuery;
    return cachedToken;
  }
  // Tauri's Rust side injects the token on the window object.
  const injected = (window as unknown as { __STACKSEARCH_TOKEN__?: string })
    .__STACKSEARCH_TOKEN__;
  cachedToken = injected ?? null;
  return cachedToken;
}

export function getToken(): string | null {
  return cachedToken ?? initToken();
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

  const resp = await fetch(`${API_BASE}${path}`, { ...init, headers });

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

export function postSearch(query: string, limit = 20): Promise<SearchResponse> {
  return request<SearchResponse>("/search", {
    method: "POST",
    body: JSON.stringify({ query, limit }),
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
export function postAnswer(query: string, cb: AnswerCallbacks): () => void {
  const token = getToken();
  const headers = new Headers();
  if (token) headers.set("X-Local-Token", token);
  headers.set("Content-Type", "application/json");

  const ctrl = new AbortController();
  (async () => {
    let resp: Response;
    try {
      resp = await fetch(`${API_BASE}/answer`, {
        method: "POST",
        headers,
        body: JSON.stringify({ query }),
        signal: ctrl.signal,
      });
    } catch (e) {
      if ((e as Error).name !== "AbortError") cb.onError(String(e));
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
        if (event === "sources") {
          cb.onSources(
            (data.sources as AnswerSource[]) ?? [],
            (data.warnings as string[]) ?? [],
          );
        } else if (event === "delta") {
          cb.onDelta((data.text as string) ?? "");
        } else if (event === "done") {
          cb.onDone(data as unknown as AnswerDone);
        } else if (event === "error") {
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
      if ((e as Error).name !== "AbortError") cb.onError(String(e));
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
    ? `${API_BASE}/events?token=${encodeURIComponent(token)}`
    : `${API_BASE}/events`;

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