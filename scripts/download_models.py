#!/usr/bin/env python3
"""Download the GGUF weights into models/ (PLAN.md §4.1, TODO 0.1).

Both the 4B default and the 8B quality opt-in are declared here. The 8B is only
fetched with ``--include-8b`` so Phase 0 does not pay 5.5 GB for a model whose
ablation runs in Phase 4.5 (TODO 4.5).

Usage:
    uv run python scripts/download_models.py
    uv run python scripts/download_models.py --include-8b
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from core.config import LLM_MODELS, model_path, model_repo_id  # noqa: E402

# Approximate download sizes, for the progress message. Not used for any decision.
HINT_GB = {"Qwen3-4B": 3.0, "Qwen3-8B": 5.5}


def download(repo_id: str, filename: str, dest: Path) -> Path:
    from huggingface_hub import hf_hub_download

    dest.parent.mkdir(parents=True, exist_ok=True)

    if dest.exists():
        size_gb = dest.stat().st_size / 1e9
        print(f"  {dest.name}: present ({size_gb:.2f} GB)")
        return dest

    print(f"  {dest.name}: ~{HINT_GB.get(filename, 0):.1f} GB from {repo_id}")
    cached = hf_hub_download(
        repo_id=repo_id,
        filename=filename,
        local_dir=str(dest.parent),
        local_dir_use_symlinks=False,
    )
    moved = dest.parent / Path(cached).name
    if moved != dest:
        moved.replace(dest)
    print(f"  -> {dest} ({dest.stat().st_size / 1e9:.2f} GB)")
    return dest


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--include-8b",
        action="store_true",
        help="also fetch the 8B quality opt-in (needed before Phase 4.5, TODO 4.5)",
    )
    ap.add_argument(
        "--only",
        choices=["4b", "8b"],
        default=None,
        help="fetch exactly one size",
    )
    args = ap.parse_args()

    wanted: list[str] = []
    if args.only:
        wanted = [args.only]
    else:
        wanted = ["4b"]
        if args.include_8b:
            wanted.append("8b")

    for key in wanted:
        key = key.upper()
        model = LLM_MODELS[key]
        print(f"{model.label} ({key}) — {model.role}")
        download(model_repo_id(model), model.gguf_filename, model_path(key))

    print("")
    print("models/ now contains:")
    for p in sorted((REPO_ROOT / "models").glob("*.gguf")):
        print(f"  {p.name}  {p.stat().st_size / 1e9:.2f} GB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
