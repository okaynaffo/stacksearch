//! Desktop shell for the local-first search engine (PLAN.md §11, §13.1).
//!
//! Phase 0 proved the risky half of the integration: spawning and supervising the Python
//! core service, completing the §9 token handshake, putting a global hotkey in front of a
//! real window, and getting a server-sent event stream through WKWebView. Phase 6 turns
//! that skeleton into the product surface (§11.2/§11.3): a Spotlight-style window, a
//! menu-bar item, deep-link opening of results, and a rebindable hotkey.

mod core_service;

use std::path::PathBuf;
use std::process::Command;
use std::sync::Mutex;

use tauri::menu::{Menu, MenuItem};
use tauri::tray::TrayIconBuilder;
use tauri::{AppHandle, Emitter, Manager, State, WindowEvent};
use tauri_plugin_global_shortcut::{Code, GlobalShortcutExt, Modifiers, Shortcut, ShortcutState};

use crate::core_service::{base_url, CoreService, DEFAULT_API_PORT};

/// Hotkey used when nothing has been saved or configured.
const DEFAULT_HOTKEY: &str = "Cmd+Shift+Space";

/// Long-lived handles to the supervised child.
///
/// Mutexed because the hotkey handler, the supervisor thread, and the window event loop
/// all touch it. The lock is only ever held for short bookkeeping calls, never across the
/// child's lifetime.
struct AppState {
    service: Mutex<Option<CoreService>>,
    token: String,
    port: u16,
    /// The currently registered hotkey spec (PLAN.md §11.3 rebinding).
    hotkey: Mutex<String>,
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
        return;
    };
    let visible = window.is_visible().unwrap_or(false);
    let focused = window.is_focused().unwrap_or(false);

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
        return;
    };
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
    let mut guard = state.service.lock().unwrap();
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
    toggle_window(&app);
}

/// Current hotkey spec, for the settings UI (§11.3).
#[tauri::command]
fn get_hotkey(state: State<'_, AppState>) -> String {
    state.hotkey.lock().unwrap().clone()
}

/// Rebind the global hotkey, persisting the choice (§11.3).
///
/// A conflict is reported rather than swallowed, and the previous binding is restored so
/// a failed rebind cannot leave the app without a hotkey.
#[tauri::command]
fn set_hotkey(app: AppHandle, state: State<'_, AppState>, spec: String) -> Result<String, String> {
    parse_hotkey(&spec)?;
    let previous = state.hotkey.lock().unwrap().clone();
    let _ = app.global_shortcut().unregister_all();
    match register_hotkey(&app, &spec) {
        Ok(()) => {
            *state.hotkey.lock().unwrap() = spec.clone();
            if let Err(e) = save_hotkey(&spec) {
                eprintln!("[hotkey] could not persist {spec:?}: {e}");
            }
            Ok(spec)
        }
        Err(e) => {
            let _ = register_hotkey(&app, &previous);
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

        loop {
            std::thread::sleep(std::time::Duration::from_secs(2));

            let state = match app.try_state::<AppState>() {
                Some(s) => s,
                None => return, // the app is shutting down
            };

            let (alive, token) = {
                let mut guard = state.service.lock().unwrap();
                let alive = guard.as_mut().is_some_and(|s| s.is_alive());
                (alive, state.token.clone())
            };

            if alive {
                backoff = std::time::Duration::from_millis(500);
                continue;
            }

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

            match CoreService::start(&repo_root, token, port) {
                Ok(service) => {
                    let mut guard = state.service.lock().unwrap();
                    // Drop the previous handle before installing the new one, so its
                    // Drop cannot signal the pid we just started.
                    guard.take();
                    *guard = Some(service);
                    let _ = app.emit(
                        "service://status",
                        ServiceStatus {
                            ready: true,
                            url: base_url(port),
                            detail: "restarted".into(),
                        },
                    );
                }
                Err(e) => eprintln!("[core-service] restart failed: {e}"),
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
        .on_menu_event(|app, event| match event.id.as_ref() {
            "toggle" => toggle_window(app),
            "settings" => open_settings(app),
            "quit" => app.exit(0),
            _ => {}
        })
        .build(app)?;

    app.manage(TrayHandle {
        _tray: Mutex::new(Some(tray)),
    });
    Ok(())
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let repo_root = repo_root();
    let token = core_service::generate_token();
    let port: u16 = std::env::var("STACKSEARCH_API_PORT")
        .ok()
        .and_then(|p| p.parse().ok())
        .unwrap_or(DEFAULT_API_PORT);
    let hotkey_spec = resolve_hotkey_spec();

    // The child is started before the window is created so the window only ever loads a
    // URL that can answer. A failed start is reported in the window rather than being a
    // silent blank page — which is the whole point of surfacing it here.
    let service = CoreService::start(&repo_root, token.clone(), port);
    let startup_error = match &service {
        Err(e) => Some(e.clone()),
        Ok(_) => None,
    };

    tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .plugin(tauri_plugin_global_shortcut::Builder::new().build())
        .manage(AppState {
            service: Mutex::new(service.ok()),
            token: token.clone(),
            port,
            hotkey: Mutex::new(hotkey_spec.clone()),
        })
        .invoke_handler(tauri::generate_handler![
            core_service_status,
            core_service_url,
            reveal_window,
            get_hotkey,
            set_hotkey,
            open_source
        ])
        .setup(move |app| {
            if let Some(err) = startup_error {
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
            let url: tauri::Url = match format!("{}/?token={}", base_url(port), token).parse() {
                Ok(u) => u,
                Err(e) => {
                    eprintln!("[window] invalid service URL: {e}");
                    return Ok(());
                }
            };

            let window = tauri::WebviewWindowBuilder::new(
                app,
                "main",
                tauri::WebviewUrl::External(url),
            )
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
                    eprintln!("[window] vibrancy unavailable: {e}");
                }
            }

            // Menu-bar item (§11.3). A failure here must not abort startup: the app is
            // still fully usable from the hotkey alone.
            if let Err(e) = build_tray(app.handle()) {
                eprintln!("[tray] could not create the menu-bar item: {e}");
            }

            // Hotkey: register the resolved binding and toggle on press (§11.2, §13.1).
            if let Err(e) = register_hotkey(app.handle(), &hotkey_spec) {
                // A conflict must be reported, not swallowed: silently doing nothing on
                // the hotkey would look like a broken app.
                eprintln!(
                    "[hotkey] could not register {hotkey_spec:?}: {e}. Another app may already use it."
                );
            }

            eprintln!("[window] built for {}", base_url(port));

            let revealer = window.clone();
            std::thread::spawn(move || {
                // The child is already healthy by this point (CoreService::start waited),
                // so a short settle is enough before revealing.
                std::thread::sleep(std::time::Duration::from_millis(400));
                eprintln!("[window] show -> {:?}", revealer.show());
                eprintln!("[window] focus -> {:?}", revealer.set_focus());
            });

            // Supervision lives in this same closure on purpose: Tauri v2 honours only
            // the last `.setup()` on the builder, so a second one silently discards all
            // of the above — window creation, the tray, and the hotkey included.
            supervise(app.handle().clone(), port);

            Ok(())
        })
        .on_window_event(|window, event| {
            match event {
                // Losing focus dismisses the bar, the way Spotlight behaves (§11.2). The
                // process keeps running so the next hotkey press is instant.
                WindowEvent::Focused(false) => {
                    let _ = window.hide();
                }
                WindowEvent::Destroyed => {
                    // Exiting drops AppState and the tray, which shuts the child process
                    // down; the hotkey outlives the window, so the app should go with it
                    // rather than linger with nothing to summon.
                    window.app_handle().exit(0);
                }
                _ => {}
            }
        })
        .run(tauri::generate_context!())
        .expect("error while running the StackSearch desktop shell");
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
