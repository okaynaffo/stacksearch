/**
 * Results list with keyboard navigation (PLAN.md §11.2; TODO 6.2).
 *
 * Snippets already carry `<mark>` highlights from FTS5 (§10), so they are rendered
 * as HTML. Keyboard: ArrowUp/ArrowDown move, Enter opens the original. The parent
 * owns selection so Tab can switch focus between this and the answer panel.
 */

import { useEffect, useRef } from "react";
import { openSource, type SearchResult, type SearchResponse } from "./api";
import { SearchOffIcon } from "./icons";

const KIND_LABEL: Record<string, string> = {
  files: "file",
  pdf: "pdf",
  email: "email",
  email_imap: "imap",
  browser: "web",
  chat: "chat",
  code: "code",
};

export default function ResultsList({
  results,
  selected,
  onSelect,
  onOpen,
}: {
  results: SearchResponse;
  selected: number;
  onSelect: (index: number) => void;
  onOpen: (result: SearchResult) => void;
}) {
  const listRef = useRef<HTMLUListElement | null>(null);

  useEffect(() => {
    const node = listRef.current?.querySelector<HTMLElement>(
      `[data-result-index="${selected}"]`,
    );
    node?.scrollIntoView({ block: "nearest" });
  }, [selected]);

  const count = results.results.length;

  return (
    <section aria-label="results" className="panel">
      <div className="panel-head">
        <span className="panel-title">Results</span>
        <span className="panel-note">
          {count} {count === 1 ? "match" : "matches"} · {results.latency_ms} ms
        </span>
      </div>

      {count === 0 ? (
        <div className="result-empty">
          <SearchOffIcon size={20} />
          <strong>No matches</strong>
          <span>Try a shorter query, or clear a filter.</span>
        </div>
      ) : (
        <ul ref={listRef} className="results-list">
          {results.results.map((r, i) => (
            <li key={r.chunk_id}>
              <button
                type="button"
                data-result-index={i}
                aria-selected={i === selected}
                onMouseEnter={() => onSelect(i)}
                onFocus={() => onSelect(i)}
                onClick={() => onOpen(r)}
                className="result-row"
              >
                <span className="flex items-baseline gap-2">
                  <span className="result-title">{r.title ?? r.uri}</span>
                  <span className="result-meta shrink-0">
                    <span className="kind">{KIND_LABEL[r.source_kind] ?? r.source_kind}</span>
                    {r.date ? <span>{r.date}</span> : null}
                    {r.author ? <span>· {r.author}</span> : null}
                  </span>
                </span>
                <span
                  className="result-snippet"
                  // Snippet markup is produced by the FTS5 highlighter, not user HTML.
                  dangerouslySetInnerHTML={{ __html: r.snippet }}
                />
              </button>
            </li>
          ))}
        </ul>
      )}

      {results.warnings?.map((w, i) => (
        <p key={i} className="result-warning">
          {w}
        </p>
      ))}
    </section>
  );
}

/** Open a result through the host (Tauri deep link) or a new tab (browser). */
export function openResult(result: SearchResult): void {
  if (result.uri) void openSource(result.uri, result.loc);
}
