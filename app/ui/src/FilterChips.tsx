/**
 * Filter chips for the search bar (PLAN.md §11.1; TODO 6.2).
 *
 * Two independent rows map onto :class:`QueryFilters`: a source-kind toggle and a
 * relative date range. Author filtering is left to the query parser (`from:alice`)
 * until the parser lands widely enough to advertise it in the UI.
 */

import { SOURCE_KINDS, type QueryFilters } from "./api";

type DatePreset = { key: string; label: string; days: number | null };

const DATE_PRESETS: DatePreset[] = [
  { key: "any", label: "any time", days: null },
  { key: "week", label: "past week", days: 7 },
  { key: "month", label: "past month", days: 30 },
  { key: "year", label: "past year", days: 365 },
];

function daysAgo(days: number): string {
  const d = new Date();
  d.setDate(d.getDate() - days);
  return d.toISOString().slice(0, 10);
}

export default function FilterChips({
  filters,
  onChange,
}: {
  filters: QueryFilters;
  onChange: (next: QueryFilters) => void;
}) {
  const activeKind = filters.source_kind ?? "any";
  const activePreset =
    DATE_PRESETS.find((p) => p.days !== null && filters.date_from === daysAgo(p.days))?.key ??
    "any";

  const chip = (active: boolean) =>
    "rounded-full border px-2.5 py-0.5 text-xs transition-colors " +
    (active
      ? "border-sky-500/70 bg-sky-500/15 text-sky-200"
      : "border-neutral-800 text-neutral-400 hover:border-neutral-700 hover:text-neutral-200");

  return (
    <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
      <div className="flex flex-wrap gap-1.5" role="group" aria-label="source kind">
        <button
          type="button"
          className={chip(activeKind === "any")}
          onClick={() => onChange({ ...filters, source_kind: undefined })}
        >
          all
        </button>
        {SOURCE_KINDS.map((kind) => (
          <button
            key={kind}
            type="button"
            className={chip(activeKind === kind)}
            onClick={() =>
              onChange({ ...filters, source_kind: activeKind === kind ? undefined : kind })
            }
          >
            {kind}
          </button>
        ))}
      </div>
      <div className="flex flex-wrap gap-1.5" role="group" aria-label="date range">
        {DATE_PRESETS.map((p) => (
          <button
            key={p.key}
            type="button"
            className={chip(activePreset === p.key)}
            onClick={() =>
              onChange({
                ...filters,
                date_from: p.days === null ? undefined : daysAgo(p.days),
                date_to: undefined,
              })
            }
          >
            {p.label}
          </button>
        ))}
      </div>
    </div>
  );
}
