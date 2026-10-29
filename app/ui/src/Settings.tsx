/**
 * Settings (PLAN.md §11.3; TODO 6.3/6.4).
 *
 * A two-pane macOS-style settings window: section rail on the left, one focused pane on
 * the right. Everything the search surface does not own lives here — sources, the answer
 * model, indexing behaviour, the global shortcut, privacy, and diagnostics.
 */

import { useCallback, useEffect, useState, type ReactNode } from "react";

import * as logger from "./logger";
import {
  ActivityIcon,
  AlertIcon,
  ChatIcon,
  CheckIcon,
  CodeIcon,
  CommandIcon,
  CopyIcon,
  CpuIcon,
  DatabaseIcon,
  ExternalIcon,
  FileTextIcon,
  FolderIcon,
  GlobeIcon,
  LayersIcon,
  LockIcon,
  MailIcon,
  PauseIcon,
  PlayIcon,
  PlusIcon,
  RefreshIcon,
  ShieldIcon,
  SparkleIcon,
  TrashIcon,
} from "./icons";
import {
  Badge,
  Button,
  Dialog,
  Field,
  IconButton,
  SectionHeader,
  Stat,
  Switch,
  Toast,
  cx,
  useDismiss,
  type ToastTone,
} from "./ui";
import {
  ApiError,
  SOURCE_KINDS,
  createSource,
  deleteSource,
  getHotkey,
  isDesktop,
  listSources,
  openSource,
  patchSource,
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

type SectionId = "sources" | "answer" | "indexing" | "shortcut" | "privacy" | "advanced";

const SECTIONS: { id: SectionId; label: string; icon: ReactNode }[] = [
  { id: "sources", label: "Sources", icon: <LayersIcon size={14} /> },
  { id: "answer", label: "Answer model", icon: <SparkleIcon size={14} /> },
  { id: "indexing", label: "Indexing", icon: <ActivityIcon size={14} /> },
  { id: "shortcut", label: "Shortcut", icon: <CommandIcon size={14} /> },
  { id: "privacy", label: "Privacy", icon: <ShieldIcon size={14} /> },
  { id: "advanced", label: "Advanced", icon: <CpuIcon size={14} /> },
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

function errText(error: unknown): string {
  return error instanceof ApiError ? error.message : String(error);
}

function number(value: number | null | undefined): string {
  return value == null ? "—" : value.toLocaleString();
}

function formatUptime(seconds: number | undefined): string {
  if (seconds == null) return "—";
  if (seconds < 60) return `${Math.round(seconds)}s`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ${Math.round(seconds % 60)}s`;
  return `${Math.floor(minutes / 60)}h ${minutes % 60}m`;
}

function KindIcon({ kind }: { kind: string }) {
  switch (kind) {
    case "pdf":
    case "files":
      return <FileTextIcon size={13} />;
    case "email":
    case "email_imap":
      return <MailIcon size={13} />;
    case "browser":
      return <GlobeIcon size={13} />;
    case "chat":
      return <ChatIcon size={13} />;
    case "code":
      return <CodeIcon size={13} />;
    default:
      return <FolderIcon size={13} />;
  }
}

function workerTone(state: string | undefined): "positive" | "warning" | "neutral" {
  if (state === "idle") return "positive";
  if (state === "paused") return "warning";
  return "neutral";
}

export default function Settings({
  status,
  onRefreshStatus,
}: {
  status: Status | null;
  onRefreshStatus: () => void;
}) {
  const [section, setSection] = useState<SectionId>("sources");

  const [sources, setSources] = useState<Source[]>([]);
  const [busy, setBusy] = useState(false);
  const [busyId, setBusyId] = useState<number | null>(null);
  const [configError, setConfigError] = useState<string | null>(null);

  const [kind, setKind] = useState<string>("files");
  const [name, setName] = useState("");
  const [configText, setConfigText] = useState(
    JSON.stringify(KIND_DEFAULTS.files, null, 2) ?? "{}",
  );

  const [hotkey, setHotkeyState] = useState<string | null>(null);
  const [hotkeyDraft, setHotkeyDraft] = useState("");
  const [hotkeyMsg, setHotkeyMsg] = useState<string | null>(null);
  const [hotkeyTone, setHotkeyTone] = useState<"ok" | "error" | null>(null);
  const [hotkeyBusy, setHotkeyBusy] = useState(false);

  const [batteryLow, setBatteryLowState] = useState(false);
  const [batteryBusy, setBatteryBusy] = useState(false);
  const [batteryMsg, setBatteryMsg] = useState<string | null>(null);

  const [idleMinutes, setIdleMinutes] = useState("60");
  const [idleBusy, setIdleBusy] = useState(false);
  const [idleMsg, setIdleMsg] = useState<string | null>(null);

  const [forgetTarget, setForgetTarget] = useState<Source | null>(null);
  const [notice, setNotice] = useState<{ message: string; tone: ToastTone } | null>(null);
  const dismissNotice = useDismiss(setNotice);

  const desktop = isDesktop();
  const idx = status?.index;
  const model = status?.models.answer_llm;
  const pending = idx?.jobs_pending ?? 0;
  const pausedReason = status?.queue_paused_reason ?? null;

  const notify = (message: string, tone: ToastTone = "positive") => {
    setNotice({ message, tone });
  };

  useEffect(() => {
    const call = getHotkey();
    if (!call) return;
    void call
      .then((spec) => {
        setHotkeyState(spec);
        setHotkeyDraft(spec);
      })
      .catch((error) => {
        setHotkeyMsg(`Couldn’t read the current shortcut: ${String(error)}`);
        setHotkeyTone("error");
      });
  }, []);

  useEffect(() => {
    if (status?.battery_low != null) setBatteryLowState(status.battery_low);
  }, [status?.battery_low]);

  useEffect(() => {
    if (status?.idle_timeout_minutes != null) {
      setIdleMinutes(String(status.idle_timeout_minutes));
    }
  }, [status?.idle_timeout_minutes]);

  const reload = useCallback(async () => {
    try {
      setSources(await listSources());
    } catch {
      // The app-level banner already reports connectivity failures.
    }
  }, []);

  useEffect(() => {
    if (section === "sources") void reload();
  }, [section, reload]);

  const onKindChange = (next: string) => {
    setKind(next);
    setConfigText(JSON.stringify(KIND_DEFAULTS[next] ?? {}, null, 2) ?? "{}");
    setConfigError(null);
  };

  const addSource = async () => {
    setBusy(true);
    setConfigError(null);
    const sourceName = name.trim() || kind;
    logger.log(logger.categories.action, "settings.source.add.begin", {
      kind,
      name: sourceName,
    });
    try {
      const config = configText.trim() ? JSON.parse(configText) : {};
      await createSource({ kind, name: sourceName, config });
      setName("");
      await reload();
      onRefreshStatus();
      notify(`Added “${sourceName}”. Indexing will start shortly.`);
      logger.log(logger.categories.action, "settings.source.add.end", { kind });
    } catch (error) {
      const message =
        error instanceof SyntaxError ? "Config must be valid JSON." : errText(error);
      setConfigError(message);
      logger.error(logger.categories.action, "settings.source.add.error", { kind, message });
    } finally {
      setBusy(false);
    }
  };

  const toggle = async (source: Source) => {
    setBusyId(source.id);
    logger.log(logger.categories.action, "settings.source.toggle", {
      source_id: source.id,
      enabled: !source.enabled,
    });
    try {
      await patchSource(source.id, { enabled: !source.enabled });
      await reload();
      onRefreshStatus();
    } catch (error) {
      notify(errText(error), "danger");
      logger.error(logger.categories.action, "settings.source.toggle.error", {
        source_id: source.id,
        message: errText(error),
      });
    } finally {
      setBusyId(null);
    }
  };

  const reindex = async (source: Source) => {
    setBusyId(source.id);
    logger.log(logger.categories.action, "settings.source.reindex", { source_id: source.id });
    try {
      await reindexSource(source.id);
      onRefreshStatus();
      notify(`Reindex queued for “${source.name}”.`);
    } catch (error) {
      notify(errText(error), "danger");
      logger.error(logger.categories.action, "settings.source.reindex.error", {
        source_id: source.id,
        message: errText(error),
      });
    } finally {
      setBusyId(null);
    }
  };

  const requestForget = (source: Source) => {
    logger.log(logger.categories.action, "settings.source.forget.request", {
      source_id: source.id,
    });
    setForgetTarget(source);
  };

  const cancelForget = () => {
    if (forgetTarget) {
      logger.log(logger.categories.action, "settings.source.forget.cancelled", {
        source_id: forgetTarget.id,
      });
    }
    setForgetTarget(null);
  };

  const confirmForget = async () => {
    const source = forgetTarget;
    if (!source) return;
    setForgetTarget(null);
    setBusyId(source.id);
    logger.log(logger.categories.action, "settings.source.forget", { source_id: source.id });
    try {
      await deleteSource(source.id);
      await reload();
      onRefreshStatus();
      notify(`Removed “${source.name}”.`);
    } catch (error) {
      notify(errText(error), "danger");
      logger.error(logger.categories.action, "settings.source.forget.error", {
        source_id: source.id,
        message: errText(error),
      });
    } finally {
      setBusyId(null);
    }
  };

  const switchModel = async (key: string) => {
    logger.log(logger.categories.action, "settings.model.switch", { key });
    try {
      await setAnswerModel(key);
      onRefreshStatus();
    } catch (error) {
      notify(errText(error), "danger");
      logger.error(logger.categories.action, "settings.model.switch.error", {
        key,
        message: errText(error),
      });
    }
  };

  const applyHotkey = async () => {
    const spec = hotkeyDraft.trim();
    if (!spec) return;
    setHotkeyMsg(null);
    setHotkeyTone(null);
    setHotkeyBusy(true);
    logger.log(logger.categories.action, "settings.hotkey.bind", { spec });
    try {
      const applied = await setHotkey(spec);
      setHotkeyState(applied);
      setHotkeyDraft(applied);
      setHotkeyMsg(`Bound to ${applied}`);
      setHotkeyTone("ok");
      logger.log(logger.categories.action, "settings.hotkey.bound", { spec: applied });
    } catch (error) {
      setHotkeyMsg(errText(error));
      setHotkeyTone("error");
      logger.error(logger.categories.action, "settings.hotkey.error", {
        spec,
        message: errText(error),
      });
    } finally {
      setHotkeyBusy(false);
    }
  };

  const toggleBattery = async (next: boolean) => {
    setBatteryBusy(true);
    setBatteryMsg(null);
    logger.log(logger.categories.action, "settings.battery.toggle", { low: next });
    try {
      const response = await setBatteryLow(next);
      setBatteryLowState(response.battery_low);
      setBatteryMsg(
        response.battery_low ? "Bulk indexing paused." : "Bulk indexing resumed.",
      );
      onRefreshStatus();
    } catch (error) {
      setBatteryMsg(errText(error));
      logger.error(logger.categories.action, "settings.battery.error", {
        message: errText(error),
      });
    } finally {
      setBatteryBusy(false);
    }
  };

  const applyIdle = async () => {
    const minutes = parseInt(idleMinutes, 10);
    if (!Number.isFinite(minutes) || minutes < 1) {
      setIdleMsg("Enter at least 1 minute.");
      return;
    }
    setIdleBusy(true);
    setIdleMsg(null);
    logger.log(logger.categories.action, "settings.idle.apply", { minutes });
    try {
      const response = await setIdleTimeout(minutes);
      setIdleMsg(`Idle unload set to ${response.minutes} min.`);
      onRefreshStatus();
    } catch (error) {
      setIdleMsg(errText(error));
      logger.error(logger.categories.action, "settings.idle.error", {
        message: errText(error),
      });
    } finally {
      setIdleBusy(false);
    }
  };

  const copyPath = async (value: string | undefined, label: string) => {
    if (!value) return;
    try {
      if (!navigator.clipboard) throw new Error("clipboard unavailable");
      await navigator.clipboard.writeText(value);
      notify(`Copied ${label} path.`);
    } catch {
      notify("Couldn’t copy to the clipboard.", "danger");
    }
  };

  const revealLogs = () => {
    const dir = status?.logs?.dir;
    if (!dir) return;
    void openSource(`file://${encodeURI(dir)}`);
  };

  const openPrivacySettings = () => {
    logger.log(logger.categories.action, "settings.privacy.open", {});
    void openSource(
      "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles",
    );
  };

  const sourceList = (
    <section className="panel">
      {sources.length === 0 ? (
        <div className="result-empty">
          <FolderIcon size={20} />
          <strong>No sources yet</strong>
          <span>Add a folder below to start indexing.</span>
        </div>
      ) : (
        <ul className="results-list">
          {sources.map((source) => (
            <li key={source.id} className="setting-row">
              <span className={cx("kind-icon", !source.enabled && "opacity-50")}>
                <KindIcon kind={source.kind} />
              </span>
              <span className="min-w-0 flex-1">
                <span className="flex items-center gap-2">
                  <span className="result-title">{source.name}</span>
                  {!source.enabled ? <Badge>Paused</Badge> : null}
                  {source.errors > 0 ? (
                    <Badge tone="warning">{source.errors} errors</Badge>
                  ) : null}
                </span>
                <span className="source-meta block">
                  {KIND_LABEL[source.kind] ?? source.kind} · {number(source.documents)} docs
                  {source.last_sync_at ? ` · synced ${source.last_sync_at}` : ""}
                </span>
              </span>
              <span className="flex items-center gap-0.5">
                <IconButton
                  label={source.enabled ? `Pause ${source.name}` : `Enable ${source.name}`}
                  size="sm"
                  disabled={busyId === source.id}
                  onClick={() => void toggle(source)}
                >
                  {source.enabled ? <PauseIcon size={13} /> : <PlayIcon size={13} />}
                </IconButton>
                <IconButton
                  label={`Reindex ${source.name}`}
                  size="sm"
                  disabled={busyId === source.id}
                  onClick={() => void reindex(source)}
                >
                  <RefreshIcon size={13} />
                </IconButton>
                <IconButton
                  label={`Forget ${source.name}`}
                  tone="danger"
                  size="sm"
                  disabled={busyId === source.id}
                  onClick={() => requestForget(source)}
                >
                  <TrashIcon size={13} />
                </IconButton>
              </span>
            </li>
          ))}
        </ul>
      )}
    </section>
  );

  let pane: ReactNode;

  if (section === "sources") {
    pane = (
      <>
        <SectionHeader
          title="Sources"
          description="Folders and services StackSearch reads. Everything is indexed on this Mac."
          action={<Badge>{sources.length} configured</Badge>}
        />
        {sourceList}
        <section className="panel">
          <div className="panel-head">
            <span className="panel-title">
              <PlusIcon size={12} /> Add a source
            </span>
            <span className="panel-note">indexed locally</span>
          </div>
          <div className="form">
            <div className="form-row">
              <Field label="Kind" htmlFor="source-kind">
                <select
                  id="source-kind"
                  className="select"
                  value={kind}
                  onChange={(event) => onKindChange(event.target.value)}
                >
                  {SOURCE_KINDS.map((option) => (
                    <option key={option} value={option}>
                      {KIND_LABEL[option] ?? option}
                    </option>
                  ))}
                </select>
              </Field>
              <Field label="Name" htmlFor="source-name" hint="optional">
                <input
                  id="source-name"
                  className="input"
                  value={name}
                  onChange={(event) => setName(event.target.value)}
                  placeholder={KIND_LABEL[kind] ?? kind}
                />
              </Field>
            </div>
            <Field label="Config (JSON)" htmlFor="source-config" hint="paths and excludes">
              <textarea
                id="source-config"
                className="textarea"
                value={configText}
                onChange={(event) => setConfigText(event.target.value)}
                rows={5}
                spellCheck={false}
              />
            </Field>
            {configError ? (
              <p className="form-error" role="alert">
                <AlertIcon size={12} />
                {configError}
              </p>
            ) : null}
            <div className="form-actions">
              <span className="form-hint">
                Secrets such as <span className="mono">.env</span> and private keys are excluded
                by default.
              </span>
              <Button variant="primary" onClick={() => void addSource()} disabled={busy}>
                {busy ? "Adding…" : "Add and index"}
              </Button>
            </div>
          </div>
        </section>
      </>
    );
  } else if (section === "answer") {
    const options = status?.models.answer_llm_options ?? MODEL_FALLBACK;
    pane = (
      <>
        <SectionHeader
          title="Answer model"
          description="The local model that writes grounded answers. Switching unloads the resident model; the next question loads the choice."
        />
        <section className="panel">
          <div className="radio-list">
            {options.map((option) => {
              const active = model?.key === option.key;
              return (
                <label key={option.key} className="radio-row" data-checked={active}>
                  <input
                    type="radio"
                    className="radio"
                    name="answer-model"
                    checked={active}
                    onChange={() => void switchModel(option.key)}
                  />
                  <span className="radio-body">
                    <span className="row-title">{option.label}</span>
                    <span className="radio-meta block">
                      {option.size_gb > 0 ? `${option.size_gb} GB resident · ` : ""}
                      {option.present ? "downloaded" : "not downloaded"}
                      {active
                        ? status?.llm_server.running
                          ? " · loaded"
                          : " · idle"
                        : ""}
                    </span>
                  </span>
                  {active ? <Badge tone="accent">Selected</Badge> : null}
                </label>
              );
            })}
          </div>
        </section>
        <p className="inline-note">
          <SparkleIcon size={12} />
          Answers are extractive and cited. If model weights are missing, search still works.
        </p>
      </>
    );
  } else if (section === "indexing") {
    pane = (
      <>
        <SectionHeader
          title="Indexing"
          description="What StackSearch has read, and how it behaves under load."
          action={
            <Badge tone={pending > 0 ? "accent" : "positive"}>
              {pending > 0 ? `${pending} queued` : "up to date"}
            </Badge>
          }
        />
        <div className="stat-grid">
          <Stat label="Documents" value={number(idx?.documents)} />
          <Stat label="Chunks" value={number(idx?.chunks)} />
          <Stat label="Vectors" value={number(idx?.vectors)} />
          <Stat
            label="Queued"
            value={number(idx?.jobs_pending)}
            hint={status?.worker_state ? `worker ${status.worker_state}` : undefined}
          />
        </div>
        <section className="panel">
          <div className="panel-head">
            <span className="panel-title">Behaviour</span>
            <Badge tone={workerTone(status?.worker_state)}>
              {status?.worker_state ?? "unknown"}
            </Badge>
          </div>
          <div className="setting-row">
            <span className="min-w-0 flex-1">
              <span className="row-title block">Pause bulk indexing on battery</span>
              <span className="row-desc block">
                Live changes still index. Bulk scans wait until the Mac is on AC power.
              </span>
            </span>
            <Switch
              checked={batteryLow}
              onChange={(next) => void toggleBattery(next)}
              label="Pause bulk indexing on battery"
              disabled={batteryBusy}
            />
          </div>
          {batteryMsg || pausedReason ? (
            <p className="inline-note px-3 pb-2">
              <AlertIcon size={11} />
              {batteryMsg ?? `Queue paused: ${pausedReason}`}
            </p>
          ) : null}
          <div className="setting-row">
            <span className="min-w-0 flex-1">
              <span className="row-title block">Unload the answer model when idle</span>
              <span className="row-desc block">
                Frees roughly 3 GB after a period without questions.
              </span>
            </span>
            <input
              className="input input-min"
              inputMode="numeric"
              value={idleMinutes}
              onChange={(event) => setIdleMinutes(event.target.value)}
              aria-label="Idle unload minutes"
            />
            <span className="unit">min</span>
            <Button size="sm" onClick={() => void applyIdle()} disabled={idleBusy}>
              Apply
            </Button>
          </div>
          {idleMsg ? (
            <p className="inline-note px-3 pb-2">
              <CheckIcon size={11} />
              {idleMsg}
            </p>
          ) : null}
        </section>
        {idx?.embedding_model ? (
          <p className="inline-note">
            <DatabaseIcon size={12} /> Embedding model: {idx.embedding_model}
            {idx.embedding_dim ? ` · ${idx.embedding_dim} dimensions` : ""}
          </p>
        ) : null}
      </>
    );
  } else if (section === "shortcut") {
    pane = (
      <>
        <SectionHeader
          title="Global shortcut"
          description="Summon StackSearch from anywhere. The binding is saved for the next launch."
        />
        <section className="panel">
          <div className="form">
            <Field label="Key combination" hint={desktop ? undefined : "desktop app only"}>
              <div className="flex gap-2">
                <input
                  className="input mono"
                  value={hotkeyDraft}
                  onChange={(event) => setHotkeyDraft(event.target.value)}
                  placeholder="Cmd+Shift+Space"
                  aria-label="Global shortcut"
                  disabled={!desktop}
                  spellCheck={false}
                />
                <Button
                  variant="primary"
                  onClick={() => void applyHotkey()}
                  disabled={!desktop || hotkeyBusy || !hotkeyDraft.trim()}
                >
                  {hotkeyBusy ? "Binding…" : "Bind"}
                </Button>
              </div>
            </Field>
            <p
              className={cx(
                "inline-note",
                hotkeyTone === "ok" && "inline-note-ok",
                hotkeyTone === "error" && "inline-note-warn",
              )}
            >
              {hotkeyMsg ??
                (hotkey ? `Currently ${hotkey}` : "No shortcut loaded.")}
              {!desktop && " Only available in the desktop app."}
            </p>
            <p className="form-hint">
              If binding fails, macOS may already reserve the combination or be blocking the
              shortcut. Use the menu-bar item instead, or allow Input Monitoring in System
              Settings.
            </p>
          </div>
        </section>
      </>
    );
  } else if (section === "privacy") {
    pane = (
      <>
        <SectionHeader
          title="Privacy"
          description="StackSearch is local-first: your index, answers, and model run on this Mac."
        />
        <section className="panel">
          <div className="callout">
            <LockIcon size={15} />
            <div>
              <strong>Nothing is uploaded.</strong> Files are read in place, embedded locally,
              and the answer model runs on-device. The only network traffic is the loopback
              connection between this window and the local service.
            </div>
          </div>
        </section>
        <section className="panel">
          <div className="panel-head">
            <span className="panel-title">Full Disk Access</span>
          </div>
          <div className="callout">
            <ShieldIcon size={15} />
            <div>
              Mail, Safari, and Messages live behind macOS privacy protection. If those sources
              index nothing, grant StackSearch Full Disk Access and reindex.
            </div>
          </div>
          <div className="panel-actions">
            <Button onClick={openPrivacySettings} disabled={!desktop}>
              Open Privacy settings
              <ExternalIcon size={12} />
            </Button>
          </div>
        </section>
        <p className="inline-note">
          <AlertIcon size={11} />
          Secret-looking files (.env, private keys, certificates) are excluded by default.
        </p>
      </>
    );
  } else {
    const logs = status?.logs;
    const uptime = status ? formatUptime(status.uptime_s) : undefined;
    pane = (
      <>
        <SectionHeader
          title="Advanced"
          description="Runtime details and the categorized debug logs used to investigate problems."
          action={
            <Button size="sm" onClick={revealLogs} disabled={!logs?.dir}>
              <FolderIcon size={13} />
              Reveal logs
            </Button>
          }
        />
        <section className="panel">
          <div className="panel-head">
            <span className="panel-title">Runtime</span>
          </div>
          <dl className="kv">
            <dt>Version</dt>
            <dd>{status?.version ?? "—"}</dd>
            <dt>Uptime</dt>
            <dd>{uptime ?? "—"}</dd>
            <dt>Index path</dt>
            <dd>{status?.index_path ?? "—"}</dd>
            <dt>Schema</dt>
            <dd>{idx?.schema_version ?? "—"}</dd>
            <dt>Vectors extension</dt>
            <dd>{idx?.vec_version ?? "—"}</dd>
            <dt>Embedding dim</dt>
            <dd>{idx?.embedding_dim ?? "—"}</dd>
            <dt>Answer server</dt>
            <dd>{status?.llm_server.running ? "running" : "stopped"}</dd>
            <dt>Event subscribers</dt>
            <dd>{status?.events_subscribers ?? "—"}</dd>
          </dl>
          {status?.index_error ? (
            <p className="form-error px-3 pb-2" role="alert">
              <AlertIcon size={12} />
              {status.index_error}
            </p>
          ) : null}
        </section>
        <section className="panel">
          <div className="panel-head">
            <span className="panel-title">Debug logs</span>
            <span className="panel-note">one JSON record per line</span>
          </div>
          {[
            { label: "core", value: logs?.core },
            { label: "host", value: logs?.host },
            { label: "crash", value: logs?.faulthandler },
          ].map((entry) => (
            <div key={entry.label} className="log-path">
              <span className="log-path-label">{entry.label}</span>
              <span className="log-path-value" title={entry.value}>
                {entry.value ?? "—"}
              </span>
              <IconButton
                label={`Copy ${entry.label} log path`}
                size="sm"
                disabled={!entry.value}
                onClick={() => void copyPath(entry.value, entry.label)}
              >
                <CopyIcon size={12} />
              </IconButton>
            </div>
          ))}
          <div className="panel-foot">
            Filter by category, for example{" "}
            <code className="mono">rg &apos;&quot;category&quot;:&quot;ui.click&quot;&apos;</code>
          </div>
        </section>
      </>
    );
  }

  return (
    <div className="ss-settings">
      <nav className="ss-settings-nav" aria-label="Settings sections">
        <div className="settings-nav-label">Settings</div>
        {SECTIONS.map((entry) => (
          <button
            key={entry.id}
            type="button"
            className="settings-nav-item"
            aria-current={section === entry.id ? "page" : undefined}
            onClick={() => {
              setSection(entry.id);
              logger.log(logger.categories.nav, "settings.section", { section: entry.id });
            }}
          >
            {entry.icon}
            <span>{entry.label}</span>
            {entry.id === "sources" && sources.length > 0 ? (
              <span className="settings-nav-badge">{sources.length}</span>
            ) : null}
          </button>
        ))}
      </nav>

      <div className="ss-settings-body">
        <div className="settings-pane">{pane}</div>
      </div>

      <Dialog
        open={forgetTarget !== null}
        onClose={cancelForget}
        title={forgetTarget ? `Forget “${forgetTarget.name}”?` : "Forget source"}
        footer={
          <>
            <Button data-autofocus onClick={cancelForget}>
              Cancel
            </Button>
            <Button variant="danger" onClick={() => void confirmForget()}>
              <TrashIcon size={13} />
              Forget source
            </Button>
          </>
        }
      >
        <p>
          This removes {number(forgetTarget?.documents)} documents and their vectors from the
          index. Your files on disk are not deleted.
        </p>
        <p>This cannot be undone.</p>
      </Dialog>

      {notice ? (
        <Toast message={notice.message} tone={notice.tone} onDismiss={dismissNotice} />
      ) : null}
    </div>
  );
}
