#!/usr/bin/env bash
# Continuous integration (PLAN.md §4, §16).
#
# Kept as a script rather than only a GitHub Actions workflow so the same gates run
# locally before a commit. The torch guard is the one that matters most: torch's ~2.5 GB
# RSS cannot coexist with the answer LLM inside the §2.1 budget, and a transitive
# dependency pulling it in would be invisible until someone measured peak memory.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

export PATH="$HOME/.cargo/bin:$PATH"
export FASTEMBED_CACHE_PATH="${FASTEMBED_CACHE_PATH:-$REPO_ROOT/.cache/fastembed}"

step() { printf '\n\033[1m== %s\033[0m\n' "$1"; }

step "ruff"
uv run ruff check .

step "pytest"
# The default set is fast and model-free; the slow, model-dependent suites are opt-in.
uv run pytest -q

step "torch guard (PLAN.md §4.1 / §16)"
uv run python scripts/check_no_torch.py

step "schema guard: no hardcoded vector width (PLAN.md §5)"
# Enforced by tests/unit/test_schema.py::test_no_literal_float_dim_in_source_tree, which
# walks the AST. A grep here would flag the docstrings and comments that *describe* the
# rule, as well as the correct f-string in chunks_vec_ddl — all three of which are fine.
echo "covered by the AST-based pytest guard (see tests/unit/test_schema.py)"

if [ -f app/src-tauri/Cargo.toml ]; then
  step "cargo test"
  (cd app/src-tauri && cargo test --quiet)

  step "clippy"
  (cd app/src-tauri && cargo clippy --all-targets --quiet)

  step "ui build (PLAN.md §4: one static build, two hosts)"
  (cd app/ui && npm run build)
fi

printf '\n\033[1mall gates passed\033[0m\n'
