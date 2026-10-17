#!/usr/bin/env python3
"""Synthetic personal corpus generator (PLAN.md §14.1, TODO 1.3/2.3).

Deterministic (--seed) so eval runs are reproducible. Each file embeds a unique
retrievable key (ORDER-ID-XXXXX) for exact-lookup questions. Reused by the Phase 2
100-question harness: manifest records each file's key, topic, and mtime.

File mtimes are spread deterministically across 2025 so time-filtered questions
(PLAN.md §14.1, "emails from last week about the offer") have a real date
dimension to filter on. A `--pdfs` subset is written as real PDFs (via PyMuPDF)
so the harness covers the PDF extraction path.

Usage:
    uv run python eval/scripts/make_corpus.py --out eval/datasets/phase1 --files 5000 --seed 0
    uv run python eval/scripts/make_corpus.py --out eval/datasets/phase2 --files 500 --seed 7
"""

from __future__ import annotations

import argparse
import json
import os
import random
from datetime import datetime, timedelta
from pathlib import Path

TOPICS = [
    "storage pricing",
    "internship stipend",
    "flight itinerary",
    "recipe lentils",
    "meeting notes",
    "book summary",
    "lease renewal",
    "dentist appointment",
    "conference talk",
    "garden plan",
]

WORDS = (
    "alpha bravo charlie delta echo foxtrot golf hotel india juliet kilo lima mike "
    "november oscar papa quebec romeo sierra tango uniform victor whiskey xray yankee "
    "zulu river mountain ledger quietly rapid gentle harvest timber cloud field stone"
).split()

#: One canonical fact per topic, in plain language. Every file of a topic states
#: the same fact in its FIRST sentence (before the gibberish), so any retrieved
#: same-topic chunk supports the meaning/time questions about it. This is what
#: makes the Phase 4 citation-validity gate answerable: before this, meaning
#: questions asked for facts no file contained, and correct refusals scored 0.
#: EXPECTED maps topic -> acceptable answer substrings (case-insensitive).
TOPIC_FACTS: dict[str, tuple[str, list[str]]] = {
    "storage pricing": (
        "Storage pricing is $145 per month for a 10x10 unit.",
        ["$145"],
    ),
    "internship stipend": (
        "The internship stipend is 25,000 INR per month, paid on the 5th.",
        ["25,000"],
    ),
    "flight itinerary": (
        "The flight departs from JFK at 8:40 AM with a layover in Chicago.",
        ["JFK", "8:40"],
    ),
    "recipe lentils": (
        "To cook red lentils for dinner: rinse 2 cups of red lentils, boil them "
        "with spices, then simmer for 35 minutes.",
        ["35 minutes", "35 min", "simmer"],
    ),
    "meeting notes": (
        "The team decided to launch on Friday, and Priya owns the rollout checklist.",
        ["Friday", "Priya"],
    ),
    "book summary": (
        "The novel's main argument is that harbor cities grow rich on repair docks, "
        "not trade.",
        ["repair docks"],
    ),
    "lease renewal": (
        "The lease renews on April 1 at $1,900 per month.",
        ["$1,900", "$1900"],
    ),
    "dentist appointment": (
        "The teeth cleaning and checkup visit is on Tuesday, June 3 at 10:30 AM.",
        ["June 3"],
    ),
    "conference talk": (
        "The talk is on the main stage at 2:00 PM with 12 slides.",
        ["2:00 PM", "2:00"],
    ),
    "garden plan": (
        "Plant tomatoes and basil in the backyard beds this spring.",
        ["tomatoes"],
    ),
}


def _body(rng: random.Random, key: str, topic: str) -> str:
    n_paras = rng.randint(3, 6)
    fact = TOPIC_FACTS[topic][0]
    paras = [f"# Notes on {topic}\n", fact, f"Reference key {key} for exact lookup."]
    for _ in range(n_paras):
        paras.append(" ".join(rng.choice(WORDS) for _ in range(rng.randint(60, 120))))
    paras.append(f"The document about {topic} concludes with key {key}.")
    return "\n\n".join(paras)


def _write_pdf(path: Path, body: str) -> None:
    """Write *body* as a single real PDF (Phase 2: cover the PDF path)."""
    import pymupdf

    doc = pymupdf.open()
    page = doc.new_page()
    rect = pymupdf.Rect(72, 72, 540, 770)
    page.insert_textbox(rect, body, fontsize=11)
    doc.save(path)
    doc.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate a synthetic search corpus")
    parser.add_argument("--out", required=True)
    parser.add_argument("--files", type=int, default=5000)
    parser.add_argument("--pdfs", type=int, default=0, help="how many files are real PDFs")
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    rng = random.Random(args.seed)
    base_day = datetime(2025, 1, 1)
    manifest = []
    for i in range(args.files):
        topic = TOPICS[i % len(TOPICS)]
        key = f"ORDER-ID-{10000 + i}"
        is_pdf = i < args.pdfs
        if is_pdf:
            name = f"note_{i:05d}_{topic.replace(' ', '_')}.pdf"
        else:
            suffix = [".md", ".md", ".md", ".txt", ".txt", ".md", ".txt"][i % 7]
            name = f"note_{i:05d}_{topic.replace(' ', '_')}{suffix}"
        body = _body(rng, key, topic)
        target = out / name
        if is_pdf:
            _write_pdf(target, body)
        else:
            target.write_text(body, encoding="utf-8")
        # Deterministic mtime spread across 2025 for time-filtered questions.
        mtime = base_day + timedelta(days=(i * 37) % 300)
        ts = mtime.timestamp()
        os.utime(target, (ts, ts))
        manifest.append(
            {"file": name, "key": key, "topic": topic, "mtime": mtime.date().isoformat()}
        )
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"wrote {args.files} files ({args.pdfs} PDFs) to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
