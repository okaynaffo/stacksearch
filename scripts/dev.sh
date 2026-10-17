#!/usr/bin/env bash
# Run the app in development (PLAN.md §4 "UI host" row, §12).
#
# Two hosts, one build:
#
#   scripts/dev.sh browser   core service + Vite dev server at 127.0.0.1:8000
#   scripts/dev.sh app       core service, spawned and supervised by the Tauri shell
#
# The UI is a single static build, so `browser` and `app` render the same components.
# Phases 1-5 are developed in the browser because it has a reload cycle instead of a
# webview rebuild; `app` is here from Phase 0 so the desktop integration stays exercised
# (PLAN.md §13.1).

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MODE="${1:-browser}"

# cargo is installed but not on the default PATH for GUI shells on this machine.
export PATH="$HOME/.cargo/bin:$PATH"

# Keep the fastembed/HF caches inside the repo so nothing lands in a temp directory that
# `fastembed` would otherwise treat as ephemeral (PLAN.md §4.1).
export FASTEMBED_CACHE_PATH="${FASTEMBED_CACHE_PATH:-$REPO_ROOT/.cache/fastembed}"

die() { echo "error: $*" >&2; exit 1; }

check_assets() {
  [[ -f "$REPO_ROOT/vendor/llama-server/llama-b11462/llama-server" ]] ||
    die "llama-server missing; run scripts/fetch_llama_server.sh"
}

case "$MODE" in
  browser)
    check_assets
    echo "core service on http://127.0.0.1:8000 — the token it prints is the UI credential"
    echo "press ctrl+c to stop both"
    trap 'kill 0' EXIT INT TERM

    (cd "$REPO_ROOT" && uv run python -m core.api.run) &

    # Wait for the service before starting Vite, so the first proxy hit is not a 502.
    for _ in $(seq 1 120); do
      if curl -s -o /dev/null "http://127.0.0.1:8000/status"; then break; fi
      sleep 0.25
    done

    # Vite proxies /status and /events to the service so the browser sees one origin,
    # which keeps the ?token= SSE fallback working exactly as it does in the webview.
    (cd "$REPO_ROOT/app/ui" && npm run dev)
    ;;

  app)
    check_assets
    echo "building the UI, then launching the Tauri shell"
    (cd "$REPO_ROOT/app/ui" && npm run build)
    # The shell finds the repository for `uv run --project` via this variable rather than
    # inferring it from the executable path.
    export STACKSEARCH_REPO_ROOT="$REPO_ROOT"
    cd "$REPO_ROOT/app/src-tauri"
    exec cargo run
    ;;

  test)
    echo "== python tests"
    (cd "$REPO_ROOT" && uv run pytest -q)
    echo "== ruff"
    (cd "$REPO_ROOT" && uv run ruff check .)
    echo "== torch guard (PLAN.md §4.1 / §16)"
    (cd "$REPO_ROOT" && uv run python scripts/check_no_torch.py)
    if command -v cargo >/dev/null 2>&1; then
      echo "== rust tests"
      (cd "$REPO_ROOT/app/src-tauri" && cargo test --quiet)
      echo "== clippy"
      (cd "$REPO_ROOT/app/src-tauri" && cargo clippy --all-targets --quiet)
    fi
    echo "all green"
    ;;

  *)
    die "usage: scripts/dev.sh [browser|app|test]"
    ;;
esac