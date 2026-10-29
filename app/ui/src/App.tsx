/**
 * Phase 6 shell (PLAN.md §11.1-§11.3; TODO 6.2/6.3).
 *
 * One window, two views. The top bar is the window chrome — brand, a centered
 * segmented control, and a live index status — while the footer keeps the local-only
 * promise and the shortcut hints visible. Search owns retrieval and answer streaming;
 * Settings owns sources, models, policy, and diagnostics.
 */

import { useCallback, useEffect, useState } from "react";
import * as logger from "./logger";
import SearchView from "./SearchView";
import Settings from "./Settings";
import {
  ApiError,
  fetchStatus,
  getStartupError,
  subscribeEvents,
  toggleWindow,
  type Status,
} from "./api";
import { AlertIcon, LockIcon, SearchIcon, SettingsIcon } from "./icons";
import { Kbd, StatusDot } from "./ui";

type View = "search" | "settings";

function pausedLabel(reason: string): string {
  if (reason.includes("battery")) return "on battery";
  if (reason.includes("query")) return "answering";
  return "paused";
}

export default function App() {
  const [view, setView] = useState<View>("search");
  const [status, setStatus] = useState<Status | null>(null);
  // Seed with the host's startup failure (if any) so the user sees the real reason the
  // service is unavailable rather than a generic 401 from a foreign process.
  const [error, setError] = useState<string | null>(() => getStartupError());
  const [progress, setProgress] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setStatus(await fetchStatus());
      setError(null);
    } catch (e) {
      // A startup failure is the real cause; a 401 against whatever else holds the port
      // is a symptom, so do not let the poll overwrite the clearer message.
      if (!getStartupError()) {
        setError(e instanceof ApiError ? e.message : String(e));
      }
    }
  }, []);

  useEffect(() => {
    logger.log(logger.categories.lifecycle, "app.mount", {});
    void load();
    const poll = window.setInterval(load, 3000);
    return () => {
      window.clearInterval(poll);
      logger.log(logger.categories.lifecycle, "app.unmount", {});
    };
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
  const openSettings = useCallback((from: string) => {
    logger.log(logger.categories.nav, "view.settings", { from });
    setView("settings");
  }, []);

  useEffect(() => {
    const onOpenSettings = () => openSettings("host-event");
    window.addEventListener("stacksearch:open-settings", onOpenSettings);
    return () => window.removeEventListener("stacksearch:open-settings", onOpenSettings);
  }, [openSettings]);

  // Window-level shortcuts. Escape dismisses the Spotlight-style bar (§11.2); ⌘, and
  // ⌘1/⌘2 switch views. Dialog handles its own Escape in the capture phase, so a
  // confirmation dialog never hides the window.
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.defaultPrevented) return;
      const dialogOpen = document.querySelector('[role="dialog"]') !== null;

      if (event.key === "Escape") {
        if (dialogOpen) return;
        logger.log(logger.categories.action, "escape.toggle-window", {});
        toggleWindow();
        return;
      }

      if (!(event.metaKey || event.ctrlKey) || dialogOpen) return;
      if (event.key === ",") {
        event.preventDefault();
        openSettings("shortcut");
      } else if (event.key === "1") {
        event.preventDefault();
        logger.log(logger.categories.nav, "view.search", { from: "shortcut" });
        setView("search");
      } else if (event.key === "2") {
        event.preventDefault();
        openSettings("shortcut");
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [openSettings]);

  const pending = status?.index.jobs_pending ?? 0;
  const paused = status?.queue_paused_reason ?? null;
  const bar = pending > 0 ? `${pending} queued${progress ? ` · ${progress}` : ""}` : null;

  let tone: "neutral" | "accent" | "positive" | "warning" | "danger" = "neutral";
  let statusText = "connecting";
  if (error) {
    tone = "danger";
    statusText = "offline";
  } else if (status) {
    if (paused) {
      tone = "warning";
      statusText = pausedLabel(paused);
    } else if (pending > 0) {
      tone = "accent";
      statusText = `${pending} indexing`;
    } else {
      tone = "positive";
      statusText = "ready";
    }
  }

  return (
    <div className="ss-app">
      <header className="ss-topbar" data-tauri-drag-region>
        <div className="ss-brand" data-tauri-drag-region>
          <span className="ss-brand-mark" aria-hidden="true">
            <SearchIcon size={14} />
          </span>
          <span className="ss-brand-word">StackSearch</span>
        </div>

        <nav className="segmented" aria-label="Views">
          <button
            type="button"
            aria-current={view === "search" ? "page" : undefined}
            onClick={() => {
              logger.log(logger.categories.nav, "view.search", { from: "segmented" });
              setView("search");
            }}
          >
            <SearchIcon size={12} />
            <span>Search</span>
          </button>
          <button
            type="button"
            aria-current={view === "settings" ? "page" : undefined}
            onClick={() => openSettings("segmented")}
          >
            <SettingsIcon size={12} />
            <span>Settings</span>
          </button>
        </nav>

        <div className="ss-topbar-right">
          <div
            className="status-pill"
            title={
              status
                ? `StackSearch ${status.version}${paused ? ` · ${paused}` : ""}`
                : "Connecting to the local service"
            }
          >
            <StatusDot tone={tone} />
            <span>{statusText}</span>
          </div>
        </div>
      </header>

      {error ? (
        <div className="banner-danger" role="alert">
          <AlertIcon size={15} />
          <div>
            <div className="banner-title">Can’t reach the local service</div>
            <div className="banner-detail">{error}</div>
          </div>
        </div>
      ) : null}

      <main className="ss-main">
        {view === "search" ? (
          <SearchView
            progress={bar}
            onOpenSettings={() => openSettings("empty-state")}
          />
        ) : (
          <Settings status={status} onRefreshStatus={() => void load()} />
        )}
      </main>

      <footer className="ss-footer">
        <span className="hint-local">
          <LockIcon size={11} />
          Everything stays on this Mac
        </span>
        <span className="footer-hints">
          <span className="hint">
            <Kbd>⌘K</Kbd>
            <span className="hint-label">search</span>
          </span>
          <span className="hint">
            <Kbd>⌘,</Kbd>
            <span className="hint-label">settings</span>
          </span>
          <span className="hint">
            <Kbd>esc</Kbd>
            <span className="hint-label">hide</span>
          </span>
        </span>
      </footer>
    </div>
  );
}
