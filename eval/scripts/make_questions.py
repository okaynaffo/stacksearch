#!/usr/bin/env python3
"""Generate the Phase 2 eval question set (PLAN.md §14.1, TODO 2.3).

110 deterministic questions (`--seed`) from a corpus manifest, split by type:

- `exact` (40): unique ORDER-ID keys — BM25 should ace these; hybrid must not
  regress them.
- `meaning` (40): paraphrases sharing no content words with the topic header,
  e.g. "how much does keeping belongings in a rented unit cost" for the
  "storage pricing" topic — BM25 is near-random here; vectors must win.
- `time-filtered` (15): a meaning paraphrase plus a month phrase ("in March
  2025") constraining the file mtimes that `make_corpus.py` spreads across 2025.
- `unanswerable` (15): keys that do not exist plus off-corpus topics. Excluded
  from Recall/MRR/nDCG; the runner reports top-1 score separation instead (the
  signal Phase 4 refusal thresholds will use).

Each row: {id, type, query, expected_files, date_from?, date_to?}.
`expected_files` is the relevant set: the single keyed file for `exact`, all
same-topic (and in-range, for `time-filtered`) files otherwise.

Usage:
    uv run python eval/scripts/make_questions.py --corpus eval/datasets/phase2 \\
        --out eval/datasets/phase2_questions.jsonl --seed 7
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import random
import sys
from pathlib import Path

#: Meaning/time questions carry `expected_answers`: substrings of the canonical
#: per-topic fact (`make_corpus.TOPIC_FACTS`) that a grounded answer should
#: contain. The answer eval reports accuracy alongside citation validity.


def _topic_expected() -> dict[str, list[str]]:
    mod_path = Path(__file__).resolve().parent / "make_corpus.py"
    spec = importlib.util.spec_from_file_location("make_corpus", mod_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(mod_path.parent.parent.parent))
    spec.loader.exec_module(module)
    return {topic: answers for topic, (_, answers) in module.TOPIC_FACTS.items()}

#: Paraphrases deliberately share no content words with the topic name, so BM25
#: has nothing to grip and the meaning split discriminates vector search.
PARAPHRASES: dict[str, list[str]] = {
    "storage pricing": [
        "how much does keeping belongings in a rented unit cost per month",
        "what are the monthly fees for holding my boxes in a locker facility",
    ],
    "internship stipend": [
        "what monthly pay do summer trainees receive",
        "how much allowance is offered to seasonal junior staff",
    ],
    "flight itinerary": [
        "which airports and departure times are on my trip schedule",
        "what legs and layovers does my upcoming journey include",
    ],
    "recipe lentils": [
        "how to cook red legumes with spices for dinner",
        "what is the procedure for preparing a hearty pulse stew",
    ],
    "meeting notes": [
        "what was discussed and decided in the team sync",
        "summarize the action items from our group call",
    ],
    "book summary": [
        "what is the main argument of the novel I finished",
        "give me the gist of the story I just read",
    ],
    "lease renewal": [
        "when does my apartment contract extend and at what rent",
        "what are the terms for continuing my flat tenancy another year",
    ],
    "dentist appointment": [
        "when is my teeth cleaning and checkup visit",
        "what time is my oral hygiene clinic slot",
    ],
    "conference talk": [
        "what are the slides and schedule for my presentation",
        "when do I speak on stage and what is the session outline",
    ],
    "garden plan": [
        "which vegetables to plant in the backyard beds this spring",
        "what seeds and layout suit my home plot this season",
    ],
}

UNANSWERABLE = [
    "ORDER-ID-99999",
    "ORDER-ID-00000",
    "quantum entanglement research grant budget approval",
    "scuba diving certification renewal in the Maldives",
    "vintage motorcycle carburetor replacement part number",
    "ORDER-ID-42424242",
    "lunar eclipse photography exposure settings tunable",
    "sourdough starter hydration ratio for rye loaves",
    "antique violin appraisal certificate downtown",
    "ORDER-ID-7777777",
    "beekeeping hive inspection log for autumn mites",
    "hot air balloon pilot license medical requirements",
    "rare orchid greenhouse humidity controller manual",
    "ORDER-ID-1357924680",
    "medieval tapestry restoration invoice from Bruges",
]

TIME_MONTHS = [("March", "2025-03-01", "2025-03-31"), ("June", "2025-06-01", "2025-06-30")]


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate Phase 2 eval questions")
    parser.add_argument("--corpus", required=True, help="corpus dir with manifest.json")
    parser.add_argument("--out", required=True, help="output .jsonl path")
    parser.add_argument("--seed", type=int, default=7)
    args = parser.parse_args()

    manifest = json.loads((Path(args.corpus) / "manifest.json").read_text())
    rng = random.Random(args.seed)
    expected_by_topic = _topic_expected()
    by_topic: dict[str, list[dict]] = {}
    for entry in manifest:
        by_topic.setdefault(entry["topic"], []).append(entry)

    questions: list[dict] = []

    # -- exact: sample keyed files across topics --------------------------------
    sampled = rng.sample(manifest, min(40, len(manifest)))
    for i, entry in enumerate(sampled):
        query = entry["key"] if i % 2 == 0 else f'"{entry["key"]}"'
        questions.append(
            {"id": f"exact-{i:02d}", "type": "exact", "query": query,
             "expected_files": [entry["file"]], "expected_answers": [entry["key"]]}
        )

    # -- meaning: paraphrases, relevant = every same-topic file -----------------
    topics = sorted(by_topic)
    for i in range(40):
        topic = topics[i % len(topics)]
        paraphrase = PARAPHRASES[topic][(i // len(topics)) % 2]
        questions.append(
            {"id": f"meaning-{i:02d}", "type": "meaning", "query": paraphrase,
             "expected_files": [e["file"] for e in by_topic[topic]],
             "expected_answers": expected_by_topic[topic]}
        )

    # -- time-filtered: paraphrase + month phrase -------------------------------
    for i in range(15):
        topic = topics[i % len(topics)]
        paraphrase = PARAPHRASES[topic][i % 2]
        month_name, date_from, date_to = TIME_MONTHS[i % 2]
        expected = [
            e["file"]
            for e in by_topic[topic]
            if date_from <= e.get("mtime", "") <= date_to
        ]
        questions.append(
            {"id": f"time-{i:02d}", "type": "time-filtered",
             "query": f"{paraphrase} in {month_name} 2025",
             "expected_files": expected,
             "expected_answers": expected_by_topic[topic],
             "date_from": date_from, "date_to": date_to}
        )

    # -- unanswerable ------------------------------------------------------------
    for i, query in enumerate(UNANSWERABLE):
        questions.append(
            {"id": f"unanswerable-{i:02d}", "type": "unanswerable", "query": query,
             "expected_files": []}
        )

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as fh:
        for q in questions:
            fh.write(json.dumps(q) + "\n")
    n_time_empty = sum(
        1 for q in questions if q["type"] == "time-filtered" and not q["expected_files"]
    )
    print(f"wrote {len(questions)} questions to {out} (empty time-filtered: {n_time_empty})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
