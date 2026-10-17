/**
 * Results list with keyboard navigation (PLAN.md §11.2; TODO 6.2).
 *
 * Snippets already carry `<mark>` highlights from FTS5 (§10), so they are rendered
 * as HTML. Keyboard: ArrowUp/ArrowDown move, Enter opens the original. The parent
 * owns selection so Tab can switch focus between this and the answer panel.
 */

import { openSource, type SearchResult, type SearchResponse } from "./api";

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
  return (
    <section
      aria-label="results"
      className="rounded-xl border border-neutral-800 bg-neutral-900/40"
    >
      <header className="flex items-baseline justify-between border-b border-neutral-800 px-3 py-2">
        <h2 className="text-xs font-semibold uppercase tracking-wide text-neutral-400">
          results
        </h2>
        <span className="text-xs text-neutral-600">
          {results.results.length} in {results.latency_ms} ms
        </span>
      </header>
      {results.results.length === 0 ? (
        <p className="px-3 py-4 text-sm text-neutral-500">nothing found.</p>
      ) : (
        <ul className="divide-y divide-neutral-800/70">
          {results.results.map((r, i) => (
            <li key={r.chunk_id}>
              <button
                type="button"
                onMouseEnter={() => onSelect(i)}
                onFocus={() => onSelect(i)}
                onClick={() => onOpen(r)}
                className={
                  "block w-full px-3 py-2 text-left transition-colors " +
                  (i === selected ? "bg-neutral-800/80" : "hover:bg-neutral-800/40")
                }
              >
                <p className="flex items-baseline gap-2 text-sm text-neutral-100">
                  <span className="truncate">{r.title ?? r.uri}</span>
                  <span className="shrink-0 text-xs font-normal text-neutral-500">
                    {r.source_kind}
                    {r.date ? ` · ${r.date}` : ""}
                    {r.author ? ` · ${r.author}` : ""}
                  </span>
                </p>
                <p
                  className="mt-0.5 line-clamp-2 text-xs text-neutral-400 [&_mark]:rounded [&_mark]:bg-amber-500/20 [&_mark]:px-0.5 [&_mark]:text-amber-200"
                  dangerouslySetInnerHTML={{ __html: r.snippet }}
                />
              </button>
            </li>
          ))}
        </ul>
      )}
      {results.warnings?.map((w, i) => (
        <p key={i} className="border-t border-neutral-800 px-3 py-1 text-xs text-neutral-500">
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
