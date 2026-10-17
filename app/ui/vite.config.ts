import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

// One static build, two hosts (PLAN.md §4 "UI host" row, §12).
//
// - Phases 1-5: the core service mounts `dist/` at `/`, and `npm run dev` serves it on
//   its own port for hot reload against the same API.
// - Phase 6: the Tauri window loads the identical `dist/` assets.
//
// Nothing is rewritten at that boundary, so the build must not assume a dev server or
// bake in a host-specific base URL. Paths are relative and the API origin is resolved at
// runtime in src/api.ts.
export default defineConfig({
  plugins: [react(), tailwindcss()],
  base: "./",
  build: {
    outDir: "dist",
    emptyOutDir: true,
    // A single bundle keeps the packaged app simple; there is no code splitting
    // benefit at this size.
    rollupOptions: { output: { manualChunks: undefined } },
  },
  server: {
    port: 5173,
    strictPort: true,
    // The browser dev host keeps the UI on one origin by proxying the API to the core
    // service, exactly as the Tauri window reaches it. Without this, the UI would be a
    // different origin from the API and every request would need CORS plus a
    // hand-written token plumb-through.
    proxy: {
      "/status": { target: "http://127.0.0.1:8000", changeOrigin: false },
      "/health": { target: "http://127.0.0.1:8000", changeOrigin: false },
      "/events": { target: "http://127.0.0.1:8000", changeOrigin: false },
    },
  },
});
