/**
 * Answer panel: streams a grounded answer above the results list
 * (PLAN.md §7.4 two-speed, §11.2; TODO 4.2/4.3).
 *
 * Citations `[n]` render as chips. Hovering or focusing a chip shows the cited passage;
 * activating it opens the original source through the host deep-link command. Weakly
 * supported claims (§8.3) are underlined amber; stripped citations and the
 * no-grounded-answer path are shown explicitly rather than failing silently.
 */

import { useState } from "react";
import { openSource, type AnswerDone, type AnswerSource } from "./api";
import { AlertIcon, SparkleIcon } from "./icons";

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

function sourceLabel(s: AnswerSource): string {
  const bits = [s.source_kind ?? "source", s.date ?? "undated"];
  if (s.author) bits.push(`from ${s.author}`);
  if (s.title) bits.push(`“${s.title}”`);
  return bits.join(" · ");
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
    return (
      <span className="cite cite-missing" title="This citation has no matching source">
        [{n}]
      </span>
    );
  }

  return (
    <span className="cite-wrap">
      <button
        type="button"
        className={weak ? "cite cite-weak" : "cite"}
        aria-label={`Citation ${n}: ${source.title ?? source.source_kind ?? "source"}`}
        onMouseEnter={() => setOpen(true)}
        onMouseLeave={() => setOpen(false)}
        onFocus={() => setOpen(true)}
        onBlur={() => setOpen(false)}
        onClick={() => {
          if (source.uri) void openSource(source.uri, source.loc);
        }}
      >
        {n}
      </button>
      {open ? (
        <span className="cite-pop" role="tooltip">
          <span className="cite-pop-label">{sourceLabel(source)}</span>
          <span className="cite-pop-text">{source.text}</span>
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

function Dots() {
  return (
    <span className="dots" aria-hidden="true">
      <i />
      <i />
      <i />
    </span>
  );
}

export default function AnswerPanel({ state }: { state: AnswerState }) {
  if (state.phase === "idle") return null;

  if (state.phase === "loading") {
    return (
      <section aria-label="answer" className="panel" aria-busy="true">
        <div className="panel-head">
          <span className="panel-title">
            <SparkleIcon size={12} /> Answer
          </span>
          <span className="panel-note">
            thinking <Dots />
          </span>
        </div>
        <div className="answer-body">
          <p className="answer-muted">Reading sources for “{state.query}”…</p>
        </div>
      </section>
    );
  }

  if (state.phase === "error") {
    return (
      <section aria-label="answer" className="panel">
        <div className="panel-head">
          <span className="panel-title">
            <SparkleIcon size={12} /> Answer
          </span>
          <span className="panel-note">unavailable</span>
        </div>
        <div className="answer-body">
          <p className="answer-refusal">No grounded answer was produced.</p>
          <p className="answer-muted" style={{ marginTop: 4 }}>
            {state.message}
          </p>
          <p className="answer-muted" style={{ marginTop: 6 }}>
            The results below are still available.
          </p>
        </div>
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
  const streaming = state.phase === "streaming";

  return (
    <section aria-label="answer" className="panel" aria-busy={streaming}>
      <div className="panel-head">
        <span className="panel-title">
          <SparkleIcon size={12} /> Answer
        </span>
        <span className="panel-note">
          {streaming ? (
            <>
              streaming <Dots />
            </>
          ) : state.done.grounded ? (
            `${state.done.citations.length} citation${state.done.citations.length === 1 ? "" : "s"} · ${(
              state.done.latency_ms / 1000
            ).toFixed(1)}s`
          ) : (
            "not grounded"
          )}
        </span>
      </div>
      <div className="answer-body" aria-live={state.phase === "done" ? "polite" : "off"}>
        {!grounded ? (
          <p className="answer-refusal">I couldn’t find that in your data.</p>
        ) : (
          <p className="answer-text">
            {segments(text).map((seg, i) =>
              seg.kind === "text" ? (
                <span key={i}>{seg.value}</span>
              ) : (
                <CitationChip key={i} n={seg.n!} source={byN.get(seg.n!)} weak={weakNs.has(seg.n!)} />
              ),
            )}
            {streaming ? <span className="caret" aria-hidden="true" /> : null}
          </p>
        )}
        {state.phase === "done" &&
        (state.done.flags.length > 0 || state.done.warnings.length > 0) ? (
          <ul className="answer-notes">
            {state.done.flags.map((flag, i) => (
              <li key={`f${i}`} className="answer-note">
                <AlertIcon size={12} />
                <span>{flag}</span>
              </li>
            ))}
            {state.done.warnings.map((warning, i) => (
              <li key={`w${i}`} className="answer-note answer-note-warn">
                <AlertIcon size={12} />
                <span>{warning}</span>
              </li>
            ))}
          </ul>
        ) : null}
      </div>
    </section>
  );
}
