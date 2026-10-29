fn main() {
  // The UI is served by the local core service (http://127.0.0.1:8000), so the webview
  // origin is remote from Tauri's point of view. Remote IPC is deny-by-default: without
  // these generated permissions every host command (`get_hotkey`, `open_source`, …) is
  // rejected with "not allowed. Plugin not found", and the UI silently loses settings.
  let attributes = tauri_build::Attributes::new().app_manifest(
    tauri_build::AppManifest::new().commands(&[
      "core_service_status",
      "core_service_url",
      "reveal_window",
      "get_hotkey",
      "set_hotkey",
      "open_source",
      "debug_log_ui",
      "debug_log_paths",
      "flush_ui_logs",
    ]),
  );
  tauri_build::try_build(attributes).expect("failed to run tauri-build");
}
