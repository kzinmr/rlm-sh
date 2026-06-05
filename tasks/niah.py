#!/usr/bin/env python3
"""Generate a simple Needles-in-a-Haystack context for rlm-sh."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import random


WORDS = [
    "archive",
    "bridge",
    "carbon",
    "delta",
    "ember",
    "field",
    "glacier",
    "harbor",
    "isotope",
    "junction",
    "kernel",
    "ledger",
    "matrix",
    "nebula",
    "orbit",
    "packet",
    "quartz",
    "signal",
    "tundra",
    "vector",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True, help="directory to write context.txt")
    parser.add_argument("--answer-out", default=None, help="optional expected answer file")
    parser.add_argument("--lines", type=int, default=10000)
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--needle-label", default="magic number")
    return parser.parse_args()


def filler_line(rng: random.Random, index: int) -> str:
    words = " ".join(rng.choice(WORDS) for _ in range(12))
    return f"{index:08d} {words}\n"


def main() -> int:
    args = parse_args()
    if args.lines < 1:
        raise SystemExit("niah.py: --lines must be >= 1")

    rng = random.Random(args.seed)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    context_path = out_dir / "context.txt"

    answer = str(rng.randint(1000000, 9999999))
    needle_index = rng.randrange(args.lines)
    with context_path.open("w", encoding="utf-8") as f:
        for i in range(args.lines):
            if i == needle_index:
                f.write(f"{i:08d} The {args.needle_label} is {answer}.\n")
            else:
                f.write(filler_line(rng, i))

    query = (
        "Find the magic number hidden in /context/context.txt. "
        "Return only the number."
    )
    result = {
        "context_dir": str(out_dir),
        "context_file": str(context_path),
        "answer": answer,
        "needle_line": needle_index + 1,
        "query": query,
    }
    if args.answer_out:
        Path(args.answer_out).write_text(answer + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
