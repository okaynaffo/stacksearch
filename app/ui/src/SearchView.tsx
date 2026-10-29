/**
 * Search surface (PLAN.md §11.1–§11.3; TODO 6.2).
 *
 * Two-speed by design: the results list paints as soon as retrieval returns while the
 * grounded answer streams above it. The input stays fixed at the top so repeated
 * queries never require scrolling back, and filters re-run the active query.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import * as logger from "./logger";
import AnswerPanel, { type AnswerState } from "./AnswerPanel";
import FilterChips from "./FilterChips";
import ResultsList, { openResult } from "./ResultsList";
import { AlertIcon, CloseIcon, PlusIcon, SearchIcon } from "./icons";
import { Button, Kbd } from "./ui";
import {
  ApiError,
  postAnswer,
  postSearch,
  type QueryFilters,
  type SearchResponse,
} from "./api";

const EXAMPLES = [
  "What’s on my shopping list?",
  "When does the internship start?",
  "Find the latest invoice",
];

function ResultsSkeleton() {
  return (
    <section className="panel" aria-hidden="true">
      <div className="panel-head">
        <span className="panel-title">Results</span>
        <span className="skeleton" style={{ width: 64, height: 8 }} />
      </div>
      {[0, 1, 2].map((i) => (
        <div key={i} className="result-skeleton">
          <div className="skeleton" style={{ width: `${44 - i * 6}%`, height: 9 }} />
          <div className="skeleton" style={{ width: `${88 - i * 8}%`, height: 8, marginTop: 7 }} />
        </div>
      ))}
    </section>
  );
}

function EmptyState({
  onOpenSettings,
  onExample,
}: {
  onOpenSettings: () => void;
  onExample: (query: string) => void;
}) {
  return (
    <div className="empty">
      <div className="empty-icon">
        <SearchIcon size={21} />
      </div>
      <h2>Search everything, privately</h2>
      <p>
        Find anything in your files, notes, PDFs, and mail — then ask a question and get an
        answer with citations. Nothing leaves this Mac.
      </p>
      <div className="empty-examples">
        {EXAMPLES.map((example) => (
          <Button key={example} size="sm" variant="secondary" onClick={() => onExample(example)}>
            {example}
          </Button>
        ))}
      </div>
      <div className="empty-foot">
        <PlusIcon size={12} />
        <span>No sources yet?</span>
        <button type="button" className="link" onClick={onOpenSettings}>
          Add a folder
        </button>
      </div>
    </div>
  );
}

export default function SearchView({
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
  const [pending, setPending] = useState(false);
  const [answer, setAnswer] = useState<AnswerState>({ phase: "idle" });
  const [selected, setSelected] = useState(0);
  const abortRef = useRef<(() => void) | null>(null);
  const seqRef = useRef(0);
  const inputRef = useRef<HTMLInputElement | null>(null);
  const answerRef = useRef<HTMLDivElement | null>(null);
  const resultsRef = useRef<HTMLDivElement | null>(null);

  useEffect(
    () => () => {
      abortRef.current?.();
      logger.log(logger.categories.lifecycle, "search-view.unmount", {});
    },
    [],
  );

  const run = useCallback((raw: string, nextFilters: QueryFilters) => {
    const trimmed = raw.trim();
    if (!trimmed) return;
    abortRef.current?.();
    const seq = ++seqRef.current;
    logger.log(logger.categories.action, "search.run", { query: trimmed, filters: nextFilters });
    setSelected(0);
    setSearchError(null);
    setResults(null);
    setPending(true);
    setAnswer({ phase: "loading", query: trimmed });

    void postSearch(trimmed, { filters: nextFilters })
      .then((response) => {
        if (seq !== seqRef.current) return;
        setResults(response);
        setPending(false);
      })
      .catch((error) => {
        if (seq !== seqRef.current) return;
        setSearchError(error instanceof ApiError ? error.message : String(error));
        setPending(false);
      });

    let streamed = "";
    abortRef.current = postAnswer(
      trimmed,
      {
        onSources: (sources) => {
          if (seq !== seqRef.current) return;
          streamed = "";
          setAnswer({ phase: "streaming", query: trimmed, text: "", sources });
        },
        onDelta: (text) => {
          if (seq !== seqRef.current) return;
          streamed += text;
          const snapshot = streamed;
          setAnswer((prev) =>
            prev.phase === "streaming" ? { ...prev, text: snapshot } : prev,
          );
        },
        onDone: (done) => {
          if (seq !== seqRef.current) return;
          setAnswer({ phase: "done", query: trimmed, text: done.text, done });
        },
        onError: (message) => {
          if (seq !== seqRef.current) return;
          setAnswer({ phase: "error", query: trimmed, message });
        },
      },
      nextFilters,
    );
  }, []);

  // Global focus shortcuts: ⌘K / ⌘F focus the field, "/" when not already typing.
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement | null;
      const typing =
        target?.tagName === "INPUT" ||
        target?.tagName === "TEXTAREA" ||
        target?.isContentEditable === true;
      if ((event.metaKey || event.ctrlKey) && (event.key === "k" || event.key === "f")) {
        event.preventDefault();
        inputRef.current?.focus();
        inputRef.current?.select();
        logger.log(logger.categories.key, "search.focus-shortcut", {});
        return;
      }
      if (event.key === "/" && !typing) {
        event.preventDefault();
        inputRef.current?.focus();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const clear = () => {
    abortRef.current?.();
    seqRef.current += 1;
    setQuery("");
    setResults(null);
    setSearchError(null);
    setPending(false);
    setAnswer({ phase: "idle" });
    inputRef.current?.focus();
  };

  const onInputKey = (event: React.KeyboardEvent<HTMLInputElement>) => {
    if (event.key === "Escape" && query.length > 0) {
      // Clear the field first; a second Escape falls through to the app-wide hide.
      event.preventDefault();
      event.stopPropagation();
      clear();
      logger.log(logger.categories.action, "search.clear", {});
    }
  };

  const onResultsKey = (event: React.KeyboardEvent) => {
    if (!results || results.results.length === 0) return;
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      const dir = event.key === "ArrowDown" ? 1 : -1;
      setSelected((current) =>
        Math.min(Math.max(current + dir, 0), results.results.length - 1),
      );
    } else if (event.key === "Home") {
      event.preventDefault();
      setSelected(0);
    } else if (event.key === "End") {
      event.preventDefault();
      setSelected(Math.max(results.results.length - 1, 0));
    } else if (event.key === "Enter") {
      const hit = results.results[selected];
      if (hit) openResult(hit);
    } else if (event.key === "Tab") {
      event.preventDefault();
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

  const showEmpty = !results && !pending && !searchError && answer.phase === "idle";

  return (
    <section className="ss-search" aria-label="search">
      <form
        onSubmit={(event) => {
          event.preventDefault();
          run(query, filters);
        }}
      >
        <div className="ss-searchbar">
          <SearchIcon size={18} className="ss-search-icon" />
          <input
            ref={inputRef}
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            onKeyDown={onInputKey}
            autoFocus
            placeholder="Search your files, notes, PDFs, mail…"
            aria-label="Search your files"
            spellCheck={false}
          />
          {query.length > 0 ? (
            <button
              type="button"
              className="icon-btn icon-btn-sm"
              aria-label="Clear search"
              title="Clear search"
              onClick={clear}
            >
              <CloseIcon size={13} />
            </button>
          ) : (
            <Kbd>⌘K</Kbd>
          )}
        </div>
      </form>

      <FilterChips filters={filters} onChange={setFilter} />

      {progress ? (
        <p className="progress" role="status">
          <span className="spinner" aria-hidden="true" />
          {progress}
        </p>
      ) : null}

      <div className="ss-scroll">
        {searchError ? (
          <div className="banner-danger banner-inline" role="alert">
            <AlertIcon size={15} />
            <div>
              <div className="banner-title">Search failed</div>
              <div className="banner-detail">{searchError}</div>
            </div>
          </div>
        ) : null}

        {showEmpty ? (
          <EmptyState onOpenSettings={onOpenSettings} onExample={(q) => {
            setQuery(q);
            run(q, filters);
          }} />
        ) : (
          <>
            <div ref={answerRef} tabIndex={0} aria-label="Answer panel">
              <AnswerPanel state={answer} />
            </div>

            {results ? (
              <div
                ref={resultsRef}
                tabIndex={0}
                onKeyDown={onResultsKey}
                aria-label="Results panel"
              >
                <ResultsList
                  results={results}
                  selected={selected}
                  onSelect={setSelected}
                  onOpen={openResult}
                />
              </div>
            ) : pending ? (
              <ResultsSkeleton />
            ) : null}
          </>
        )}
      </div>
    </section>
  );
}
