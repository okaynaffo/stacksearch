/**
 * Phase 4 shell (PLAN.md §7.4, §11.2; TODO 4.2).
 *
 * Two-speed search: the results list returns immediately after retrieval while
 * the answer streams above it. Keyboard-first: arrows move through results,
 * Enter opens the original, Tab switches between results and answer.
 * The Phase 0 status panels stay below for development; Phase 6 restyles this
 * into the Spotlight window without touching the API client.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import AnswerPanel, { type AnswerState } from "./AnswerPanel";
import {
  ApiError,
  fetchStatus,
  postAnswer,
  postSearch,
  subscribeEvents,
  type SearchResponse,
  type Status,
} from "./api";

type EventLog = { id: number; event: string; at: string; detail: string };

function Row({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="grid grid-cols-[minmax(9rem,auto)_1fr] gap-3 py-0.5">
      <span className="text-neutral-500">{label}</span>
      <span className="tabular-nums">{value}</span>
    </div>
  );
}

function IndexPanel({ status }: { status: Status }) {
  const i = status.index;
  return (
    <section className="rounded-lg border border-neutral-800 bg-neutral-900/40 p-3">
      <h2 className="mb-2 text-xs font-semibold uppercase tracking-wide text-neutral-400">
        index
      </h2>
      {status.index_error ? (
        <p className="text-sm text-red-400">unavailable: {status.index_error}</p>
      ) : (
        <div className="text-sm">
          <Row label="path" value={<span className="break-all text-neutral-300">{status.index_path}</span>} />
          <Row label="sources" value={i.sources ?? "—"} />
          <Row label="documents" value={i.documents ?? "—"} />
          <Row label="chunks" value={i.chunks ?? "—"} />
          <Row label="vectors" value={i.vectors ?? "—"} />
          <Row label="queue" value={i.jobs_pending ?? "—"} />
          <Row label="schema" value={`v${i.schema_version ?? "—"}`} />
          <Row label="sqlite-vec" value={i.vec_version ?? "—"} />
        </div>
      )}
    </section>
  );
}

function ModelsPanel({ status }: { status: Status }) {
  const { embedding, answer_llm: llm, reranker } = status.models;
  return (
    <section className="rounded-lg border border-neutral-800 bg-neutral-900/40 p-3">
      <h2 className="mb-2 text-xs font-semibold uppercase tracking-wide text-neutral-400">
        models
      </h2>
      <div className="text-sm">
        <Row
          label="embeddings"
          value={
            <>
              {embedding.id} · {embedding.dim}d · {embedding.size_gb} GB ·{" "}
              {embedding.loaded ? "resident" : "evicted"}
            </>
          }
        />
        <Row
          label="answer LLM"
          value={
            <>
              {llm.label} · {llm.size_gb} GB ·{" "}
              {llm.present ? (
                <span className="text-neutral-300">{llm.path.split("/").pop()}</span>
              ) : (
                <span className="text-amber-400">not downloaded</span>
              )}
            </>
          }
        />
        <Row
          label="reasoning"
          value={
            llm.reasoning_enabled ? (
              <span className="text-amber-400">
                on — consumes the §8.2 token budget before answering
              </span>
            ) : (
              "off (required by §4.1)"
            )
          }
        />
        <Row label="reranker" value={`${reranker.key} · ${reranker.enabled ? "on" : "Phase 3"}`} />
      </div>
    </section>
  );
}

function LlmPanel({ status }: { status: Status }) {
  const s = status.llm_server;
  return (
    <section className="rounded-lg border border-neutral-800 bg-neutral-900/40 p-3">
      <h2 className="mb-2 text-xs font-semibold uppercase tracking-wide text-neutral-400">
        llama-server
      </h2>
      <div className="text-sm">
        <Row
          label="state"
          value={
            s.running ? (
              <span className="text-emerald-400">running (pid {s.pid})</span>
            ) : (
              <span className="text-neutral-500">stopped — spawns on first question</span>
            )
          }
        />
        <Row label="subscribers" value={status.events_subscribers} />
      </div>
    </section>
  );
}

function EventsPanel({ log }: { log: EventLog[] }) {
  return (
    <section className="rounded-lg border border-neutral-800 bg-neutral-900/40 p-3">
      <h2 className="mb-2 text-xs font-semibold uppercase tracking-wide text-neutral-400">
        /events <span className="text-neutral-600">(SSE through WKWebView, §13.1)</span>
      </h2>
      {log.length === 0 ? (
        <p className="text-sm text-neutral-500">waiting for events…</p>
      ) : (
        <ul className="max-h-40 space-y-0.5 overflow-auto text-xs">
          {log.map((e) => (
            <li key={e.id} className="flex gap-2">
              <span className="tabular-nums text-neutral-600">{e.at}</span>
              <span className="text-neutral-300">{e.event}</span>
              <span className="truncate text-neutral-500">{e.detail}</span>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

function SearchSection() {
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<SearchResponse | null>(null);
  const [searchError, setSearchError] = useState<string | null>(null);
  const [answer, setAnswer] = useState<AnswerState>({ phase: "idle" });
  const [selected, setSelected] = useState(0);
  const abortRef = useRef<(() => void) | null>(null);
  const answerRef = useRef<HTMLElement | null>(null);
  const resultsRef = useRef<HTMLElement | null>(null);

  useEffect(() => () => abortRef.current?.(), []);

  const run = useCallback(
    (q: string) => {
      const trimmed = q.trim();
      if (!trimmed) return;
      abortRef.current?.();
      setSelected(0);
      setSearchError(null);
      setAnswer({ phase: "loading", query: trimmed });
      void postSearch(trimmed)
        .then((r) => setResults(r))
        .catch((e) => setSearchError(e instanceof ApiError ? e.message : String(e)));
      let streamed = "";
      abortRef.current = postAnswer(trimmed, {
        onSources: (sources, warnings) => {
          streamed = "";
          setAnswer({ phase: "streaming", query: trimmed, text: "", sources });
          if (warnings.length > 0) setSearchError(null);
        },
        onDelta: (text) => {
          streamed += text;
          const snapshot = streamed;
          setAnswer((prev) =>
            prev.phase === "streaming" ? { ...prev, text: snapshot } : prev,
          );
        },
        onDone: (done) => setAnswer({ phase: "done", query: trimmed, text: done.text, done }),
        onError: (message) => setAnswer({ phase: "error", query: trimmed, message }),
      });
    },
    [],
  );

  const onResultsKey = (e: React.KeyboardEvent) => {
    if (!results) return;
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      const dir = e.key === "ArrowDown" ? 1 : -1;
      setSelected((s) => Math.min(Math.max(s + dir, 0), results.results.length - 1));
    } else if (e.key === "Enter") {
      const hit = results.results[selected];
      if (hit?.uri) window.open(hit.uri, "_blank", "noopener");
    } else if (e.key === "Tab") {
      // Tab switches between results and answer (§11.2).
      e.preventDefault();
      (document.activeElement === resultsRef.current ? answerRef.current : resultsRef.current)?.focus();
    }
  };

  return (
    <section className="space-y-3">
      <form
        onSubmit={(e) => {
          e.preventDefault();
          run(query);
        }}
      >
        <input
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="Search your files, notes, PDFs…"
          aria-label="search"
          className="w-full rounded-lg border border-neutral-700 bg-neutral-900 px-3 py-2 text-sm text-neutral-100 placeholder:text-neutral-600 focus:border-sky-600 focus:outline-none"
        />
      </form>

      <div ref={(el) => { answerRef.current = el; }} tabIndex={0} className="focus:outline-none">
        <AnswerPanel state={answer} />
      </div>

      {searchError ? <p className="text-sm text-red-300">{searchError}</p> : null}

      {results ? (
        <section
          aria-label="results"
          ref={(el) => { resultsRef.current = el; }}
          tabIndex={0}
          onKeyDown={onResultsKey}
          className="rounded-lg border border-neutral-800 bg-neutral-900/40 p-3 focus:outline-none"
        >
          <h2 className="mb-2 text-xs font-semibold uppercase tracking-wide text-neutral-400">
            results{" "}
            <span className="text-neutral-600">
              {results.results.length} in {results.latency_ms} ms
            </span>
          </h2>
          {results.results.length === 0 ? (
            <p className="text-sm text-neutral-500">nothing found.</p>
          ) : (
            <ul className="space-y-2">
              {results.results.map((r, i) => (
                <li
                  key={r.chunk_id}
                  onMouseEnter={() => setSelected(i)}
                  onClick={() => r.uri && window.open(r.uri, "_blank", "noopener")}
                  className={
                    "cursor-pointer rounded p-1 " +
                    (i === selected ? "bg-neutral-800" : "")
                  }
                >
                  <p className="text-sm text-neutral-200">
                    {r.title ?? r.uri}{" "}
                    <span className="text-xs text-neutral-500">
                      {r.source_kind} · {r.date ?? "undated"}
                    </span>
                  </p>
                  {/* Snippets arrive with <mark> highlights from FTS5 (§10). */}
                  <p
                    className="text-xs text-neutral-400 [&_mark]:bg-yellow-900 [&_mark]:text-yellow-200"
                    dangerouslySetInnerHTML={{ __html: r.snippet }}
                  />
                </li>
              ))}
            </ul>
          )}
          {results.warnings?.map((w, i) => (
            <p key={i} className="mt-1 text-xs text-neutral-500">
              {w}
            </p>
          ))}
        </section>
      ) : null}
    </section>
  );
}

export default function App() {
  const [status, setStatus] = useState<Status | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [log, setLog] = useState<EventLog[]>([]);
  const [heartbeats, setHeartbeats] = useState(0);
  const nextId = useRef(0);

  const load = useCallback(async () => {
    try {
      setStatus(await fetchStatus());
      setError(null);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    }
  }, []);

  useEffect(() => {
    void load();
    const poll = window.setInterval(load, 2000);
    return () => window.clearInterval(poll);
  }, [load]);

  useEffect(() => {
    const unsubscribe = subscribeEvents((event, data) => {
      const id = nextId.current++;
      const at = new Date().toLocaleTimeString();
      if (event === "heartbeat") {
        // Proves the stream is alive without filling the log with noise.
        setHeartbeats((n) => n + 1);
        return;
      }
      setLog((entries) =>
        [{ id, event, at, detail: JSON.stringify(data) }, ...entries].slice(0, 50),
      );
    });
    return unsubscribe;
  }, []);

  return (
    <main className="mx-auto min-h-full max-w-3xl space-y-3 p-4 text-sm">
      <header className="flex items-baseline justify-between">
        <h1 className="text-base font-semibold">StackSearch</h1>
        <span className="text-xs text-neutral-600">
          phase {status?.phase ?? 0} · v{status?.version ?? "—"} · {status?.uptime_s ?? 0}s
        </span>
      </header>

      {error ? (
        <p className="rounded-lg border border-red-900 bg-red-950/40 p-3 text-red-300">
          {error}
        </p>
      ) : null}

      <SearchSection />

      {!status && !error ? (
        <p className="text-neutral-500">connecting to the core service…</p>
      ) : null}

      {status ? (
        <>
          <IndexPanel status={status} />
          <ModelsPanel status={status} />
          <LlmPanel status={status} />
          <EventsPanel log={log} />
          <p className="text-xs text-neutral-600">
            {heartbeats} heartbeat{heartbeats === 1 ? "" : "s"} received · nothing leaves this
            machine (PLAN.md §9)
          </p>
        </>
      ) : null}
    </main>
  );
}