"""Categorized, persistent debug logging for StackSearch.

The app writes **one JSON object per line** so logs are both human-greppable and
machine-parseable.  Records are categorized by ``component`` + ``category`` so a
crash investigation can filter out the noise (3-second ``/status`` polls, index
progress) without losing any events.

Files (default ``~/Library/Logs/StackSearch/``):

* ``stacksearch.log`` — core service + UI events forwarded by the webview.
* ``host.log`` — Tauri shell events (written by the Rust host).
* ``faulthandler.log`` — native crash stacks (ONNX / llama-server).

Everything is local; no record ever leaves the machine.  The per-launch API
token is never logged: request query strings and sensitive field names are
redacted before a record is formatted.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import platform
import re
import sys
import threading
import time
import traceback
import uuid
from contextvars import ContextVar
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

#: Logger name. Kept private to this module so uvicorn/FastAPI loggers are untouched.
LOGGER_NAME = "stacksearch.debug"

#: Default log level written to disk. DEBUG keeps poll traffic available but filterable.
_FILE_LEVEL = logging.DEBUG

#: Rotate at 10 MB, keep five generations (~60 MB total per log file).
_MAX_BYTES = 10 * 1024 * 1024
_BACKUP_COUNT = 5

#: Truncate giant values (chunk text, request bodies) so one record stays usable.
_MAX_VALUE_CHARS = 4000

#: Field names whose values must never reach the log.
_SENSITIVE_KEYS = {
    "token",
    "api_token",
    "access_token",
    "authorization",
    "cookie",
    "password",
    "passwd",
    "secret",
    "api_key",
    "apikey",
    "private_key",
    "client_secret",
}

#: Request id for the current task/thread, injected by the HTTP middleware.
_request_id: ContextVar[str | None] = ContextVar("stacksearch_request_id", default=None)

_configured = False
_configure_lock = threading.Lock()
_faulthandler_file: Any = None
_hooks_installed = False
_started_at = time.time()

_REDACTED = "[REDACTED]"


def log_dir() -> Path:
    """Directory holding every StackSearch log file."""
    override = os.environ.get("STACKSEARCH_LOG_DIR")
    if override:
        return Path(override).expanduser()
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Logs" / "StackSearch"
    state_home = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(state_home) / "stacksearch" / "logs"


def log_paths() -> dict[str, str]:
    """The paths the UI/Settings and ``/status`` can show."""
    directory = log_dir()
    return {
        "dir": str(directory),
        "core": str(directory / "stacksearch.log"),
        "host": str(directory / "host.log"),
        "faulthandler": str(directory / "faulthandler.log"),
    }


def set_request_id(value: str | None) -> None:
    """Bind a request id to the current context (set/reset by the middleware)."""
    _request_id.set(value)


def get_request_id() -> str | None:
    return _request_id.get()


def new_request_id() -> str:
    return uuid.uuid4().hex[:12]


# --------------------------------------------------------------------------------
# Sanitising
# --------------------------------------------------------------------------------


def _truncate(text: str) -> str:
    if len(text) <= _MAX_VALUE_CHARS:
        return text
    return f"{text[:_MAX_VALUE_CHARS]}…[{len(text) - _MAX_VALUE_CHARS} chars truncated]"


def sanitize(value: Any, *, depth: int = 0) -> Any:
    """Return a JSON-safe, size-bounded, secret-free copy of ``value``."""
    if depth > 6:
        return "…[depth limit]"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return _truncate(value)
    if isinstance(value, (bytes, bytearray)):
        return f"<{len(value)} bytes>"
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, BaseException):
        return {
            "type": type(value).__name__,
            "message": _truncate(str(value)),
            "traceback": _truncate("".join(traceback.format_exception(value))),
        }
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= 60:
                out["…"] = "[more keys truncated]"
                break
            name = str(key)
            if name.lower() in _SENSITIVE_KEYS or any(
                marker in name.lower() for marker in ("password", "secret", "token")
            ):
                out[name] = _REDACTED
            else:
                out[name] = sanitize(item, depth=depth + 1)
        return out
    if isinstance(value, (list, tuple, set)):
        items = list(value)
        out = [sanitize(item, depth=depth + 1) for item in items[:60]]
        if len(items) > 60:
            out.append(f"…[{len(items) - 60} items truncated]")
        return out
    return _truncate(repr(value))


_QUERY_SECRET = re.compile(
    r"(?i)(token|api[_-]?key|password|secret)=([^&\s]+)"
)


def redact_query(query: str) -> str:
    """Remove token-looking query parameters (``/events?token=…``)."""
    return _QUERY_SECRET.sub(lambda m: f"{m.group(1)}={_REDACTED}", query or "")


# --------------------------------------------------------------------------------
# Logging setup
# --------------------------------------------------------------------------------


class _ConsoleFormatter(logging.Formatter):
    """Human-readable stderr mirror; the file keeps the raw JSON line."""

    def format(self, record: logging.LogRecord) -> str:
        payload = getattr(record, "ss_record", None)
        if not isinstance(payload, dict):
            return super().format(record)
        stamp = payload.get("ts", "")
        level = payload.get("level", record.levelname)
        component = payload.get("component", "?")
        category = payload.get("category", "?")
        event = payload.get("event", "?")
        data = payload.get("data") or {}
        request_id = payload.get("request_id")
        suffix = f" req={request_id}" if request_id else ""
        detail = ""
        if data:
            try:
                detail = " " + json.dumps(data, ensure_ascii=False, default=str)
            except Exception:  # noqa: BLE001 — formatting must never break logging
                detail = " <unserializable>"
        return f"{stamp} {level:<5} [{component}/{category}] {event}{suffix}{detail}"


def setup_logging(*, console: bool = True) -> None:
    """Install the rotating JSONL handler. Idempotent and safe to call early."""
    global _configured
    with _configure_lock:
        if _configured:
            return
        directory = log_dir()
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError:
            # If the log directory cannot be created, fall back to stderr only rather
            # than taking the app down — logging must never be the reason it fails.
            console = True
        logger = logging.getLogger(LOGGER_NAME)
        logger.setLevel(logging.DEBUG)
        logger.propagate = False
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
        if directory.is_dir():
            try:
                file_handler = logging.handlers.RotatingFileHandler(
                    directory / "stacksearch.log",
                    maxBytes=_MAX_BYTES,
                    backupCount=_BACKUP_COUNT,
                    encoding="utf-8",
                )
                file_handler.setLevel(_FILE_LEVEL)
                file_handler.setFormatter(logging.Formatter("%(message)s"))
                logger.addHandler(file_handler)
            except OSError:
                console = True
        if console:
            stream_handler = logging.StreamHandler(stream=sys.stderr)
            stream_handler.setLevel(
                os.environ.get("STACKSEARCH_LOG_LEVEL", "INFO").upper()
            )
            stream_handler.setFormatter(_ConsoleFormatter())
            logger.addHandler(stream_handler)
        _configured = True
    install_exception_hooks()


def _ensure_configured() -> None:
    if not _configured:
        setup_logging()


def _record(
    level: str,
    category: str,
    event: str,
    *,
    component: str,
    data: dict[str, Any],
) -> dict[str, Any]:
    now = datetime.now(UTC)
    payload: dict[str, Any] = {
        "ts": now.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "ts_unix": time.time(),
        "level": level.upper(),
        "component": component,
        "category": category,
        "event": event,
        "pid": os.getpid(),
        "thread": threading.current_thread().name,
    }
    request_id = _request_id.get()
    if request_id:
        payload["request_id"] = request_id
    if data:
        payload["data"] = sanitize(data)
    return payload


def log_event(
    category: str,
    event: str,
    *,
    level: str = "INFO",
    component: str = "core",
    **data: Any,
) -> None:
    """Write one categorized record. Never raises. Avoid keyword collisions on ``data``
    by passing structured fields under ``extra=`` when a name would clash."""
    try:
        _ensure_configured()
        payload = _record(level, category, event, component=component, data=data)
        line = json.dumps(payload, ensure_ascii=False, default=str, separators=(",", ":"))
        logging.getLogger(LOGGER_NAME).log(
            getattr(logging, level.upper(), logging.INFO), line, extra={"ss_record": payload}
        )
    except Exception:  # noqa: BLE001, S110 — logging must never break the app
        pass


def _log_exit() -> None:
    """atexit handler. Suppressed while logging is being torn down (pytest, reload).

    At interpreter shutdown the FileHandler and the stderr handler may already be
    closed; writing then raises inside ``logging``'s own error handling and prints a
    confusing traceback. Detect that and skip the final record instead.
    """
    try:
        logger = logging.getLogger(LOGGER_NAME)
        # Any handler whose stream has already been closed is torn down; emitting into it
        # raises inside logging's own error path and prints a confusing traceback.
        for handler in logger.handlers:
            stream = getattr(handler, "stream", None)
            if stream is None or getattr(stream, "closed", False):
                return
        if not logger.handlers:
            return
        log_event("process", "exit", uptime_s=round(time.time() - _started_at, 1))
    except Exception:  # noqa: BLE001, S110
        pass


def log_exception(
    category: str,
    event: str,
    exc: BaseException,
    *,
    level: str = "ERROR",
    component: str = "core",
    **data: Any,
) -> None:
    """Log a traceback plus structured context."""
    log_event(
        category,
        event,
        level=level,
        component=component,
        error={
            "type": type(exc).__name__,
            "message": str(exc),
            "traceback": "".join(traceback.format_exception(exc)),
        },
        **data,
    )


def write_ui_records(records: list[dict[str, Any]]) -> int:
    """Persist frontend records forwarded by ``POST /debug/log``.

    The webview is the only place clicks/keys/SPA lifecycle are visible, so the
    UI posts its records here; the core appends them to the same file with
    ``component=ui`` so a single timeline is available.
    """
    written = 0
    for record in records[:500]:
        if not isinstance(record, dict):
            continue
        try:
            category = str(record.get("category") or "ui.event")[:120]
            event = str(record.get("event") or "event")[:120]
            level = str(record.get("level") or "INFO").upper()
            if level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
                level = "INFO"
            data: dict[str, Any] = {}
            raw_data = record.get("data")
            if raw_data is not None:
                data["payload"] = raw_data
            for key in ("url", "seq", "client_ts", "recovered", "user_agent", "viewport"):
                if key in record:
                    data[key] = record[key]
            log_event(category, event, level=level, component="ui", **data)
            written += 1
        except Exception:  # noqa: BLE001, S112 — one malformed record must not drop the batch
            continue
    return written


# --------------------------------------------------------------------------------
# Crash / exit hooks
# --------------------------------------------------------------------------------


def install_exception_hooks() -> None:
    """Capture uncaught exceptions, native crashes, and process exit."""
    global _hooks_installed, _faulthandler_file
    with _configure_lock:
        if _hooks_installed:
            return
        _hooks_installed = True

    try:
        import faulthandler

        directory = log_dir()
        directory.mkdir(parents=True, exist_ok=True)
        _faulthandler_file = open(directory / "faulthandler.log", "a", encoding="utf-8")
        faulthandler.enable(file=_faulthandler_file, all_threads=True)
    except Exception:  # noqa: BLE001, S110 — optional diagnostic
        pass

    previous_hook = sys.excepthook

    def _excepthook(exc_type, exc, tb):  # type: ignore[no-untyped-def]
        log_event(
            "process.uncaught",
            "exception",
            level="CRITICAL",
            error={
                "type": exc_type.__name__,
                "message": str(exc),
                "traceback": "".join(traceback.format_exception(exc_type, exc, tb)),
            },
        )
        previous_hook(exc_type, exc, tb)

    sys.excepthook = _excepthook

    if hasattr(threading, "excepthook"):
        previous_thread_hook = threading.excepthook

        def _thread_hook(args):  # type: ignore[no-untyped-def]
            log_event(
                "process.uncaught",
                "thread-exception",
                level="CRITICAL",
                thread_name=getattr(args.thread, "name", None),
                error={
                    "type": args.exc_type.__name__,
                    "message": str(args.exc_value),
                    "traceback": "".join(
                        traceback.format_exception(
                            args.exc_type, args.exc_value, args.exc_traceback
                        )
                    ),
                },
            )
            previous_thread_hook(args)

        threading.excepthook = _thread_hook  # type: ignore[assignment]

    global _started_at
    _started_at = time.time()
    import atexit

    atexit.register(_log_exit)


def process_info() -> dict[str, Any]:
    """Startup context written once so a log is self-describing."""
    return {
        "pid": os.getpid(),
        "python": sys.version.split()[0],
        "executable": sys.executable,
        "platform": platform.platform(),
        "cwd": os.getcwd(),
        "log_paths": log_paths(),
    }
