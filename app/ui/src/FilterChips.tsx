/**
 * Filter chips for the search bar (PLAN.md §11.1; TODO 6.2).
 *
 * Two independent rows map onto :class:`QueryFilters`: a source-kind toggle and a
 * relative date range. Author filtering is left to the query parser (`from:alice`)
 * until the parser lands widely enough to advertise it in the UI.
 */

import { SOURCE_KINDS, type QueryFilters } from "./api";
import { Button, Chip } from "./ui";

type DatePreset = { key: string; label: string; days: number | null };

const DATE_PRESETS: DatePreset[] = [
  { key: "any", label: "Any time", days: null },
  { key: "week", label: "Past week", days: 7 },
  { key: "month", label: "Past month", days: 30 },
  { key: "year", label: "Past year", days: 365 },
];

const KIND_LABEL: Record<string, string> = {
  files: "Files",
  pdf: "PDF",
  email: "Email",
  email_imap: "IMAP",
  browser: "Browser",
  chat: "Chat",
  code: "Code",
};

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
  const hasFilter = Boolean(filters.source_kind || filters.date_from || filters.date_to || filters.author);

  return (
    <div className="filter-bar" role="group" aria-label="search filters">
      <div className="filter-group" role="group" aria-label="source type">
        <span className="filter-label">Type</span>
        <Chip
          active={activeKind === "any"}
          onClick={() => onChange({ ...filters, source_kind: undefined })}
        >
          All
        </Chip>
        {SOURCE_KINDS.map((kind) => (
          <Chip
            key={kind}
            active={activeKind === kind}
            onClick={() =>
              onChange({ ...filters, source_kind: activeKind === kind ? undefined : kind })
            }
          >
            {KIND_LABEL[kind] ?? kind}
          </Chip>
        ))}
      </div>

      <span className="filter-sep" aria-hidden="true" />

      <div className="filter-group" role="group" aria-label="date range">
        <span className="filter-label">When</span>
        {DATE_PRESETS.map((preset) => (
          <Chip
            key={preset.key}
            active={activePreset === preset.key}
            onClick={() =>
              onChange({
                ...filters,
                date_from: preset.days === null ? undefined : daysAgo(preset.days),
                date_to: undefined,
              })
            }
          >
            {preset.label}
          </Chip>
        ))}
      </div>

      {hasFilter ? (
        <Button variant="ghost" size="sm" onClick={() => onChange({})}>
          Clear
        </Button>
      ) : null}
    </div>
  );
}
