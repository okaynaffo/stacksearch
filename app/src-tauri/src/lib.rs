//! Desktop shell for the local-first search engine (PLAN.md §11, §13.1).
//!
//! Phase 0 proved the risky half of the integration: spawning and supervising the Python
//! core service, completing the §9 token handshake, putting a global hotkey in front of a
//! real window, and getting a server-sent event stream through WKWebView. Phase 6 turns
//! that skeleton into the product surface (§11.2/§11.3): a Spotlight-style window, a
//! menu-bar item, deep-link opening of results, and a rebindable hotkey.

mod core_service;
mod debug_log;

use std::path::PathBuf;
use std::process::Command;
use std::sync::Mutex;
use std::sync::atomic::{AtomicBool, Ordering};

use serde_json::json;
use tauri::menu::{Menu, MenuItem};
use tauri::tray::TrayIconBuilder;
use tauri::{AppHandle, Emitter, Manager, State, WindowEvent};
use tauri_plugin_global_shortcut::{Code, GlobalShortcutExt, Modifiers, Shortcut, ShortcutState};

use crate::core_service::{base_url, CoreService, DEFAULT_API_PORT};

/// Hotkey used when nothing has been saved or configured.
const DEFAULT_HOTKEY: &str = "Cmd+Shift+Space";

/// Lock a mutex, recovering from poisoning rather than panicking.
///
/// The crate builds with `panic = "abort"` (§2.1 binary size), so a poisoned mutex
/// would abort the entire app. A panicked lock holder should degrade to the last
/// value, not take the window down with it.
fn lock_or_recover<T>(mutex: &Mutex<T>) -> std::sync::MutexGuard<'_, T> {
    mutex.lock().unwrap_or_else(|poisoned| poisoned.into_inner())
}

/// Long-lived handles to the supervised child.
///
/// Mutexed because the hotkey handler, the supervisor thread, and the window event loop
/// all touch it. The lock is only ever held for short bookkeeping calls, never across the
/// child's lifetime.
struct AppState {    service: Mutex<Option<CoreService>>,
    token: String,
    port: u16,
    /// The currently registered hotkey spec (PLAN.md §11.3 rebinding).
    hotkey: Mutex<String>,
    /// Whether a global hotkey is actually registered.
    ///
    /// Load-bearing for the window's blur behaviour: hiding a Spotlight-style bar only
    /// works if the user can summon it again. On hosts where `RegisterEventHotKey` fails
    /// (macOS Input Monitoring permission), blur-hiding would strand the window, so the
    /// window stays visible instead.
    hotkey_ok: AtomicBool,
    /// Whether the window has ever actually had focus.
    ///
    /// Load-bearing for launch: under LaunchServices (`open`) the window is revealed but
    /// never gains focus, so a `Focused(false)` fires immediately after `show()` and the
    /// blur-to-hide handler would hide it before the user ever saw it. Only hide on blur
    /// after the window has genuinely been focused at least once.
    ever_focused: AtomicBool,
}

/// Owns the tray icon so it is not dropped (and therefore removed) after setup.
struct TrayHandle {
    _tray: Mutex<Option<tauri::tray::TrayIcon>>,
}

/// Payload for the `service://status` event the window listens for.
#[derive(Clone, serde::Serialize)]
struct ServiceStatus {
    ready: bool,
    url: String,
    detail: String,
}

/// Parse a hotkey spec like `Cmd+Shift+Space` (PLAN.md §11.2).
///
/// Configurable because the default could not be assumed: on macOS, Command+Space and
/// Command+Shift+Space both fail to register for many users, which is why §11.3 makes the
/// binding editable rather than fixed.
fn parse_hotkey(spec: &str) -> Result<Shortcut, String> {
    let mut mods = Modifiers::empty();
    let mut key: Option<Code> = None;

    for part in spec.split(['+', '-']).map(str::trim).filter(|s| !s.is_empty()) {
        let lowered = part.to_ascii_lowercase();
        match lowered.as_str() {
            "cmd" | "command" | "super" | "meta" => mods |= Modifiers::SUPER,
            "ctrl" | "control" => mods |= Modifiers::CONTROL,
            "alt" | "option" => mods |= Modifiers::ALT,
            "shift" => mods |= Modifiers::SHIFT,
            _ => {
                // `Code`'s FromStr is case-sensitive and expects the exact variant name
                // ("Space", "KeyJ", "F5"), so try a few spellings rather than lowercasing
                // and losing the capital that matters.
                let mut candidates = vec![part.to_string()];
                let mut chars = lowered.chars();
                if let Some(first) = chars.next() {
                    let mut capitalized = String::new();
                    capitalized.push(first.to_ascii_uppercase());
                    capitalized.extend(chars);
                    candidates.push(capitalized.clone());
                    if lowered.len() == 1 {
                        // Single characters are physical letter keys: "j" -> "KeyJ".
                        candidates.push(format!("Key{}", capitalized));
                    }
                }
                let parsed = candidates
                    .iter()
                    .find_map(|c| c.parse::<Code>().ok())
                    .ok_or_else(|| format!("unknown key {part:?}"))?;
                key = Some(parsed);
            }
        }
    }

    let key = key.ok_or_else(|| "no key given".to_string())?;
    if mods.is_empty() {
        return Err("at least one modifier is required for a global hotkey".into());
    }
    Ok(Shortcut::new(Some(mods), key))
}

// --------------------------------------------------------------------------------
// Hotkey persistence (PLAN.md §11.3)
// --------------------------------------------------------------------------------

/// `~/Library/Application Support/stacksearch/desktop.json`.
///
/// Deliberately outside the repo and outside the indexed corpus: the app's own desktop
/// preferences should never show up in search results.
fn desktop_config_path() -> Option<PathBuf> {
    let home = std::env::var_os("HOME")?;
    Some(
        PathBuf::from(home)
            .join("Library")
            .join("Application Support")
            .join("stacksearch")
            .join("desktop.json"),
    )
}

fn load_saved_hotkey() -> Option<String> {
    let text = std::fs::read_to_string(desktop_config_path()?).ok()?;
    let value: serde_json::Value = serde_json::from_str(&text).ok()?;
    value.get("hotkey")?.as_str().map(str::to_string)
}

fn save_hotkey(spec: &str) -> Result<(), String> {
    let path = desktop_config_path().ok_or("HOME is not set")?;
    if let Some(dir) = path.parent() {
        std::fs::create_dir_all(dir).map_err(|e| e.to_string())?;
    }
    let body = serde_json::json!({ "hotkey": spec });
    std::fs::write(path, serde_json::to_string_pretty(&body).unwrap())
        .map_err(|e| e.to_string())
}

/// Resolve the hotkey to use at startup: saved preference, then env override, then default.
fn resolve_hotkey_spec() -> String {
    let candidate = load_saved_hotkey()
        .or_else(|| std::env::var("STACKSEARCH_HOTKEY").ok())
        .unwrap_or_else(|| DEFAULT_HOTKEY.to_string());
    if parse_hotkey(&candidate).is_ok() {
        candidate
    } else {
        eprintln!("[hotkey] cannot parse {candidate:?}; falling back to {DEFAULT_HOTKEY}");
        DEFAULT_HOTKEY.to_string()
    }
}

/// Register a hotkey that toggles the window.
///
/// The caller is responsible for unregistering any previous binding first.
fn register_hotkey(app: &AppHandle, spec: &str) -> Result<(), String> {
    let shortcut = parse_hotkey(spec)?;
    let handle = app.clone();
    app.global_shortcut()
        .on_shortcut(shortcut, move |_app, _sc, event| {
            if event.state() == ShortcutState::Pressed {
                toggle_window(&handle);
            }
        })
        .map_err(|e| format!("{e}"))
}

/// Show the window if hidden, hide it if already visible.
///
/// A Spotlight-style bar is a toggle, not a show (§11.2): pressing the hotkey again
/// should dismiss it rather than re-focusing something the user just dismissed.
fn toggle_window(app: &AppHandle) {
    let Some(window) = app.get_webview_window("main") else {
        debug_log::warn("window", "toggle-no-window", json!({}));
        return;
    };
    let visible = window.is_visible().unwrap_or(false);
    let focused = window.is_focused().unwrap_or(false);

    debug_log::info("window", "toggle", json!({"visible": visible, "focused": focused}));
    if visible && focused {
        let _ = window.hide();
    } else {
        let _ = window.show();
        let _ = window.unminimize();
        let _ = window.set_focus();
    }
}

/// Bring the window forward and ask the UI to switch to its settings view (§11.3).
///
/// The UI has no `@tauri-apps/api` dependency, so instead of an event from Rust the page
/// receives a plain DOM event. Keeping the Tauri JS API out of the bundle is deliberate:
/// the same bundle runs in a browser during development (§4 "one static build, two hosts").
fn open_settings(app: &AppHandle) {
    let Some(window) = app.get_webview_window("main") else {
        debug_log::warn("window", "open-settings-no-window", json!({}));
        return;
    };
    debug_log::info("window", "open-settings", json!({}));
    let _ = window.show();
    let _ = window.unminimize();
    let _ = window.set_focus();
    let _ = window.eval("window.dispatchEvent(new Event('stacksearch:open-settings'))");
}

// --------------------------------------------------------------------------------
// Commands
// --------------------------------------------------------------------------------

/// The service's `/status` payload, fetched by Rust with the §9 token.
#[tauri::command]
fn core_service_status(state: State<'_, AppState>) -> serde_json::Value {
    let mut guard = lock_or_recover(&state.service);
    match guard.as_mut() {
        Some(service) => match service.status() {
            Ok(value) => value,
            Err(e) => serde_json::json!({ "error": e }),
        },
        None => serde_json::json!({ "error": "core service not running" }),
    }
}

/// The base URL the window should load, so the UI can show the real address on failure.
#[tauri::command]
fn core_service_url(state: State<'_, AppState>) -> String {
    base_url(state.port)
}

/// Toggle the window from the UI, so the hotkey and an in-app affordance behave
/// identically (PLAN.md §11.2).
#[tauri::command]
fn reveal_window(app: AppHandle) {
    debug_log::info("command", "reveal_window", json!({}));
    toggle_window(&app);
}

/// Record a frontend audit event in `host.log`.
///
/// The webview already batches its records to the core's `/debug/log`; this command
/// exists for events that must be captured even when the core service is down (a
/// click that precedes a crash, an SPA lifecycle transition during shutdown).
#[tauri::command]
fn debug_log_ui(category: String, event: String, data: Option<serde_json::Value>) {
    debug_log::log_event(
        &format!("ui.{category}"),
        &event,
        "INFO",
        data.unwrap_or(serde_json::Value::Null),
    );
}

/// Where this launch is writing logs, so Settings can show them.
#[tauri::command]
fn debug_log_paths() -> serde_json::Value {
    json!({"dir": debug_log::log_dir().to_string_lossy(), "host": debug_log::log_path().to_string_lossy()})
}

/// Ask the webview to hand over its buffered records immediately.
///
/// Used before a deliberate exit, where the page may be torn down before its next
/// scheduled flush.
#[tauri::command]
fn flush_ui_logs(app: AppHandle) {
    if let Some(window) = app.get_webview_window("main") {
        let _ = window.eval(
            "window.dispatchEvent(new Event('stacksearch:flush-logs'));\
             if (window.__STACKSEARCH_FLUSH_LOGS__) window.__STACKSEARCH_FLUSH_LOGS__();",
        );
    }
}

/// Current hotkey spec, for the settings UI (§11.3).
#[tauri::command]
fn get_hotkey(state: State<'_, AppState>) -> String {
    let spec = lock_or_recover(&state.hotkey).clone();
    debug_log::info("command", "get_hotkey", json!({"spec": spec}));
    spec
}

/// Rebind the global hotkey, persisting the choice (§11.3).
///
/// A conflict is reported rather than swallowed, and the previous binding is restored so
/// a failed rebind cannot leave the app without a hotkey.
#[tauri::command]
fn set_hotkey(app: AppHandle, state: State<'_, AppState>, spec: String) -> Result<String, String> {
    debug_log::info("command", "set_hotkey", json!({"spec": spec}));
    parse_hotkey(&spec)?;
    let previous = lock_or_recover(&state.hotkey).clone();
    let _ = app.global_shortcut().unregister_all();
    match register_hotkey(&app, &spec) {
        Ok(()) => {
            *lock_or_recover(&state.hotkey) = spec.clone();
            if let Err(e) = save_hotkey(&spec) {
                eprintln!("[hotkey] could not persist {spec:?}: {e}");
                debug_log::warn("hotkey", "persist-failed", json!({"spec": spec, "error": e}));
            }
            debug_log::info("hotkey", "rebound", json!({"spec": spec}));
            Ok(spec)
        }
        Err(e) => {
            let _ = register_hotkey(&app, &previous);
            debug_log::warn(
                "hotkey",
                "rebind-failed",
                json!({"spec": spec, "error": e, "kept": previous}),
            );
            Err(format!(
                "could not register {spec:?}: {e}; kept {previous:?}. Another app may already use it."
            ))
        }
    }
}

/// Open a result's original source: a file at a line, a PDF, `message://`, `https://`, or
/// a System Settings pane (PLAN.md §6.3, §11.2).
///
/// The `loc` object is the same one stored on the chunk, so a code hit can jump to its
/// line and a PDF hit to its page rather than only opening the document.
#[tauri::command]
fn open_source(uri: String, loc: Option<serde_json::Value>) -> Result<(), String> {
    let loc = loc.unwrap_or(serde_json::Value::Null);
    debug_log::info("command", "open_source", json!({"uri": uri, "loc": loc}));
    if let Some(path) = file_uri_to_path(&uri) {
        // A PDF hit carries the page it was found on; `file://...#page=N` is the form
        // Preview understands (PLAN.md §12 "file.pdf#page=3").
        if let Some(page) = loc.get("page").and_then(serde_json::Value::as_u64) {
            return spawn_open(&format!("{uri}#page={page}"));
        }
        return open_path(&path, &loc);
    }
    spawn_open(&uri)
}

/// Convert a `file://` URI to a filesystem path, or `None` for any other scheme.
fn file_uri_to_path(uri: &str) -> Option<PathBuf> {
    if !uri.starts_with("file://") {
        return None;
    }
    tauri::Url::parse(uri).ok()?.to_file_path().ok()
}

/// Open a local path, honouring a `line` hint for code hits.
fn open_path(path: &std::path::Path, loc: &serde_json::Value) -> Result<(), String> {
    if let Some(line) = loc.get("line").and_then(serde_json::Value::as_u64)
        && let Some(code) = core_service::which("code")
    {
        // VS Code's `-g file:line` is the one editor jump that is safe to assume on a
        // developer machine; anything else would be guessing at the user's tools.
        return Command::new(code)
            .arg("-g")
            .arg(format!("{}:{line}", path.display()))
            .spawn()
            .map(|_| ())
            .map_err(|e| format!("failed to open {}: {e}", path.display()));
    }
    spawn_open(&path.to_string_lossy())
}

/// Open a URL or path with the platform launcher.
fn spawn_open(target: &str) -> Result<(), String> {
    #[cfg(target_os = "macos")]
    let (program, prefix): (&str, &[&str]) = ("open", &[]);
    #[cfg(target_os = "windows")]
    let (program, prefix): (&str, &[&str]) = ("cmd", &["/C", "start", ""]);
    #[cfg(all(unix, not(target_os = "macos")))]
    let (program, prefix): (&str, &[&str]) = ("xdg-open", &[]);

    let mut cmd = Command::new(program);
    for arg in prefix {
        cmd.arg(arg);
    }
    cmd.arg(target)
        .spawn()
        .map(|_| ())
        .map_err(|e| format!("failed to open {target}: {e}"))
}

// --------------------------------------------------------------------------------
// Supervision
// --------------------------------------------------------------------------------

/// Watch the child and restart it if it dies (PLAN.md §13.1).
///
/// Restarting rather than surfacing a dead window is deliberate: supervision is what
/// makes the desktop app usable, and a crash during indexing should be invisible. The
/// backoff is capped so a child that fails instantly at startup does not spin.
fn supervise(app: AppHandle, port: u16) {
    std::thread::spawn(move || {
        let repo_root = repo_root();
        let mut backoff = std::time::Duration::from_millis(500);
        debug_log::info("supervisor", "started", json!({"port": port}));

        loop {
            std::thread::sleep(std::time::Duration::from_secs(2));

            let state = match app.try_state::<AppState>() {
                Some(s) => s,
                None => {
                    debug_log::info("supervisor", "app-state-gone", json!({}));
                    return; // the app is shutting down
                }
            };

            let (alive, token) = {
                let mut guard = lock_or_recover(&state.service);
                let alive = guard.as_mut().is_some_and(|s| s.is_alive());
                (alive, state.token.clone())
            };

            if alive {
                backoff = std::time::Duration::from_millis(500);
                continue;
            }

            debug_log::warn("supervisor", "child-dead", json!({"port": port}));
            let _ = app.emit(
                "service://status",
                ServiceStatus {
                    ready: false,
                    url: base_url(port),
                    detail: "core service stopped; restarting".into(),
                },
            );

            std::thread::sleep(backoff);
            backoff = (backoff * 2).min(std::time::Duration::from_secs(10));

            debug_log::info("supervisor", "restarting", json!({"backoff_ms": backoff.as_millis()}));
            match CoreService::start(&repo_root, token, port) {
                Ok(service) => {
                    let mut guard = lock_or_recover(&state.service);
                    // Drop the previous handle before installing the new one, so its
                    // Drop cannot signal the pid we just started.
                    guard.take();
                    *guard = Some(service);
                    debug_log::info("supervisor", "restarted", json!({"port": port}));
                    let _ = app.emit(
                        "service://status",
                        ServiceStatus {
                            ready: true,
                            url: base_url(port),
                            detail: "restarted".into(),
                        },
                    );
                }
                Err(e) => {
                    debug_log::error("supervisor", "restart-failed", json!({"error": e}));
                    eprintln!("[core-service] restart failed: {e}");
                }
            }
        }
    });
}

/// Build the menu-bar item (PLAN.md §11.3).
///
/// The tray keeps the app reachable when the window is hidden, which is the whole point
/// of a toggle-style launcher: the hotkey hides the window, and the menu bar is how it
/// comes back without the keyboard.
fn build_tray(app: &AppHandle) -> Result<(), Box<dyn std::error::Error>> {
    let toggle = MenuItem::with_id(app, "toggle", "Show / Hide", true, None::<&str>)?;
    let settings = MenuItem::with_id(app, "settings", "Settings…", true, None::<&str>)?;
    let quit = MenuItem::with_id(app, "quit", "Quit StackSearch", true, None::<&str>)?;
    let menu = Menu::with_items(app, &[&toggle, &settings, &quit])?;

    let mut builder = TrayIconBuilder::with_id("stacksearch-tray")
        .menu(&menu)
        .show_menu_on_left_click(false)
        .tooltip("StackSearch");
    if let Some(icon) = app.default_window_icon().cloned() {
        builder = builder.icon(icon);
    }
    let tray = builder
        .on_menu_event(|app, event| {
            let id = event.id.as_ref().to_string();
            debug_log::info("tray", "menu-event", json!({"id": id}));
            match id.as_str() {
                "toggle" => toggle_window(app),
                "settings" => open_settings(app),
                "quit" => {
                    debug_log::warn("tray", "quit-requested", json!({}));
                    let _ = app.emit("host://exiting", json!({"reason": "tray-quit"}));
                    app.exit(0);
                }
                _ => {}
            }
        })
        .build(app)?;

    app.manage(TrayHandle {
        _tray: Mutex::new(Some(tray)),
    });
    Ok(())
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    debug_log::install_panic_hook();
    let repo_root = repo_root();
    let token = core_service::generate_token();
    let port: u16 = std::env::var("STACKSEARCH_API_PORT")
        .ok()
        .and_then(|p| p.parse().ok())
        .unwrap_or(DEFAULT_API_PORT);
    let hotkey_spec = resolve_hotkey_spec();

    debug_log::info(
        "host",
        "launch",
        json!({
            "version": env!("CARGO_PKG_VERSION"),
            "pid": std::process::id(),
            "port": port,
            "hotkey": hotkey_spec,
            "repo_root": repo_root.to_string_lossy(),
            "log_path": debug_log::log_path().to_string_lossy(),
            "args": std::env::args().collect::<Vec<_>>(),
        }),
    );

    // The child is started before the window is created so the window only ever loads a
    // URL that can answer. A failed start is reported in the window rather than being a
    // silent blank page — which is the whole point of surfacing it here.
    let service = CoreService::start(&repo_root, token.clone(), port);
    let startup_error = match &service {
        Err(e) => Some(e.clone()),
        Ok(_) => None,
    };
    if let Some(error) = &startup_error {
        debug_log::error(
            "core.spawn",
            "startup-failed",
            json!({
                "error": error,
                "repo_root": repo_root.to_string_lossy(),
            }),
        );
    }

    let app = tauri::Builder::default()
        // Must be the first plugin. A second launch never becomes a second app: it hands
        // its arguments to the running instance, which surfaces its window, and then the
        // new process exits. Without this, both instances race for port 8000 and the loser
        // loads the winner's UI with its own (wrong) §9 token — the "Unauthorized" banner.
        .plugin(tauri_plugin_single_instance::init(|app, _argv, _cwd| {
            debug_log::info("host", "second-instance", json!({}));
            if let Some(window) = app.get_webview_window("main") {
                let _ = window.show();
                let _ = window.unminimize();
                let _ = window.set_focus();
            }
        }))
        .plugin(tauri_plugin_opener::init())
        .plugin(tauri_plugin_global_shortcut::Builder::new().build())
        .manage(AppState {
            service: Mutex::new(service.ok()),
            token: token.clone(),
            port,
            hotkey: Mutex::new(hotkey_spec.clone()),
            hotkey_ok: AtomicBool::new(false),
            ever_focused: AtomicBool::new(false),
        })
        .invoke_handler(tauri::generate_handler![
            core_service_status,
            core_service_url,
            reveal_window,
            get_hotkey,
            set_hotkey,
            open_source,
            debug_log_ui,
            debug_log_paths,
            flush_ui_logs
        ])
        .setup(move |app| {
            if let Some(err) = &startup_error {
                debug_log::error("core.spawn", "startup-error", json!({"error": err}));
                eprintln!("[core-service] {err}");
            }

            // Create the window here rather than declaring it in tauri.conf.json.
            //
            // Two things are load-bearing, and both were learned the hard way:
            //
            // * No config-declared window and no `devUrl`. Tauri begins loading a
            //   declared window's URL immediately, and that load can complete *after* an
            //   explicit `navigate()`, silently replacing it. The result was a page that
            //   loaded from the right server but without `?token=`, so every API call
            //   401'd and the window showed nothing but an auth error.
            // * The token rides in the query string because WKWebView's EventSource
            //   cannot set request headers, which is why `GET /events` accepts ?token=
            //   (§9). The alternative is a Rust SSE proxy: more code, more ports, and
            //   more to go wrong, for a transport that already works.
            // Where the window loads from.
            //
            // Healthy: the core service origin, so the page and its API share an origin
            // and no CORS is involved (PLAN.md §9). The token rides in the query string
            // because WKWebView's EventSource cannot set request headers.
            //
            // Failed start: there is no origin to load. An External URL aimed at a dead
            // loopback port paints a blank window, and a `startup_error` query parameter
            // can never reach a page the service did not serve — so the user is left with
            // an unexplained blank window. Load the bundled build instead: it always
            // paints, and the reason arrives as an injected global for the UI to show.
            let webview_url = match &startup_error {
                Some(_) => tauri::WebviewUrl::App("index.html".into()),
                None => {
                    let mut url = match tauri::Url::parse(&format!("{}/", base_url(port))) {
                        Ok(u) => u,
                        Err(e) => {
                            eprintln!("[window] invalid service URL: {e}");
                            return Ok(());
                        }
                    };
                    url.query_pairs_mut().append_pair("token", &token);
                    tauri::WebviewUrl::External(url)
                }
            };

            // Values the UI needs before its first request, whatever origin it loaded
            // from: the §9 token, the API base (absolute when the page is not served by
            // the service), and — on a failed start — the reason to display. Injected as
            // globals so the bundled fallback page carries them without a query string.
            let init_script = format!(
                "window.__STACKSEARCH_TOKEN__={token};\
                 window.__STACKSEARCH_API_BASE__={base};\
                 window.__STACKSEARCH_STARTUP_ERROR__={err};",
                token = serde_json::to_string(&token).unwrap(),
                base = serde_json::to_string(&base_url(port)).unwrap(),
                err = serde_json::to_string(startup_error.as_deref().unwrap_or("")).unwrap(),
            );

            let window = tauri::WebviewWindowBuilder::new(app, "main", webview_url)
            .initialization_script(&init_script)
            .title("StackSearch")
            .inner_size(760.0, 620.0)
            .resizable(true)
            // Spotlight-style bar (§11.2): no title bar, floats above other windows, and
            // blurs what is behind it. Created hidden, then revealed once the service
            // answers, so the user never sees a window showing a connection error it
            // could have avoided.
            .decorations(false)
            .always_on_top(true)
            .transparent(true)
            .shadow(true)
            .visible(false)
            .build()?;

            // The blur is applied after creation because it mutates the live NSWindow.
            #[cfg(target_os = "macos")]
            {
                use window_vibrancy::{apply_vibrancy, NSVisualEffectMaterial};
                if let Err(e) = apply_vibrancy(
                    &window,
                    NSVisualEffectMaterial::HudWindow,
                    None,
                    Some(16.0),
                ) {
                    debug_log::warn("window", "vibrancy-unavailable", json!({"error": e.to_string()}));
                    eprintln!("[window] vibrancy unavailable: {e}");
                }
            }

            // Menu-bar item (§11.3). A failure here must not abort startup: the app is
            // still fully usable from the hotkey alone.
            if let Err(e) = build_tray(app.handle()) {
                debug_log::error("tray", "build-failed", json!({"error": e.to_string()}));
                eprintln!("[tray] could not create the menu-bar item: {e}");
            } else {
                debug_log::info("tray", "built", json!({}));
            }

            // Hotkey: register the resolved binding and toggle on press (§11.2, §13.1).
            match register_hotkey(app.handle(), &hotkey_spec) {
                Ok(()) => {
                    if let Some(state) = app.try_state::<AppState>() {
                        state.hotkey_ok.store(true, Ordering::Relaxed);
                    }
                }
                Err(e) => {
                    // A conflict must be reported, not swallowed: silently doing nothing on
                    // the hotkey would look like a broken app. The window also stays visible
                    // on blur in this case, so it cannot strand itself with no way back.
                    debug_log::warn(
                        "hotkey",
                        "register-failed",
                        json!({"spec": hotkey_spec, "error": e}),
                    );
                    eprintln!(
                        "[hotkey] could not register {hotkey_spec:?}: {e}. The window will stay visible; use the menu-bar item to show or hide it."
                    );
                }
            }

            debug_log::info("window", "built", json!({"url": base_url(port)}));
            eprintln!("[window] built for {}", base_url(port));

            let revealer = window.clone();
            std::thread::spawn(move || {
                // The child is already healthy by this point (CoreService::start waited),
                // so a short settle is enough before revealing.
                std::thread::sleep(std::time::Duration::from_millis(400));
                let shown = revealer.show();
                let focused = revealer.set_focus();
                debug_log::info(
                    "window",
                    "revealed",
                    json!({"show": format!("{shown:?}"), "focus": format!("{focused:?}")}),
                );
                eprintln!("[window] show -> {shown:?}");
                eprintln!("[window] focus -> {focused:?}");
            });

            // Supervision lives in this same closure on purpose: Tauri v2 honours only
            // the last `.setup()` on the builder, so a second one silently discards all
            // of the above — window creation, the tray, and the hotkey included.
            supervise(app.handle().clone(), port);

            Ok(())
        })
        .on_window_event(|window, event| {
            match event {
                // Losing focus dismisses the bar, the way Spotlight behaves (§11.2) — but
                // only when a global hotkey is actually registered. Without one (macOS
                // Input Monitoring denied), hiding would leave no way to bring the window
                // back, which is exactly the "the app disappeared" failure.
                WindowEvent::Focused(focused) => {
                    let state = window.app_handle().try_state::<AppState>();
                    let hotkey_ok = state
                        .as_ref()
                        .map(|s| s.hotkey_ok.load(Ordering::Relaxed))
                        .unwrap_or(false);
                    let ever_focused = state
                        .as_ref()
                        .map(|s| s.ever_focused.load(Ordering::Relaxed))
                        .unwrap_or(false);
                    debug_log::info(
                        "window",
                        "focus",
                        json!({"focused": focused, "hotkey_ok": hotkey_ok, "ever_focused": ever_focused}),
                    );
                    if *focused {
                        if let Some(s) = state.as_ref() {
                            s.ever_focused.store(true, Ordering::Relaxed);
                        }
                    } else if hotkey_ok && ever_focused {
                        // Hide, don't close: a Spotlight-style bar is expected to come
                        // back on the hotkey (PLAN.md §11.2).
                        debug_log::info("window", "hide-on-blur", json!({}));
                        let _ = window.hide();
                    }
                }
                WindowEvent::Destroyed => {
                    // Exiting drops AppState and the tray, which shuts the child process
                    // down; the hotkey outlives the window, so the app should go with it
                    // rather than linger with nothing to summon.
                    //
                    // This is the primary suspect for "the app quits by itself": any
                    // destroy — including one triggered while the webview is being
                    // rebuilt — takes the whole app down. The record makes the reason
                    // explicit so it can be told apart from a plain window close.
                    debug_log::warn("window", "destroyed-exiting-app", json!({}));
                    window.app_handle().exit(0);
                }
                WindowEvent::CloseRequested { .. } => {
                    debug_log::info("window", "close-requested", json!({}));
                }
                WindowEvent::Resized(size) => {
                    debug_log::log_event(
                        "window",
                        "resized",
                        "DEBUG",
                        json!({"width": size.width, "height": size.height}),
                    );
                }
                other => {
                    debug_log::log_event(
                        "window",
                        "event",
                        "DEBUG",
                        json!({"kind": format!("{other:?}")}),
                    );
                }
            }
        })
        .build(tauri::generate_context!())
        .unwrap_or_else(|e| {
            debug_log::error(
                "host",
                "run-failed",
                json!({"error": e.to_string()}),
            );
            panic!("error while running the StackSearch desktop shell: {e}");
        });

    app.run(|app_handle, event| {
        // Blur-hiding leaves no visible window, so a Dock click must explicitly summon
        // it back; macOS emits `Reopen` for that activation.
        if let tauri::RunEvent::Reopen {
            has_visible_windows,
            ..
        } = event
        {
            debug_log::info("window", "reopen", json!({"has_visible_windows": has_visible_windows}));
            if let Some(window) = app_handle.get_webview_window("main") {
                let _ = window.show();
                let _ = window.unminimize();
                let _ = window.set_focus();
            }
        }
    });
    debug_log::warn("host", "run-returned", json!({}));
}

/// Locate the repository root from the executable, for `uv run --project`.
///
/// Walks upward from the executable looking for the project marker (`pyproject.toml`
/// plus `core/api/run.py`) so a bundle launched from inside the repository tree still
/// finds the code. An explicit `STACKSEARCH_REPO_ROOT` always wins.
fn repo_root() -> PathBuf {
    if let Ok(explicit) = std::env::var("STACKSEARCH_REPO_ROOT") {
        return PathBuf::from(explicit);
    }
    if let Ok(exe) = std::env::current_exe() {
        for ancestor in exe.ancestors() {
            if ancestor.join("pyproject.toml").is_file()
                && ancestor.join("core").join("api").join("run.py").is_file()
            {
                return ancestor.to_path_buf();
            }
        }
        // Fall back to the legacy "three levels up" guess for dev binaries.
        if let Some(root) = exe.ancestors().nth(3) {
            return root.to_path_buf();
        }
    }
    PathBuf::from(".")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_modifier_aliases() {
        assert!(parse_hotkey("Cmd+Shift+Space").is_ok());
        assert!(parse_hotkey("command+shift+space").is_ok());
        assert!(parse_hotkey("ctrl-alt-k").is_ok());
    }

    #[test]
    fn requires_a_modifier_and_a_key() {
        assert!(parse_hotkey("Space").is_err());
        assert!(parse_hotkey("Cmd").is_err());
        assert!(parse_hotkey("Cmd+NotAKey").is_err());
    }

    #[test]
    fn single_letters_map_to_physical_keys() {
        let letter = parse_hotkey("Cmd+j").unwrap();
        let physical = parse_hotkey("Cmd+KeyJ").unwrap();
        assert_eq!(letter, physical);
    }

    #[test]
    fn file_uris_convert_and_others_do_not() {
        assert!(file_uri_to_path("https://example.com").is_none());
        assert!(file_uri_to_path("message://mid").is_none());
        let path = file_uri_to_path("file:///tmp/a%20b.md").expect("file URI should convert");
        assert!(path.ends_with("a b.md"), "got {path:?}");
    }
}
