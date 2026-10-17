#!/usr/bin/env bash
# Fetch and verify the pinned llama.cpp build (PLAN.md §4.1, TODO 0.1).
#
# Uses the official prebuilt macOS arm64 release asset rather than compiling from
# source: the archive ships with Metal dylibs already built, it is 12 MB instead of a
# multi-minute compile, and the tag pin in scripts/llama_server.pin keeps it
# reproducible. Building from source remains possible by pointing
# core.config.llama_server_path at a locally built binary.
#
# Usage: scripts/fetch_llama_server.sh [--force]

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PIN_FILE="$REPO_ROOT/scripts/llama_server.pin"
VENDOR_DIR="$REPO_ROOT/vendor/llama-server"

FORCE=0
[[ "${1:-}" == "--force" ]] && FORCE=1

# --- pin ------------------------------------------------------------------------
[[ -f "$PIN_FILE" ]] || { echo "missing pin file: $PIN_FILE" >&2; exit 1; }
# shellcheck disable=SC1090
source "$PIN_FILE"

[[ "$(uname -s)" == "Darwin" ]] || { echo "this pin targets macOS arm64 only" >&2; exit 1; }
[[ "$(uname -m)" == "arm64" ]] || { echo "this pin targets macOS arm64 only" >&2; exit 1; }

EXTRACT_DIR="$VENDOR_DIR/llama-$LLAMA_TAG"
SERVER_BIN="$EXTRACT_DIR/llama-server"

# --- already present? ----------------------------------------------------------
if [[ -x "$SERVER_BIN" && $FORCE -eq 0 ]]; then
  echo "llama-server already present at $SERVER_BIN"
  "$SERVER_BIN" --version 2>&1 | head -2
  exit 0
fi

# --- download ------------------------------------------------------------------
URL="https://github.com/ggml-org/llama.cpp/releases/download/$LLAMA_TAG/$LLAMA_ASSET"
TMP="$(mktemp -t llama-server-XXXXXX).tar.gz"
trap 'rm -f "$TMP"' EXIT

echo "downloading $URL"
curl -fsSL --progress-bar -o "$TMP" "$URL"

# --- verify --------------------------------------------------------------------
ACTUAL="$(shasum -a 256 "$TMP" | awk '{print $1}')"
if [[ "$ACTUAL" != "$LLAMA_SHA256" ]]; then
  echo "sha256 mismatch for $LLAMA_ASSET" >&2
  echo "  expected $LLAMA_SHA256" >&2
  echo "  actual   $ACTUAL" >&2
  echo "refusing to install. If upstream legitimately re-cut the asset, update" >&2
  echo "LLAMA_SHA256 in scripts/llama_server.pin and re-run the §2.1 benchmark." >&2
  exit 1
fi
echo "sha256 ok: $ACTUAL"

# --- extract -------------------------------------------------------------------
# The archive unpacks into a versioned directory and the dylibs must stay
# co-located with the binary (they resolve via @loader_path), so the whole
# directory is the install unit.
rm -rf "$EXTRACT_DIR"
mkdir -p "$VENDOR_DIR"
tar -xzf "$TMP" -C "$VENDOR_DIR"
[[ -x "$SERVER_BIN" ]] || { echo "expected $SERVER_BIN after extraction" >&2; exit 1; }

# Unsigned third-party binary: strip quarantine so Gatekeeper does not prompt on
# first launch in the packaged app.
xattr -dr com.apple.quarantine "$VENDOR_DIR" 2>/dev/null || true

echo "installed to $EXTRACT_DIR"
"$SERVER_BIN" --version 2>&1 | head -2