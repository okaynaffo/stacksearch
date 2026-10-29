"""llama-server supervisor (PLAN.md §4.1, §6.7, §8.4).

PLAN.md §4.1 commits to shipping ``llama-server`` rather than MLX, LM Studio, or Ollama
because it is the only one of the three that satisfies §8.4's lazy-load and
idle-unload requirements. This module is that commitment in code: spawn, health-check,
supervise, and kill on idle.

The per-launch API token doubles as the LLM endpoint's ``--api-key`` (§4.1), so no other
local process can reach the model while the app is running.
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import socket
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

from core.config import LLM_MODELS, llama_args, model_path
from core.debug_log import log_event, log_exception


class LlamaServerError(RuntimeError):
    """Raised when the server cannot be started or does not become healthy."""


@dataclass
class ServerState:
    """Observable supervisor state, surfaced by ``GET /status`` (PLAN.md §10)."""

    running: bool = False
    pid: int | None = None
    model_key: str | None = None
    started_at: float | None = None
    last_used_at: float | None = None
    restarts: int = 0
    last_error: str | None = None

    def as_dict(self) -> dict:
        return {
            "running": self.running,
            "pid": self.pid,
            "model": LLM_MODELS[self.model_key].label if self.model_key in LLM_MODELS else None,
            "model_key": self.model_key,
            "uptime_s": round(time.time() - self.started_at, 1) if self.started_at else 0.0,
            "restarts": self.restarts,
            "last_error": self.last_error,
        }


class LlamaServerSupervisor:
    """Owns the lifetime of one ``llama-server`` process."""

    def __init__(
        self,
        api_token: str,
        model_key: str | None = None,
        idle_timeout_minutes: int | None = None,
    ) -> None:
        from core.config import get_settings

        settings = get_settings()
        self.api_token = api_token
        self.model_key = model_key or settings.answer_llm
        self.idle_timeout_s = (
            idle_timeout_minutes
            if idle_timeout_minutes is not None
            else settings.llm_unload_after_idle_minutes * 60
        )
        self.state = ServerState(model_key=self.model_key)
        self._proc: subprocess.Popen | None = None
        self._lock = threading.RLock()
        self._monitor: threading.Thread | None = None
        self._stop_monitor = threading.Event()

    # -- paths -------------------------------------------------------------------

    @property
    def binary(self) -> Path:
        from core.config import get_settings

        return get_settings().llama_server_bin

    @property
    def gguf(self) -> Path:
        return model_path(self.model_key)

    # -- health ------------------------------------------------------------------

    @property
    def base_url(self) -> str:
        from core.config import LLAMA_HOST, LLAMA_PORT

        return f"http://{LLAMA_HOST}:{LLAMA_PORT}"

    def is_healthy(self, timeout: float = 2.0) -> bool:
        """Probe ``/health``. Unauthenticated: llama-server's health route is open."""
        try:
            resp = httpx.get(f"{self.base_url}/health", timeout=timeout)
        except httpx.HTTPError:
            return False
        return resp.status_code == 200

    def authenticates(self, timeout: float = 2.0) -> bool:
        """True only when the endpoint accepts *this launch's* token.

        ``/health`` is unauthenticated, which makes it useless for proving that the
        process answering on the port is the one we spawned. A ``llama-server`` left
        behind by an earlier launch is also healthy, but it was started with a different
        per-launch ``--api-key`` (§4.1), so every request would fail with
        ``401 Invalid API Key``. Authenticating against ``/v1/models`` distinguishes the
        two. This is the check whose absence produced the "stream interrupted" answer.
        """
        try:
            resp = httpx.get(
                f"{self.base_url}/v1/models",
                headers={"Authorization": f"Bearer {self.api_token}"},
                timeout=timeout,
            )
        except httpx.HTTPError:
            return False
        return resp.status_code == 200

    # -- lifecycle ---------------------------------------------------------------

    def ensure_running(self, wait: bool = True) -> None:
        """Start the server if it is not already serving. Idempotent.

        Called on the first question (§8.4 lazy load). If a previous process is still
        healthy — a warm model, which is the §6.7 default "keep resident" behaviour — this
        returns immediately rather than paying the ~4s cold start.

        A ``llama-server`` left over from an earlier launch is *healthy* but was spawned
        with that launch's token, so it rejects ours. Reusing it is what produced the
        ``401 Invalid API Key`` answers, so the port is reclaimed before spawning and the
        new process is authenticated before it is trusted.
        """
        with self._lock:
            if (
                self._proc is not None
                and self._proc.poll() is None
                and self.is_healthy()
                and self.authenticates()
            ):
                self.touch()
                return

            if self._proc is not None and self._proc.poll() is None:
                # Our child is up but does not answer with our token; it cannot serve.
                self.stop("token mismatch")

            self._check_paths()
            # A stale listener (previous launch, SIGKILLed core) would make our child fail
            # to bind and then be shadowed by the old server on the health probe.
            self._reclaim_port()
            self._spawn()

        if not wait:
            return

        self.wait_until_healthy()
        if not self.authenticates():
            # Still the wrong owner — reclaim and retry once. A second failure is a real
            # error rather than something to paper over.
            log_event(
                "llm",
                "token-mismatch",
                level="WARNING",
                pid=self.state.pid,
                detail="healthy endpoint rejected the per-launch token; respawning",
            )
            with self._lock:
                self.stop("token mismatch")
                self._reclaim_port()
                self._spawn()
            self.wait_until_healthy()
            if not self.authenticates():
                self.stop("token mismatch")
                raise LlamaServerError(
                    "the LLM endpoint on the loopback port rejected this launch's token; "
                    "another llama-server may be running with a different --api-key"
                )

    def _check_paths(self) -> None:
        if not self.binary.exists():
            raise LlamaServerError(
                f"llama-server not found at {self.binary}. "
                "Run scripts/fetch_llama_server.sh first."
            )
        if not self.gguf.exists():
            raise LlamaServerError(
                f"GGUF not found at {self.gguf}. Run scripts/download_models.py first."
            )

    # -- port ownership ----------------------------------------------------------

    def _port_in_use(self) -> bool:
        from core.config import LLAMA_HOST, LLAMA_PORT

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.3)
            return sock.connect_ex((LLAMA_HOST, LLAMA_PORT)) == 0

    def _port_pids(self) -> list[int]:
        from core.config import LLAMA_PORT

        try:
            out = subprocess.run(  # noqa: S603, S607
                ["lsof", "-ti", f"tcp:{LLAMA_PORT}", "-sTCP:LISTEN"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            ).stdout
        except (OSError, subprocess.SubprocessError):
            return []
        return [int(line) for line in out.split() if line.strip().isdigit()]

    def _reclaim_port(self) -> None:
        """Terminate whatever already listens on the LLM port.

        The port is exclusively the app's model endpoint (§4.1). A leftover from a
        previous launch would keep the new child from binding and, worse, answer the
        health probe while rejecting this launch's token. Mirrors the Rust host taking
        back the API port (§13.1).
        """
        if not self._port_in_use():
            return

        pids = self._port_pids()
        log_event("llm", "port-in-use", level="WARNING", pids=pids)
        for pid in pids:
            if pid == os.getpid():
                continue
            with contextlib.suppress(OSError):
                os.kill(pid, signal.SIGTERM)

        for _ in range(50):
            if not self._port_in_use():
                return
            time.sleep(0.1)

        for pid in pids:
            if pid == os.getpid():
                continue
            with contextlib.suppress(OSError):
                os.kill(pid, signal.SIGKILL)
        for _ in range(20):
            if not self._port_in_use():
                return
            time.sleep(0.1)
        log_event("llm", "port-reclaim-failed", level="ERROR", pids=pids)

    def _spawn(self) -> None:
        argv = llama_args(self.gguf, api_key=self.api_token)
        # macOS: a killed child must not outlive this process.
        env = dict(os.environ)
        # Keep Metal off the critical path for deterministic benchmarking.
        env.setdefault("GGML_METAL_LOG_LEVEL", "0")

        self.state.last_error = None
        try:
            self._proc = subprocess.Popen(  # noqa: S603
                argv,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                env=env,
                start_new_session=True,
            )
        except OSError as exc:
            log_exception("llm", "spawn-failed", exc, binary=str(self.binary), gguf=str(self.gguf))
            raise LlamaServerError(f"failed to spawn llama-server: {exc}") from exc

        now = time.time()
        self.state.running = True
        self.state.pid = self._proc.pid
        self.state.model_key = self.model_key
        self.state.started_at = now
        self.state.last_used_at = now
        # Never log `argv`: it carries the per-launch token as `--api-key` (§4.1).
        log_event(
            "llm",
            "spawned",
            pid=self._proc.pid,
            model_key=self.model_key,
            binary=str(self.binary),
            gguf=str(self.gguf),
        )

    def wait_until_healthy(self) -> None:
        """Block until ``/health`` answers, or raise with the server's own stderr."""
        from core.config import LLAMA_STARTUP_POLL_S, LLAMA_STARTUP_TIMEOUT_S

        deadline = time.time() + LLAMA_STARTUP_TIMEOUT_S
        while time.time() < deadline:
            if self._proc is not None and self._proc.poll() is not None:
                stderr = self._drain_stderr()
                self.state.running = False
                self.state.last_error = stderr or "llama-server exited during startup"
                log_event(
                    "llm",
                    "exited-during-startup",
                    level="ERROR",
                    pid=self.state.pid,
                    returncode=self._proc.returncode,
                    stderr=stderr[-4000:],
                )
                raise LlamaServerError(self.state.last_error)
            if self.is_healthy():
                log_event("llm", "healthy", pid=self.state.pid, model_key=self.model_key)
                return
            time.sleep(LLAMA_STARTUP_POLL_S)

        log_event("llm", "startup-timeout", level="ERROR", model_key=self.model_key)
        self.stop()
        raise LlamaServerError(
            f"llama-server did not become healthy within {LLAMA_STARTUP_TIMEOUT_S:.0f}s"
        )

    def set_idle_timeout(self, minutes: int) -> None:
        """Update the idle-unload window (PLAN.md §6.7, §11.3)."""
        self.idle_timeout_s = max(1, minutes) * 60

    def touch(self) -> None:
        """Mark the server as in-use, deferring the §6.7 idle unload."""
        self.state.last_used_at = time.time()

    def stop(self, reason: str = "requested") -> None:
        """Terminate the server and reclaim its weights (PLAN.md §2.1, §6.7)."""
        with self._lock:
            proc = self._proc
            self._proc = None
            self.state.running = False
            self.state.pid = None
            self.state.started_at = None

        if proc is None or proc.poll() is not None:
            return

        log_event("llm", "stopping", pid=proc.pid, reason=reason)
        # TERM first so llama-server can release the Metal allocation cleanly; KILL only
        # as a backstop.
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            proc.terminate()

        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                proc.kill()
            proc.wait(timeout=10)

        log_event("llm", "stopped", pid=proc.pid, reason=reason, returncode=proc.returncode)
        self.state.last_error = f"stopped: {reason}" if self.state.last_error is None else None

    # -- idle unload (§6.7 state 4) ----------------------------------------------

    def idle_seconds(self) -> float | None:
        if not self.state.running or self.state.last_used_at is None:
            return None
        return time.time() - self.state.last_used_at

    def should_unload(self) -> bool:
        """True once the idle window has elapsed.

        §6.7 defaults to *keep resident*: process-kill happens only on this timeout, not
        on every state transition, because paying the ~4s cold start each search session
        would defeat §7.4's two-speed design.
        """
        idle = self.idle_seconds()
        return idle is not None and idle > self.idle_timeout_s

    def start_idle_monitor(self, on_unload=None) -> None:
        """Background thread that unloads once idle (PLAN.md §6.7)."""

        def loop() -> None:
            while not self._stop_monitor.wait(5.0):
                if self.should_unload():
                    self.stop("idle timeout")
                    if on_unload:
                        on_unload()

        self._stop_monitor.clear()
        self._monitor = threading.Thread(target=loop, name="llama-idle-monitor", daemon=True)
        self._monitor.start()

    def _drain_stderr(self) -> str:
        """Read the tail of stderr, for error messages. Requires a dead process."""
        if self._proc is None or self._proc.stderr is None:
            return ""
        try:
            data = self._proc.stderr.read() or b""
        except (ValueError, OSError):
            return ""
        return data.decode("utf-8", errors="replace")[-2000:]

    # -- introspection -----------------------------------------------------------

    def server_info(self) -> dict:
        """Metadata reported by llama-server, when running."""
        try:
            resp = httpx.get(
                f"{self.base_url}/v1/models",
                headers={"Authorization": f"Bearer {self.api_token}"},
                timeout=2.0,
            )
            if resp.status_code == 200:
                return resp.json()
        except httpx.HTTPError:
            pass
        return {}

    def token_doubling_timings(self) -> dict | None:
        """Timings block from llama-server, used by the §0.2 benchmark."""
        try:
            resp = httpx.get(
                f"{self.base_url}/slots",
                headers={"Authorization": f"Bearer {self.api_token}"},
                timeout=2.0,
            )
            if resp.status_code == 200:
                payload = resp.json()
                if payload:
                    return payload[0].get("timings")
        except (httpx.HTTPError, IndexError, AttributeError):
            pass
        return None


# --------------------------------------------------------------------------------
# process-wide singleton
# --------------------------------------------------------------------------------

_supervisor: LlamaServerSupervisor | None = None
_supervisor_lock = threading.Lock()


def get_supervisor(api_token: str | None = None) -> LlamaServerSupervisor:
    """Shared supervisor for the core service."""
    global _supervisor
    from core.config import get_settings

    settings = get_settings()
    token = api_token or settings.api_token

    with _supervisor_lock:
        if _supervisor is None or _supervisor.api_token != token:
            _supervisor = LlamaServerSupervisor(token)
        return _supervisor


def shutdown_supervisor(reason: str = "core shutdown") -> None:
    """Stop the shared ``llama-server`` when the core exits.

    The child is started with ``start_new_session=True`` so it is not reached by the
    process-group SIGTERM the Rust host sends to the core (§13.1). Without this explicit
    stop the weights stay resident and, worse, the next launch finds the old server on
    the port and talks to it with a new token — the ``401 Invalid API Key`` bug. Called
    from the FastAPI lifespan's ``finally`` so SIGTERM/SIGINT both reach it.
    """
    global _supervisor
    with _supervisor_lock:
        supervisor = _supervisor
        _supervisor = None
    if supervisor is not None:
        supervisor.stop(reason)


def sse_lines(text: str) -> list[dict]:
    """Parse an SSE body into JSON payload dicts.

    Shared by the client and the benchmark so both agree on llama.cpp's wire format,
    which uses ``data:`` lines with a ``[DONE]`` sentinel.
    """
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if payload == "[DONE]":
            break
        try:
            out.append(json.loads(payload))
        except json.JSONDecodeError:
            continue
    return out
