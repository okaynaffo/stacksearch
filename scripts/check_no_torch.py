#!/usr/bin/env python3
"""Assert torch is absent from the venv and the lockfile (PLAN.md §4.1, §16).

§4.1's first non-negotiable is that PyTorch never enters this project, and §16 gives the
reason as a memory decision rather than a preference: torch's ~2.5 GB RSS cannot coexist
with the answer LLM inside the §2.1 budget. A single transitive dependency pulling it in
would quietly cost a quarter of the answering budget, so this is enforced rather than
documented.

Checks three independent surfaces, because each can be broken independently:
  1. the installed environment (`importlib.metadata`),
  2. the resolved lockfile (`uv.lock`), which catches it before it is ever installed,
  3. the running process, which catches a stray system-level torch on sys.path.

Usage:
    uv run python scripts/check_no_torch.py
    uv run python scripts/check_no_torch.py --json
"""

from __future__ import annotations

import argparse
import json
from importlib.metadata import distributions
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

#: torch ships under several distribution names; all of them drag in the same runtime.
TORCH_DISTRIBUTIONS = {
    "torch",
    "torchvision",
    "torchaudio",
    "pytorch-triton",
}


def installed_torch() -> list[str]:
    """torch distributions present in the active environment."""
    found = []
    for dist in distributions():
        name = (dist.metadata["Name"] or "").lower()
        if name in TORCH_DISTRIBUTIONS:
            found.append(f"{dist.metadata['Name']}=={dist.version}")
    return sorted(found)


def locked_torch() -> list[str]:
    """torch packages pinned in uv.lock."""
    lock = REPO_ROOT / "uv.lock"
    if not lock.exists():
        return []
    import tomllib

    data = tomllib.loads(lock.read_text())
    found = []
    for package in data.get("package", []):
        name = (package.get("name") or "").lower()
        if name in TORCH_DISTRIBUTIONS:
            found.append(f"{name}=={package.get('version', '?')}")
    return sorted(found)


def importable_torch() -> str | None:
    """torch importable from the current interpreter, or None."""
    import importlib.util

    return importlib.util.find_spec("torch")


def check() -> dict:
    return {
        "installed": installed_torch(),
        "locked": locked_torch(),
        "importable": importable_torch() is not None,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args()

    report = check()
    failures = []

    if report["installed"]:
        failures.append(f"installed in the venv: {report['installed']}")
    if report["locked"]:
        failures.append(f"pinned in uv.lock: {report['locked']}")
    if report["importable"]:
        failures.append("importable from the active interpreter")

    if args.json:
        print(json.dumps({**report, "ok": not failures, "failures": failures}, indent=2))
    else:
        print("torch guard (PLAN.md §4.1 / §16)")
        print(f"  installed : {report['installed'] or 'none'}")
        print(f"  locked    : {report['locked'] or 'none'}")
        print(f"  importable: {report['importable']}")
        if failures:
            print("\nFAIL — torch would cost ~2.5 GB RSS that the §2.1 budget cannot spare:")
            for f in failures:
                print(f"  - {f}")
            print("\nRemove the offending dependency. Embeddings and reranking must stay on")
            print("fastembed's ONNX Runtime path.")
        else:
            print("\nPASS — no torch anywhere. ONNX-only stack intact.")

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
