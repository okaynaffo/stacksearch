/**
 * Phase 6 shell (PLAN.md §11.1-§11.3; TODO 6.2/6.3).
 *
 * One window, two views: a search-first surface and a settings panel. Search keeps the
 * Phase 4 two-speed flow — results render the moment retrieval returns while the answer
 * streams above them — and adds filter chips and keyboard navigation. Everything that
 * used to be a raw diagnostics panel now lives under Settings.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import AnswerPanel, { type AnswerState } from "./AnswerPanel";
import FilterChips from "./FilterChips";
import ResultsList, { openResult } from "./ResultsList";
import Settings from "./Settings";
import {
  ApiError,
  fetchStatus,
  postAnswer,
  postSearch,
  subscribeEvents,
  toggleWindow,
  type QueryFilters,
  type SearchResponse,
  type Status,
} from "./api";

function SearchSection({
  progress,
  onOpenSettings,
}: {
  progress: string | null;
  onOpenSettings: () => void;
}) {
  const [query, setQuery] = useState("");
  const [filters, setFilters] = useState<QueryFilters>({});
  const [results, setResults] = useState<SearchResponse | null>(null);
  const [searchError, setSearchError] = useState<string | null>(null);
  const [answer, setAnswer] = useState<AnswerState>({ phase: "idle" });
  const [selected, setSelected] = useState(0);
  const abortRef = useRef<(() => void) | null>(null);
  const answerRef = useRef<HTMLDivElement | null>(null);
  const resultsRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => () => abortRef.current?.(), []);

  const run = useCallback(
    (q: string, f: QueryFilters) => {
      const trimmed = q.trim();
      if (!trimmed) return;
      abortRef.current?.();
      setSelected(0);
      setSearchError(null);
      setAnswer({ phase: "loading", query: trimmed });
      void postSearch(trimmed, { filters: f })
        .then((r) => setResults(r))
        .catch((e) => setSearchError(e instanceof ApiError ? e.message : String(e)));
      let streamed = "";
      abortRef.current = postAnswer(
        trimmed,
        {
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
        },
        f,
      );
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
      if (hit) openResult(hit);
    } else if (e.key === "Tab") {
      e.preventDefault();
      (document.activeElement === resultsRef.current
        ? answerRef.current
        : resultsRef.current
      )?.focus();
    }
  };

  const setFilter = (next: QueryFilters) => {
    setFilters(next);
    if (query.trim()) run(query, next);
  };

  return (
    <section className="space-y-3">
      <form
        onSubmit={(e) => {
          e.preventDefault();
          run(query, filters);
        }}
      >
        <input
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          autoFocus
          placeholder="Search your files, notes, PDFs, mail…"
          aria-label="search"
          className="w-full rounded-lg border border-neutral-700 bg-neutral-900 px-3 py-2 text-base text-neutral-100 placeholder:text-neutral-600 focus:border-sky-600 focus:outline-none"
        />
      </form>

      <FilterChips filters={filters} onChange={setFilter} />

      {progress && (
        <p className="flex items-center gap-2 text-xs text-neutral-500">
          <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-sky-500" />
          {progress}
        </p>
      )}

      <div ref={answerRef} tabIndex={0} className="focus:outline-none">
        <AnswerPanel state={answer} />
      </div>

      {searchError ? <p className="text-sm text-red-300">{searchError}</p> : null}

      {results ? (
        <div ref={resultsRef} tabIndex={0} onKeyDown={onResultsKey} className="focus:outline-none">
          <ResultsList
            results={results}
            selected={selected}
            onSelect={setSelected}
            onOpen={openResult}
          />
        </div>
      ) : (
        <p className="text-xs text-neutral-600">
          no results yet ·{" "}
          <button
            type="button"
            onClick={onOpenSettings}
            className="underline hover:text-neutral-400"
          >
            add a source
          </button>{" "}
          to get started
        </p>
      )}
    </section>
  );
}

export default function App() {
  const [view, setView] = useState<"search" | "settings">("search");
  const [status, setStatus] = useState<Status | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [progress, setProgress] = useState<string | null>(null);

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
    const poll = window.setInterval(load, 3000);
    return () => window.clearInterval(poll);
  }, [load]);

  useEffect(
    () =>
      subscribeEvents((event, data) => {
        if (event === "heartbeat") return;
        const detail = (data as { detail?: string })?.detail;
        setProgress(detail ?? event);
        void load();
      }),
    [load],
  );

  // The tray's "Settings…" item cannot use Tauri events (no @tauri-apps/api dependency),
  // so Rust evaluates a plain DOM event in the page and this listener switches views.
  useEffect(() => {
    const openSettings = () => setView("settings");
    window.addEventListener("stacksearch:open-settings", openSettings);
    return () => window.removeEventListener("stacksearch:open-settings", openSettings);
  }, []);

  // Escape dismisses the Spotlight-style bar (§11.2); a no-op in the browser host.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") toggleWindow();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const pending = status?.index.jobs_pending ?? 0;
  const indexing = pending > 0;
  const bar = indexing
    ? `${pending} job${pending === 1 ? "" : "s"} queued${progress ? ` · ${progress}` : ""}`
    : null;

  return (
    <div className="mx-auto flex min-h-full max-w-3xl flex-col p-4 text-sm">
      <header
        data-tauri-drag-region
        className="mb-3 flex items-center justify-between"
      >
        <h1 className="text-base font-semibold">StackSearch</h1>
        <nav className="flex items-center gap-3 text-xs">
          <button
            type="button"
            onClick={() => setView("search")}
            className={view === "search" ? "text-neutral-100" : "text-neutral-500 hover:text-neutral-300"}
          >
            search
          </button>
          <button
            type="button"
            onClick={() => setView("settings")}
            className={view === "settings" ? "text-neutral-100" : "text-neutral-500 hover:text-neutral-300"}
          >
            settings
          </button>
          {status && (
            <span className="text-neutral-700">
              {pending} queued · v{status.version}
            </span>
          )}
        </nav>
      </header>

      {error ? (
        <p className="mb-3 rounded-lg border border-red-900 bg-red-950/40 p-3 text-red-300">
          {error}
        </p>
      ) : null}

      <main className="flex-1">
        {view === "search" ? (
          <SearchSection progress={bar} onOpenSettings={() => setView("settings")} />
        ) : (
          <Settings status={status} onRefreshStatus={() => void load()} />
        )}
      </main>

      {!status && !error ? (
        <p className="text-neutral-500">connecting to the core service…</p>
      ) : null}

      <footer className="mt-4 text-center text-xs text-neutral-700">
        everything stays on this machine (PLAN.md §9)
      </footer>
    </div>
  );
}
