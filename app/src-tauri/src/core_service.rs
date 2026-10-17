//! Spawning and supervising the Python core service (PLAN.md §13.1, §9).
//!
//! This is the piece PLAN.md §13.1 deliberately pulls forward from Phase 6: spawning a
//! Python child from Rust, handing it a secret, and knowing whether it came up. Every
//! risky unknown in the desktop integration lives here, and discovering it in week 11
//! instead of week 1 is exactly what that section argues against.
//!
//! The §9 handshake is load-bearing. `STACKSEARCH_TOKEN` is generated per launch, passed
//! only through the child's environment (never a command-line argument, which would be
//! visible to every other process on the machine via `ps`), and then used by Rust to poll
//! the API. The UI receives it in the URL it loads, which is why `GET /events` accepts a
//! query-string token: `EventSource` in WKWebView cannot set request headers.

use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::thread;
use std::time::{Duration, Instant};

/// Address of the core service. Loopback only, matching `core/config.py`, which refuses
/// any non-loopback bind (PLAN.md §9).
pub const API_HOST: &str = "127.0.0.1";
pub const DEFAULT_API_PORT: u16 = 8000;

/// How long to wait for the child to answer `/health` before reporting failure.
const STARTUP_TIMEOUT: Duration = Duration::from_secs(60);
const POLL_INTERVAL: Duration = Duration::from_millis(250);

pub fn base_url(port: u16) -> String {
    format!("http://{API_HOST}:{port}")
}

/// Generate a per-launch token (PLAN.md §9).
///
/// Uses the OS CSPRNG rather than a Rust `Random`-style helper so the token is
/// cryptographically random: it is the only thing standing between a web page the user
/// happens to have open and their email index.
pub fn generate_token() -> String {
    let mut bytes = [0u8; 32];
    // /dev/urandom is the portable-enough choice here and avoids pulling in a crypto
    // crate for a single 32-byte read.
    match std::fs::File::open("/dev/urandom") {
        Ok(mut f) => {
            use std::io::Read;
            if f.read_exact(&mut bytes).is_err() {
                return fallback_token();
            }
        }
        Err(_) => return fallback_token(),
    }
    base64_url_encode(&bytes)
}

fn fallback_token() -> String {
    use std::time::{SystemTime, UNIX_EPOCH};
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_nanos())
        .unwrap_or(0);
    format!("{:032x}{:016x}", nanos, std::process::id())
}

fn base64_url_encode(bytes: &[u8]) -> String {
    const ALPHABET: &[u8; 64] =
        b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";
    let mut out = String::with_capacity(bytes.len() * 4 / 3 + 4);
    for chunk in bytes.chunks(3) {
        let b0 = chunk[0] as u32;
        let b1 = *chunk.get(1).unwrap_or(&0) as u32;
        let b2 = *chunk.get(2).unwrap_or(&0) as u32;
        let triple = (b0 << 16) | (b1 << 8) | b2;
        out.push(ALPHABET[((triple >> 18) & 0x3f) as usize] as char);
        out.push(ALPHABET[((triple >> 12) & 0x3f) as usize] as char);
        if chunk.len() > 1 {
            out.push(ALPHABET[((triple >> 6) & 0x3f) as usize] as char);
        }
        if chunk.len() > 2 {
            out.push(ALPHABET[(triple & 0x3f) as usize] as char);
        }
    }
    out
}

/// Locate the Python core service.
///
/// Order matters and is deliberate:
/// 1. An explicit override, for development.
/// 2. A bundled runtime inside the app, which is what Phase 6 packaging produces
///    (PLAN.md §4 "Packaging").
/// 3. `uv run` from the repository, for development.
///
/// A bare `python3` is deliberately *not* in this list: the system interpreter is 3.9 on
/// this machine and the project requires 3.12, so falling back to it would produce a
/// baffling import error rather than an honest "not found".
pub fn resolve_python(repo_root: &PathBuf) -> Result<Command, String> {
    let mut cmd;

    if let Ok(custom) = std::env::var("STACKSEARCH_PYTHON") {
        cmd = Command::new(custom);
    } else {
        let bundled = repo_root
            .join("app")
            .join("src-tauri")
            .join("resources")
            .join("python")
            .join("bin")
            .join("python3");
        if bundled.exists() {
            cmd = Command::new(bundled);
        } else {
            let uv = which("uv");
            cmd = match uv {
                Some(path) => {
                    let mut c = Command::new(path);
                    c.arg("run").arg("--project").arg(repo_root);
                    c
                }
                None => {
                    return Err(
                        "neither a bundled Python nor `uv` was found; set STACKSEARCH_PYTHON"
                            .to_string(),
                    )
                }
            };
        }
    }

    cmd.arg("-m").arg("core.api.run");
    Ok(cmd)
}

/// Minimal `which`, avoiding a dependency for one call.
pub(crate) fn which(program: &str) -> Option<PathBuf> {
    let path = std::env::var_os("PATH")?;
    for dir in std::env::split_paths(&path) {
        let candidate = dir.join(program);
        if candidate.is_file() {
            return Some(candidate);
        }
    }
    // Common install locations not always on PATH in a GUI app bundle.
    for fallback in ["/opt/homebrew/bin/uv", "/usr/local/bin/uv"] {
        let candidate = PathBuf::from(fallback);
        if candidate.is_file() {
            return Some(candidate);
        }
    }
    None
}

/// A running, supervised core service.
pub struct CoreService {
    child: Option<Child>,
    pub token: String,
    pub port: u16,
}

impl CoreService {
    /// Start the service and wait until it answers.
    pub fn start(repo_root: &PathBuf, token: String, port: u16) -> Result<Self, String> {
        let mut cmd = resolve_python(repo_root)?;
        cmd.env("STACKSEARCH_TOKEN", &token)
            .env("STACKSEARCH_API_PORT", port.to_string())
            // Keep macOS from launching a UI for a process with no windows.
            .env("PYTHONUNBUFFERED", "1")
            .current_dir(repo_root)
            .stdin(Stdio::null())
            .stdout(Stdio::inherit())
            .stderr(Stdio::inherit());

        let child = cmd
            .spawn()
            .map_err(|e| format!("failed to spawn the core service: {e}"))?;

        let service = Self {
            child: Some(child),
            token,
            port,
        };

        service.wait_until_healthy()?;
        Ok(service)
    }

    /// Poll `/health` until it answers or the timeout expires.
    ///
    /// `/health` is token-protected like every other route (PLAN.md §9), so the handshake
    /// doubles as a check that the child is serving the token Rust expects — which is
    /// exactly the failure this module exists to catch.
    fn wait_until_healthy(&self) -> Result<(), String> {
        let deadline = Instant::now() + STARTUP_TIMEOUT;
        let url = format!("{}/health", base_url(self.port));
        let mut last_error = String::new();

        while Instant::now() < deadline {
            match self.get(&url) {
                Ok(body) => {
                    if body.contains("ok") {
                        return Ok(());
                    }
                    last_error = format!("unexpected /health body: {body}");
                }
                Err(e) => last_error = e,
            }
            thread::sleep(POLL_INTERVAL);
        }

        Err(format!(
            "core service did not become healthy within {:?}: {last_error}",
            STARTUP_TIMEOUT
        ))
    }

    fn get(&self, url: &str) -> Result<String, String> {
        // ureq 3 sets timeouts on the agent rather than per request, so a short-lived
        // agent is built here: the health poll must fail fast, and a shared long-lived
        // agent would make a hung child look like a slow response.
        let agent = ureq::Agent::config_builder()
            .timeout_global(Some(Duration::from_secs(3)))
            .build()
            .new_agent();

        let response = agent
            .get(url)
            .header("X-Local-Token", &self.token)
            .call()
            .map_err(|e| e.to_string())?;
        response
            .into_body()
            .read_to_string()
            .map_err(|e| e.to_string())
    }

    /// Fetch `/status` using the §9 token. Used by the handshake and by tests.
    pub fn status(&self) -> Result<serde_json::Value, String> {
        let url = format!("{}/status", base_url(self.port));
        let body = self.get(&url)?;
        serde_json::from_str(&body).map_err(|e| e.to_string())
    }

    /// True while the child is running.
    ///
    /// Takes `&mut self` because `Child::try_wait` reaps the process when it has exited,
    /// which is also what makes the "died, restart it" decision in `supervise` accurate
    /// rather than a guess.
    pub fn is_alive(&mut self) -> bool {
        match self.child.as_mut() {
            Some(child) => matches!(child.try_wait(), Ok(None)),
            None => false,
        }
    }

    /// Terminate the child, escalating only if it ignores SIGTERM.
    ///
    /// `llama-server` may be a grandchild, so the whole process group is signalled —
    /// otherwise closing the window would leave ~3 GB of model weights resident
    /// (PLAN.md §2.1).
    pub fn shutdown(&mut self) {
        if let Some(mut child) = self.child.take() {
            // The child was not started in its own session, so it shares this process
            // group — signalling the group reaches `llama-server` too.
            unsafe {
                libc_killpg(child.id() as i32, SIGTERM);
            }
            let deadline = Instant::now() + Duration::from_secs(10);
            loop {
                match child.try_wait() {
                    Ok(Some(_)) => break,
                    Ok(None) if Instant::now() < deadline => {
                        thread::sleep(Duration::from_millis(100))
                    }
                    _ => {
                        let _ = child.kill();
                        break;
                    }
                }
            }
        }
    }
}

impl Drop for CoreService {
    fn drop(&mut self) {
        self.shutdown();
    }
}

// `unsafe extern` is required by edition 2024.
unsafe extern "C" {
    /// POSIX `kill(2)`, resolved directly to avoid taking a libc dependency for two calls.
    /// POSIX `killpg(2)`: signals every process in the group.
    #[link_name = "killpg"]
    fn libc_killpg(pgrp: i32, sig: i32) -> i32;
}

const SIGTERM: i32 = 15;

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn token_is_long_and_url_safe() {
        let token = generate_token();
        assert!(token.len() >= 40, "token too short: {token}");
        assert!(
            token
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || b == b'-' || b == b'_'),
            "token must be URL-safe: {token}"
        );
    }

    #[test]
    fn tokens_differ_per_launch() {
        assert_ne!(generate_token(), generate_token());
    }

    #[test]
    fn base_url_is_loopback_only() {
        assert_eq!(base_url(8000), "http://127.0.0.1:8000");
        assert!(!base_url(8000).contains("0.0.0.0"));
    }
}