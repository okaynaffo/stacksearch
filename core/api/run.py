"""Development entrypoint: serve the core service on loopback (PLAN.md §9, §10).

    uv run core.api.run

Binds 127.0.0.1 only. The per-launch token is generated on start and printed once, so a
browser session or curl can authenticate; §9's intent is that the token is per-launch
and never written to disk.
"""

from __future__ import annotations

import os

import uvicorn

from core.config import get_settings


def main() -> int:
    settings = get_settings()
    settings.ensure_dirs()

    # The app module builds its own Settings when uvicorn imports it, so a token that was
    # generated here would not be the one the app validates — the printed token would be
    # rejected. Publishing it into the environment makes the two agree, and gives child
    # processes (the llama-server supervisor) the same credential.
    os.environ["STACKSEARCH_TOKEN"] = settings.api_token

    # Flushed explicitly: stdout is block-buffered when redirected, and uvicorn's logging
    # goes to stderr unbuffered, so without this the token appears to be missing.
    print(f"core service listening on http://{settings.api_host}:{settings.api_port}")
    print(f"  index : {settings.index_db_path}")
    print(f"  token : {settings.api_token}")
    print("")
    print("Pass it to the UI as ?token=... or an X-Local-Token header. It is regenerated")
    print("on every launch (PLAN.md §9).", flush=True)

    uvicorn.run(
        "core.api.app:app",
        host=settings.api_host,
        port=settings.api_port,
        log_level="info",
        # Off by default because the UI polls /status; on when diagnosing whether the
        # webview actually reached the service (PLAN.md §13.1).
        access_log=os.environ.get("STACKSEARCH_ACCESS_LOG", "").lower() in ("1", "true", "yes"),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
