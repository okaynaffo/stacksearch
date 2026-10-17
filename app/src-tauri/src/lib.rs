//! Desktop shell for the local-first search engine (PLAN.md §11, §13.1).
//!
//! Phase 0's job here is integration, not polish: prove that a Rust process can spawn
//! and supervise the Python core service, complete the §9 token handshake, put the
//! global hotkey in front of a real window, and get a server-sent event stream through
//! WKWebView. Everything visual waits for Phase 6.

mod core_service;

use std::path::PathBuf;
use std::sync::Mutex;

use tauri::{AppHandle, Emitter, Manager, State, WindowEvent};
use tauri_plugin_global_shortcut::{Code, GlobalShortcutExt, Modifiers, Shortcut, ShortcutState};

use crate::core_service::{base_url, CoreService, DEFAULT_API_PORT};

/// Long-lived handles to the supervised child.
///
/// Mutexed because the hotkey handler, the supervisor thread, and the window event loop
/// all touch it. The lock is only ever held for short bookkeeping calls, never across the
/// child's lifetime.
struct AppState {
    service: Mutex<Option<CoreService>>,
    token: String,
    port: u16,
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
/// Configurable from the start because the default could not be assumed: on macOS,
/// Command+Space and Command+Shift+Space both fail to register. TODO 6.3 replaces this
/// with a settings UI; reading the environment now means the default can be corrected
/// without a rebuild in the meantime.
fn default_hotkey() -> Shortcut {
    let spec = std::env::var("STACKSEARCH_HOTKEY").unwrap_or_else(|_| "Cmd+Shift+Space".into());
    match parse_hotkey(&spec) {
        Ok(shortcut) => shortcut,
        Err(e) => {
            eprintln!("[hotkey] cannot parse STACKSEARCH_HOTKEY={spec:?}: {e}");
            Shortcut::new(Some(Modifiers::SUPER | Modifiers::SHIFT), Code::Space)
        }
    }
}

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

/// The service's `/status` payload, fetched by Rust with the §9 token.
///
/// Exposed to the UI as a command so the window can ask the Rust side, rather than
/// needing the token itself for anything beyond the initial page load.
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

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let repo_root = repo_root();
    let token = core_service::generate_token();
    let port: u16 = std::env::var("STACKSEARCH_API_PORT")
        .ok()
        .and_then(|p| p.parse().ok())
        .unwrap_or(DEFAULT_API_PORT);

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
        })
        .invoke_handler(tauri::generate_handler![
            core_service_status,
            core_service_url,
            reveal_window
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

            let window = tauri::WebviewWindowBuilder::new(app, "main", tauri::WebviewUrl::External(url))
                .title("StackSearch")
                .inner_size(760.0, 620.0)
                .resizable(true)
                // Created hidden, then revealed once the service answers, so the user
                // never sees a window showing a connection error it could have avoided.
                .visible(false)
                .build()?;

            // Hotkey: register, then toggle on press (PLAN.md §11.2, §13.1).
            let handle = app.handle().clone();
            let hotkey = default_hotkey();
            if let Err(e) = app.global_shortcut().on_shortcut(hotkey, move |_app, _sc, event| {
                if event.state() == ShortcutState::Pressed {
                    toggle_window(&handle);
                }
            }) {
                // A conflict must be reported, not swallowed: silently doing nothing on
                // the hotkey would look like a broken app.
                eprintln!(
                    "[hotkey] could not register {hotkey:?}: {e}. Another app may already use it."
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
                eprintln!("[window] visible = {:?}", revealer.is_visible());
                eprintln!("[window] url now = {:?}", revealer.url());
            });

            // Hotkey: register, then toggle on press (PLAN.md §11.2, §13.1).
            let handle = app.handle().clone();
            let hotkey = default_hotkey();
            if let Err(e) = app.global_shortcut().on_shortcut(hotkey, move |_app, _sc, event| {
                if event.state() == ShortcutState::Pressed {
                    toggle_window(&handle);
                }
            }) {
                // A conflict must be reported, not swallowed: silently doing nothing on
                // the hotkey would look like a broken app.
                eprintln!(
                    "[hotkey] could not register {hotkey:?}: {e}. Another app may already use it."
                );
            }

            // Supervision lives in this same closure on purpose: Tauri v2 honours only
            // the last `.setup()` on the builder, so a second one silently discards all
            // of the above — window creation and the hotkey included — with no warning.
            supervise(app.handle().clone(), port);

            Ok(())
        })
        .on_window_event(|window, event| {
            if let WindowEvent::Destroyed = event {
                // Exiting drops AppState, which shuts the child process down. The hotkey
                // outlives the window, so the app should go with it rather than linger
                // with nothing to summon. §11.3's menu-bar item arrives in Phase 6.
                window.app_handle().exit(0);
            }
        })
        .run(tauri::generate_context!())
        .expect("error while running the StackSearch desktop shell");
}

/// Locate the repository root from the executable, for `uv run --project`.
fn repo_root() -> PathBuf {
    if let Ok(explicit) = std::env::var("STACKSEARCH_REPO_ROOT") {
        return PathBuf::from(explicit);
    }
    // target/<profile>/stacksearch-app -> up three levels is the src-tauri parent.
    if let Ok(exe) = std::env::current_exe()
        && let Some(root) = exe.ancestors().nth(3) {
            return root.to_path_buf();
        }
    PathBuf::from(".")
}