/**
 * Settings panel (PLAN.md §11.3; TODO 6.3/6.4).
 *
 * Owns everything the search view does not: source CRUD, the answer-model picker, live
 * indexing progress, the Full Disk Access affordance, and the diagnostics block that
 * replaces Phase 0's raw JSON dump.
 */

import { useCallback, useEffect, useState } from "react";

import {
  ApiError,
  SOURCE_KINDS,
  deleteSource,
  getHotkey,
  isDesktop,
  listSources,
  openSource,
  patchSource,
  createSource,
  reindexSource,
  setAnswerModel,
  setBatteryLow,
  setIdleTimeout,
  setHotkey,
  type Source,
  type Status,
} from "./api";

const SECRET_EXCLUDES = [".env", "*.pem", "id_rsa", "*.key", "*.p12"];

const KIND_DEFAULTS: Record<string, Record<string, unknown>> = {
  files: { paths: ["~/Documents"], exclude: [".git", "node_modules", ".venv", ...SECRET_EXCLUDES] },
  pdf: { paths: ["~/Documents"], exclude: [".git", "node_modules", ...SECRET_EXCLUDES] },
  email: { paths: ["~/Library/Mail"], exclude: SECRET_EXCLUDES },
  email_imap: { account: "", host: "", folders: ["INBOX"], exclude: SECRET_EXCLUDES },
  browser: { paths: ["~/Library/Safari/History.db"], exclude: SECRET_EXCLUDES },
  chat: { paths: ["~/Library/Application Support"], exclude: SECRET_EXCLUDES },
  code: { paths: ["~/Code"], exclude: [".git", "node_modules", ".venv", ...SECRET_EXCLUDES] },
};

const MODEL_FALLBACK = [
  { key: "4B", label: "Qwen3-4B (default)", size_gb: 0, present: false },
  { key: "8B", label: "Qwen3-8B (quality)", size_gb: 0, present: false },
];

function errText(e: unknown): string {
  return e instanceof ApiError ? e.message : String(e);
}

export default function Settings({
  status,
  onRefreshStatus,
}: {
  status: Status | null;
  onRefreshStatus: () => void;
}) {
  const [sources, setSources] = useState<Source[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const [kind, setKind] = useState<string>("files");
  const [name, setName] = useState("");
  const [configText, setConfigText] = useState(
    JSON.stringify(KIND_DEFAULTS.files, null, 2),
  );

  const [hotkey, setHotkeyState] = useState<string | null>(null);
  const [hotkeyDraft, setHotkeyDraft] = useState("");
  const [hotkeyMsg, setHotkeyMsg] = useState<string | null>(null);
  const [batteryLow, setBatteryLowState] = useState(false);
  const [batteryMsg, setBatteryMsg] = useState<string | null>(null);
  const [idleMinutes, setIdleMinutes] = useState("60");
  const [idleMsg, setIdleMsg] = useState<string | null>(null);
  const desktop = isDesktop();

  useEffect(() => {
    const call = getHotkey();
    if (!call) return;
    void call
      .then((h) => {
        setHotkeyState(h);
        setHotkeyDraft(h);
      })
      .catch(() => undefined);
  }, []);

  const applyHotkey = async () => {
    setHotkeyMsg(null);
    try {
      const applied = await setHotkey(hotkeyDraft.trim());
      setHotkeyState(applied);
      setHotkeyMsg(`bound to ${applied}`);
    } catch (e) {
      setHotkeyMsg(errText(e));
    }
  };

  const toggleBattery = async () => {
    setBatteryMsg(null);
    try {
      const next = !batteryLow;
      const res = await setBatteryLow(next);
      setBatteryLowState(res.battery_low);
      setBatteryMsg(res.paused_reason ?? "running");
    } catch (e) {
      setBatteryMsg(errText(e));
    }
  };

  const applyIdle = async () => {
    setIdleMsg(null);
    try {
      const mins = parseInt(idleMinutes, 10);
      const res = await setIdleTimeout(mins);
      setIdleMsg(`idle unload set to ${res.minutes} min`);
    } catch (e) {
      setIdleMsg(errText(e));
    }
  };

  const reload = useCallback(async () => {
    try {
      setSources(await listSources());
      setError(null);
    } catch (e) {
      setError(errText(e));
    }
  }, []);

  useEffect(() => {
    void reload();
  }, [reload]);

  const onKindChange = (next: string) => {
    setKind(next);
    setConfigText(JSON.stringify(KIND_DEFAULTS[next] ?? {}, null, 2));
  };

  const addSource = async () => {
    setBusy(true);
    try {
      const config = configText.trim() ? JSON.parse(configText) : {};
      await createSource({ kind, name: name.trim() || kind, config });
      setName("");
      await reload();
      onRefreshStatus();
    } catch (e) {
      setError(e instanceof SyntaxError ? "config is not valid JSON" : errText(e));
    } finally {
      setBusy(false);
    }
  };

  const toggle = async (s: Source) => {
    await patchSource(s.id, { enabled: !s.enabled });
    await reload();
    onRefreshStatus();
  };

  const reindex = async (s: Source) => {
    await reindexSource(s.id);
    onRefreshStatus();
  };

  const forget = async (s: Source) => {
    if (!window.confirm(`Forget "${s.name}"? Every document, chunk, and vector is removed.`)) {
      return;
    }
    await deleteSource(s.id);
    await reload();
    onRefreshStatus();
  };

  const switchModel = async (key: string) => {
    await setAnswerModel(key);
    onRefreshStatus();
  };

  const idx = status?.index;
  const model = status?.models.answer_llm;

  return (
    <div className="space-y-6">
      {error && (
        <p className="rounded-lg border border-red-900/60 bg-red-950/40 px-3 py-2 text-sm text-red-300">
          {error}
        </p>
      )}

      <section className="rounded-xl border border-neutral-800 bg-neutral-900/40">
        <h2 className="border-b border-neutral-800 px-3 py-2 text-xs font-semibold uppercase tracking-wide text-neutral-400">
          sources
        </h2>
        <ul className="divide-y divide-neutral-800/70">
          {sources.length === 0 && (
            <li className="px-3 py-3 text-sm text-neutral-500">no sources yet.</li>
          )}
          {sources.map((s) => (
            <li key={s.id} className="flex items-center gap-3 px-3 py-2 text-sm">
              <span className="w-16 shrink-0 rounded bg-neutral-800 px-1.5 py-0.5 text-center text-xs text-neutral-300">
                {s.kind}
              </span>
              <span className="min-w-0 flex-1">
                <span className="block truncate text-neutral-100">{s.name}</span>
                <span className="block text-xs text-neutral-500">
                  {s.documents} docs{s.errors > 0 ? ` · ${s.errors} errors` : ""}
                  {s.last_sync_at ? ` · synced ${s.last_sync_at}` : ""}
                </span>
              </span>
              <button
                type="button"
                onClick={() => void toggle(s)}
                className="rounded border border-neutral-700 px-2 py-0.5 text-xs text-neutral-300 hover:border-neutral-500"
              >
                {s.enabled ? "pause" : "enable"}
              </button>
              <button
                type="button"
                onClick={() => void reindex(s)}
                className="rounded border border-neutral-700 px-2 py-0.5 text-xs text-neutral-300 hover:border-neutral-500"
              >
                reindex
              </button>
              <button
                type="button"
                onClick={() => void forget(s)}
                className="rounded border border-red-900/70 px-2 py-0.5 text-xs text-red-300 hover:border-red-700"
              >
                forget
              </button>
            </li>
          ))}
        </ul>
        <div className="space-y-2 border-t border-neutral-800 p-3">
          <div className="flex gap-2">
            <select
              value={kind}
              onChange={(e) => onKindChange(e.target.value)}
              className="rounded border border-neutral-700 bg-neutral-950 px-2 py-1 text-sm text-neutral-200"
            >
              {SOURCE_KINDS.map((k) => (
                <option key={k} value={k}>
                  {k}
                </option>
              ))}
            </select>
            <input
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="name"
              className="flex-1 rounded border border-neutral-700 bg-neutral-950 px-2 py-1 text-sm text-neutral-200 placeholder:text-neutral-600"
            />
          </div>
          <textarea
            value={configText}
            onChange={(e) => setConfigText(e.target.value)}
            rows={3}
            spellCheck={false}
            className="w-full rounded border border-neutral-700 bg-neutral-950 px-2 py-1 font-mono text-xs text-neutral-300"
          />
          <button
            type="button"
            disabled={busy}
            onClick={() => void addSource()}
            className="rounded bg-sky-600 px-3 py-1 text-sm font-medium text-white hover:bg-sky-500 disabled:opacity-50"
          >
            add source
          </button>
        </div>
      </section>

      <section className="rounded-xl border border-neutral-800 bg-neutral-900/40">
        <h2 className="border-b border-neutral-800 px-3 py-2 text-xs font-semibold uppercase tracking-wide text-neutral-400">
          answer model
        </h2>
        <div className="space-y-1 p-3">
          {(status?.models.answer_llm_options ?? MODEL_FALLBACK).map((m) => (
            <label key={m.key} className="flex items-center gap-2 text-sm text-neutral-200">
              <input
                type="radio"
                name="answer-model"
                checked={model?.key === m.key}
                onChange={() => void switchModel(m.key)}
                className="accent-sky-500"
              />
              {m.label}
              <span className="text-xs text-neutral-500">
                {m.size_gb > 0 ? `· ${m.size_gb} GB resident` : ""}
                {m.present ? "" : " · not downloaded"}
                {model?.key === m.key
                  ? status?.llm_server.running
                    ? " · loaded"
                    : " · idle"
                  : ""}
              </span>
            </label>
          ))}
          <p className="pt-1 text-xs text-neutral-500">
            switching unloads the resident model; the next question loads the choice.
          </p>
        </div>
      </section>

      <section className="rounded-xl border border-neutral-800 bg-neutral-900/40">
        <h2 className="border-b border-neutral-800 px-3 py-2 text-xs font-semibold uppercase tracking-wide text-neutral-400">
          shortcut
        </h2>
        <div className="space-y-2 p-3">
          {desktop ? (
            <>
              <div className="flex gap-2">
                <input
                  value={hotkeyDraft}
                  onChange={(e) => setHotkeyDraft(e.target.value)}
                  placeholder="Cmd+Shift+Space"
                  className="flex-1 rounded border border-neutral-700 bg-neutral-950 px-2 py-1 font-mono text-sm text-neutral-200"
                />
                <button
                  type="button"
                  onClick={() => void applyHotkey()}
                  className="rounded bg-sky-600 px-3 py-1 text-sm font-medium text-white hover:bg-sky-500"
                >
                  bind
                </button>
              </div>
              <p className="text-xs text-neutral-500">
                {hotkeyMsg ?? `currently ${hotkey ?? "…"}`}
              </p>
            </>
          ) : (
            <p className="text-sm text-neutral-500">
              the global shortcut is only available in the desktop app.
            </p>
          )}
        </div>
      </section>

      <section className="rounded-xl border border-neutral-800 bg-neutral-900/40">
        <h2 className="border-b border-neutral-800 px-3 py-2 text-xs font-semibold uppercase tracking-wide text-neutral-400">
          indexing
        </h2>
        <dl className="grid grid-cols-2 gap-x-4 gap-y-1 p-3 text-sm">
          <Row label="documents" value={idx?.documents} />
          <Row label="chunks" value={idx?.chunks} />
          <Row label="vectors" value={idx?.vectors} />
          <Row label="queued" value={idx?.jobs_pending} />
          <Row label="worker" value={status?.worker_state} />
          <Row label="paused" value={status?.queue_paused_reason ?? "no"} />
          <Row label="embedding model" value={idx?.embedding_model ?? undefined} />
        </dl>
      </section>

      <section className="rounded-xl border border-neutral-800 bg-neutral-900/40">
        <h2 className="border-b border-neutral-800 px-3 py-2 text-xs font-semibold uppercase tracking-wide text-neutral-400">
          access
        </h2>
        <div className="space-y-2 p-3 text-sm text-neutral-300">
          <p>
            Mail, Safari, and Messages live behind Full Disk Access. If those sources
            index nothing, grant it to StackSearch.
          </p>
          <button
            type="button"
            onClick={() =>
              void openSource(
                "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles",
              )
            }
            className="rounded border border-neutral-700 px-2 py-1 text-sm text-neutral-200 hover:border-neutral-500"
          >
            open privacy settings
          </button>
        </div>
      </section>

      <section className="rounded-xl border border-neutral-800 bg-neutral-900/40">
        <h2 className="border-b border-neutral-800 px-3 py-2 text-xs font-semibold uppercase tracking-wide text-neutral-400">
          thermal &amp; memory
        </h2>
        <div className="space-y-2 p-3 text-sm text-neutral-300">
          <p>
            Pausing on battery keeps bulk indexing from running while the machine is
            unplugged; live changes still index. Idle unload (default 60 min) frees the
            answer model's RAM when unused.
          </p>
          <div className="flex items-center gap-3">
            <button
              type="button"
              onClick={() => void toggleBattery()}
              className="rounded border border-neutral-700 px-2 py-1 text-xs text-neutral-200 hover:border-neutral-500"
            >
              {batteryLow ? "resume indexing" : "simulate battery low"}
            </button>
            <span className="text-xs text-neutral-500">
              {batteryMsg ?? (batteryLow ? "battery pause active" : "on AC power")}
            </span>
          </div>
          <div className="flex items-center gap-3">
            <input
              value={idleMinutes}
              onChange={(e) => setIdleMinutes(e.target.value)}
              className="w-20 rounded border border-neutral-700 bg-neutral-950 px-2 py-1 text-sm text-neutral-200"
            />
            <button
              type="button"
              onClick={() => void applyIdle()}
              className="rounded border border-neutral-700 px-2 py-1 text-xs text-neutral-200 hover:border-neutral-500"
            >
              set idle unload (min)
            </button>
            <span className="text-xs text-neutral-500">{idleMsg ?? "default 60"}</span>
          </div>
        </div>
      </section>

      <section className="rounded-xl border border-neutral-800 bg-neutral-900/40">
        <h2 className="border-b border-neutral-800 px-3 py-2 text-xs font-semibold uppercase tracking-wide text-neutral-400">
          diagnostics
        </h2>
        <dl className="grid grid-cols-2 gap-x-4 gap-y-1 p-3 text-sm">
          <Row label="version" value={status?.version} />
          <Row label="uptime" value={status ? `${Math.round(status.uptime_s)}s` : undefined} />
          <Row label="index path" value={status?.index_path} />
          <Row label="schema" value={idx?.schema_version ?? undefined} />
          <Row label="vectors ext" value={idx?.vec_version ?? undefined} />
          <Row label="embedding dim" value={idx?.embedding_dim ?? undefined} />
          <Row label="llm server" value={status?.llm_server.running ? "running" : "stopped"} />
          <Row label="events" value={status?.events_subscribers} />
        </dl>
        {status?.index_error && (
          <p className="border-t border-neutral-800 px-3 py-2 text-xs text-red-300">
            {status.index_error}
          </p>
        )}
      </section>
    </div>
  );
}

function Row({ label, value }: { label: string; value: string | number | undefined | null }) {
  return (
    <>
      <dt className="text-neutral-500">{label}</dt>
      <dd className="truncate text-neutral-200">{value ?? "—"}</dd>
    </>
  );
}
