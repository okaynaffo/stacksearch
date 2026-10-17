/**
 * Answer panel: streams a grounded answer above the results list
 * (PLAN.md §7.4 two-speed, §11.2; TODO 4.2/4.3).
 *
 * Citations `[n]` render as chips. Hovering a chip shows the cited passage;
 * clicking it opens the original source. Weakly supported claims (§8.3) are
 * underlined amber; stripped citations and the no-grounded-answer path are
 * shown explicitly rather than failing silently.
 */

import { useState } from "react";
import type { AnswerDone, AnswerSource } from "./api";

export type AnswerState =
  | { phase: "idle" }
  | { phase: "loading"; query: string }
  | {
      phase: "streaming";
      query: string;
      text: string;
      sources: AnswerSource[];
    }
  | { phase: "done"; query: string; text: string; done: AnswerDone }
  | { phase: "error"; query: string; message: string };

function SourceLabel({ s }: { s: AnswerSource }) {
  const bits = [s.source_kind ?? "source", s.date ?? "undated"];
  if (s.author) bits.push(`from ${s.author}`);
  if (s.title) bits.push(`“${s.title}”`);
  return <>{bits.join(" · ")}</>;
}

function CitationChip({
  n,
  source,
  weak,
}: {
  n: number;
  source: AnswerSource | undefined;
  weak: boolean;
}) {
  const [open, setOpen] = useState(false);
  if (!source) {
    return <span className="text-red-400">[{n}]</span>;
  }
  return (
    <span className="relative inline-block">
      <button
        type="button"
        onMouseEnter={() => setOpen(true)}
        onMouseLeave={() => setOpen(false)}
        onFocus={() => setOpen(true)}
        onBlur={() => setOpen(false)}
        onClick={() => {
          if (source.uri) window.open(source.uri, "_blank", "noopener");
        }}
        title={source.text}
        className={
          "mx-0.5 rounded px-1 text-xs font-semibold " +
          (weak
            ? "bg-amber-950 text-amber-300 underline decoration-amber-500 decoration-dotted underline-offset-2"
            : "bg-neutral-800 text-sky-300 hover:bg-neutral-700")
        }
      >
        [{n}]
      </button>
      {open ? (
        <span className="absolute bottom-full left-1/2 z-10 mb-1 w-72 -translate-x-1/2 rounded-lg border border-neutral-700 bg-neutral-900 p-2 text-xs font-normal text-neutral-300 shadow-xl">
          <span className="mb-1 block text-[10px] uppercase tracking-wide text-neutral-500">
            <SourceLabel s={source} />
          </span>
          <span className="line-clamp-6 block">{source.text}</span>
        </span>
      ) : null}
    </span>
  );
}

/** Split answer text on `[n]` markers, keeping the markers. */
function segments(text: string): { kind: "text" | "cite"; value: string; n?: number }[] {
  const out: { kind: "text" | "cite"; value: string; n?: number }[] = [];
  const re = /\[(\d+)\]/g;
  let last = 0;
  let m: RegExpExecArray | null;
  while ((m = re.exec(text)) !== null) {
    if (m.index > last) out.push({ kind: "text", value: text.slice(last, m.index) });
    out.push({ kind: "cite", value: m[0], n: parseInt(m[1], 10) });
    last = m.index + m[0].length;
  }
  if (last < text.length) out.push({ kind: "text", value: text.slice(last) });
  return out;
}

export default function AnswerPanel({ state }: { state: AnswerState }) {
  if (state.phase === "idle") return null;

  if (state.phase === "loading") {
    return (
      <section aria-label="answer" className="rounded-lg border border-neutral-800 bg-neutral-900/40 p-3">
        <p className="text-sm text-neutral-500">answering “{state.query}”…</p>
      </section>
    );
  }

  if (state.phase === "error") {
    return (
      <section aria-label="answer" className="rounded-lg border border-red-900 bg-red-950/40 p-3">
        <p className="text-sm text-red-300">No grounded answer: {state.message}</p>
        <p className="mt-1 text-xs text-neutral-500">
          The results list below is still available.
        </p>
      </section>
    );
  }

  const sources = state.phase === "done" ? state.done.sources : state.sources;
  const byN = new Map(sources.map((s) => [s.n, s]));
  const weakNs =
    state.phase === "done"
      ? new Set(
          state.done.weak_support.flatMap((w) => w.n.filter((n) => w.supported[n] === false)),
        )
      : new Set<number>();
  const text = state.phase === "done" ? state.done.text : state.text;
  const grounded = state.phase === "done" ? state.done.grounded : true;

  return (
    <section aria-label="answer" className="rounded-lg border border-neutral-800 bg-neutral-900/40 p-3">
      <h2 className="mb-2 text-xs font-semibold uppercase tracking-wide text-neutral-400">
        answer
        {state.phase === "streaming" ? (
          <span className="ml-2 text-neutral-600">streaming…</span>
        ) : null}
      </h2>
      {!grounded ? (
        <p className="text-sm text-amber-300">
          I couldn&apos;t find that in your data.
        </p>
      ) : (
        <p className="text-sm leading-relaxed text-neutral-200">
          {segments(text).map((seg, i) =>
            seg.kind === "text" ? (
              <span key={i}>{seg.value}</span>
            ) : (
              <CitationChip key={i} n={seg.n!} source={byN.get(seg.n!)} weak={weakNs.has(seg.n!)} />
            ),
          )}
        </p>
      )}
      {state.phase === "done" && state.done.flags.length > 0 ? (
        <ul className="mt-2 space-y-0.5">
          {state.done.flags.map((f, i) => (
            <li key={i} className="text-xs text-amber-400/90">
              ⚠ {f}
            </li>
          ))}
        </ul>
      ) : null}
      {state.phase === "done" && state.done.warnings.length > 0 ? (
        <ul className="mt-1 space-y-0.5">
          {state.done.warnings.map((w, i) => (
            <li key={i} className="text-xs text-neutral-500">
              {w}
            </li>
          ))}
        </ul>
      ) : null}
    </section>
  );
}
