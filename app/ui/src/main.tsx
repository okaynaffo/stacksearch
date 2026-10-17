import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import { initToken } from "./api";
import "./index.css";

// Read the per-launch token from ?token= (browser dev) or the injected window global
// (Tauri) before the first render, so no component ever fires an unauthenticated
// request (PLAN.md §9).
initToken();

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);