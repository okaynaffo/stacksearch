import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import ErrorBoundary from "./ErrorBoundary";
import { initToken } from "./api";
import * as logger from "./logger";
import "./index.css";

// Install the audit logger first, so the earliest lifecycle events and any error thrown
// during render are recorded in host.log / stacksearch.log.
logger.install();

// Read the per-launch token from ?token= (browser dev) or the injected window global
// (Tauri) before the first render, so no component ever fires an unauthenticated
// request (PLAN.md §9).
initToken();
logger.log(logger.categories.lifecycle, "token-ready", {
  has_token: Boolean(
    (window as unknown as { __STACKSEARCH_TOKEN__?: string }).__STACKSEARCH_TOKEN__,
  ),
});

// Last-resort visibility: an uncaught error outside React's render (an event handler,
// a rejected promise) must not leave a silent blank window.
function showFatal(message: string): void {
  const root = document.getElementById("root");
  if (!root) return;
  root.innerHTML =
    '<div class="fatal"><div class="fatal-card" role="alert">' +
    "<b>StackSearch hit an error.</b><pre>" +
    message.replace(/[<>&]/g, (c) => ({ "<": "&lt;", ">": "&gt;", "&": "&amp;" })[c]!) +
    "</pre></div></div>";
}
window.addEventListener("error", (e) => {
  logger.error(logger.categories.error, "fatal", {
    message: e.message,
    stack: e.error?.stack,
  });
  showFatal(String(e.error?.stack ?? e.message));
});
// Rejections are logged, not fatal: a stray unhandled promise (e.g. a rejected fetch)
// must never replace the whole UI the way the old `isDesktop()` probe did.
window.addEventListener("unhandledrejection", (e) => {
  logger.error(logger.categories.error, "unhandled-rejection", { reason: String(e.reason) });
  console.error("unhandled rejection:", e.reason);
});

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <ErrorBoundary>
      <App />
    </ErrorBoundary>
  </StrictMode>,
);