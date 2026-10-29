//! Persistent, categorized debug logging for the desktop host.
//!
//! Writes one JSON object per line to `~/Library/Logs/StackSearch/host.log` (or
//! `STACKSEARCH_LOG_DIR`). The webview and the Python core write to their own files in
//! the same directory, so a crash can be reconstructed across all three processes by
//! sorting on `ts_unix_ms`.
//!
//! Records carry `component` + `category` so noisy events (focus changes, health polls)
//! can be filtered out without losing them. Tokens and sensitive-looking fields are
//! never written: callers pass only safe metadata, and child output lines containing
//! "token" are redacted before they reach the file.

use std::fs::OpenOptions;
use std::io::Write;
use std::path::PathBuf;
use std::sync::{Mutex, OnceLock};
use std::time::{SystemTime, UNIX_EPOCH};

use serde_json::{Value, json};

/// Serializes appends from the hotkey thread, supervisor thread, window loop, and the
/// child-output pumps. Append-only writes of one short line are atomic at the OS level
/// on macOS, but the lock keeps ordering deterministic within this process.
static WRITE_LOCK: Mutex<()> = Mutex::new(());

static LOG_DIR: OnceLock<PathBuf> = OnceLock::new();

/// Maximum size of a single JSON line. Oversized records keep a truncated preview
/// instead of blowing up the file with a pasted document.
const MAX_LINE_BYTES: usize = 16_000;

pub fn log_dir() -> &'static PathBuf {
    LOG_DIR.get_or_init(|| {
        if let Ok(dir) = std::env::var("STACKSEARCH_LOG_DIR") {
            return PathBuf::from(dir);
        }
        let home = std::env::var_os("HOME")
            .map(PathBuf::from)
            .unwrap_or_else(|| PathBuf::from("."));
        home.join("Library").join("Logs").join("StackSearch")
    })
}

pub fn log_path() -> PathBuf {
    log_dir().join("host.log")
}

fn timestamp_ms() -> u128 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| duration.as_millis())
        .unwrap_or(0)
}

/// Write one categorized record. Logging failures are ignored on purpose: a full disk
/// or a missing log directory must never take the app down.
pub fn log_event(category: &str, event: &str, level: &str, data: Value) {
    let mut record = json!({
        "ts_unix_ms": timestamp_ms(),
        "level": level,
        "component": "host",
        "category": category,
        "event": event,
        "pid": std::process::id(),
        "thread": std::thread::current().name().unwrap_or("unnamed"),
        "data": data,
    });

    let mut line = serde_json::to_string(&record).unwrap_or_else(|_| "{}".to_string());
    if line.len() > MAX_LINE_BYTES {
        let preview: String = line.chars().take(MAX_LINE_BYTES / 4).collect();
        record["data"] = json!({"truncated": true, "preview": preview});
        line = serde_json::to_string(&record).unwrap_or_else(|_| "{}".to_string());
    }

    // Mirror to stderr for terminal launches; the file is the persistent copy.
    eprintln!("[host/{category}] {event} {data}");

    let _guard = WRITE_LOCK.lock().unwrap_or_else(|poisoned| poisoned.into_inner());
    let dir = log_dir();
    let _ = std::fs::create_dir_all(dir);
    if let Ok(mut file) = OpenOptions::new()
        .create(true)
        .append(true)
        .open(dir.join("host.log"))
    {
        let _ = writeln!(file, "{line}");
    }
}

pub fn info(category: &str, event: &str, data: Value) {
    log_event(category, event, "INFO", data);
}

pub fn warn(category: &str, event: &str, data: Value) {
    log_event(category, event, "WARNING", data);
}

pub fn error(category: &str, event: &str, data: Value) {
    log_event(category, event, "ERROR", data);
}

/// Redact the token line the Python core prints on startup, and any other line that
/// looks like it carries a credential.
pub fn redact(line: &str) -> String {
    let lowered = line.to_ascii_lowercase();
    if !(lowered.contains("token")
        || lowered.contains("password")
        || lowered.contains("secret")
        || lowered.contains("api_key"))
    {
        return line.to_string();
    }
    match line.find(':') {
        Some(index) => format!("{}: [REDACTED]", &line[..index]),
        None => "[REDACTED: line contained a credential]".to_string(),
    }
}

/// Persist one line of the child's stdout/stderr. Used by the pump threads in
/// `core_service`, which is the only place a Python traceback before logging starts is
/// visible.
pub fn log_core_line(stream: &str, line: &str) {
    let safe = redact(line);
    let category = if stream == "stderr" {
        "core.stderr"
    } else {
        "core.stdout"
    };
    let level = if stream == "stderr" { "WARNING" } else { "INFO" };
    log_event(category, "line", level, json!({"line": safe}));
}

/// Route panics into the log before the default hook (or abort) runs. The release
/// profile uses `panic = "abort"`, so this record is often the only evidence.
pub fn install_panic_hook() {
    let previous = std::panic::take_hook();
    std::panic::set_hook(Box::new(move |info| {
        let payload = if let Some(text) = info.payload().downcast_ref::<&str>() {
            (*text).to_string()
        } else if let Some(text) = info.payload().downcast_ref::<String>() {
            text.clone()
        } else {
            "non-string panic payload".to_string()
        };
        let location = info
            .location()
            .map(|loc| format!("{}:{}:{}", loc.file(), loc.line(), loc.column()));
        log_event(
            "host.panic",
            "panic",
            "CRITICAL",
            json!({"payload": payload, "location": location}),
        );
        previous(info);
    }));
}
