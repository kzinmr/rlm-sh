#!/usr/bin/env python3
"""Generate and inspect long-context MapReduce observation tasks for rlm-sh."""

from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import random
import re
import string
import subprocess
from typing import Any


THEMES = [
    "accessibility",
    "billing",
    "compliance",
    "latency",
    "localization",
    "onboarding",
    "reliability",
    "reporting",
]

FILLER_WORDS = [
    "archive",
    "bridge",
    "catalog",
    "delta",
    "ember",
    "framework",
    "garden",
    "harbor",
    "index",
    "junction",
    "kernel",
    "ledger",
    "matrix",
    "notebook",
    "orbit",
    "packet",
    "quartz",
    "signal",
    "timeline",
    "vector",
]

COMMAND_RE = re.compile(r"(?:(?:^|[\n;|&`(){}])\s*|\b(?:do|then|else)\s+)(\w[\w.-]*)\b")


def random_sentence(rng: random.Random, words: int) -> str:
    tokens = [rng.choice(FILLER_WORDS) for _ in range(words)]
    tokens[0] = tokens[0].capitalize()
    return " ".join(tokens) + "."


def generate(args: argparse.Namespace) -> int:
    rng = random.Random(args.seed)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    docs_dir = out_dir / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)

    theme_counts: Counter[str] = Counter()
    secret_doc = rng.randrange(args.docs)
    secret_code = "".join(rng.choice(string.digits) for _ in range(8))
    assignments: dict[str, list[str]] = {}

    for i in range(args.docs):
        doc_id = f"doc_{i:04d}"
        theme_count = rng.randint(1, min(3, len(THEMES)))
        doc_themes = sorted(rng.sample(THEMES, theme_count))
        for theme in doc_themes:
            theme_counts[theme] += 1
        assignments[f"{doc_id}.md"] = doc_themes
        lines = [
            f"# {doc_id}",
            "",
            f"DOCUMENT_ID: {doc_id}",
            "THEMES: " + ", ".join(doc_themes),
            "",
        ]
        for theme in doc_themes:
            lines.append(
                f"Finding: THEME:{theme} is mentioned in this document with supporting notes."
            )
            lines.append(random_sentence(rng, args.words_per_theme))
        if i == secret_doc:
            lines.append(f"SECRET_CODE: {secret_code}")
            lines.append("This is the only document containing the secret code marker.")
        for _ in range(args.filler_paragraphs):
            lines.append(random_sentence(rng, args.filler_words))
        (docs_dir / f"{doc_id}.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    expected = {
        "context_dir": str(out_dir),
        "docs_dir": str(docs_dir),
        "docs": args.docs,
        "themes": dict(sorted(theme_counts.items())),
        "secret_code": secret_code,
        "secret_document": f"doc_{secret_doc:04d}.md",
        "assignments": assignments,
    }
    query = (
        "Across every markdown file in /context/docs, count how many documents mention "
        "each THEME:<name> marker, identify the SECRET_CODE and the document containing "
        "it, and return compact JSON with keys themes, secret_code, secret_document."
    )
    (out_dir / "query.txt").write_text(query + "\n", encoding="utf-8")
    (out_dir / "expected.json").write_text(
        json.dumps(expected, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "context_dir": str(out_dir),
                "query": query,
                "expected": str(out_dir / "expected.json"),
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def command_text(run_dir: Path) -> str:
    files = sorted(run_dir.glob("iter_*.sh"))
    final = run_dir / "final.sh"
    if final.is_file():
        files.append(final)
    return "\n".join(
        f.read_text(encoding="utf-8", errors="replace") for f in files if f.is_file()
    )


def observe(args: argparse.Namespace) -> int:
    run_dir = Path(args.run_dir).resolve()
    if not run_dir.is_dir():
        raise SystemExit(f"mapreduce.py: run dir not found: {run_dir}")
    text = command_text(run_dir)
    commands = Counter(match.group(1) for match in COMMAND_RE.finditer(text))
    work_dir = run_dir / "work"
    buffer_files = sorted((work_dir / "buffers").glob("*")) if (work_dir / "buffers").is_dir() else []
    chunk_files = sorted((work_dir / "chunks").glob("*")) if (work_dir / "chunks").is_dir() else []
    spawn_events = run_dir / "orchestrator_events.jsonl"
    spawn_count = 0
    if spawn_events.is_file():
        with spawn_events.open(encoding="utf-8") as f:
            spawn_count = sum(1 for line in f if '"event": "spawn_finished"' in line)
    result = {
        "run_dir": str(run_dir),
        "answer_produced": (work_dir / "answer.txt").is_file(),
        "split_commands": commands.get("split", 0) + commands.get("csplit", 0),
        "free_search_commands": sum(commands.get(cmd, 0) for cmd in ("rg", "grep", "awk", "sed")),
        "llm_commands": commands.get("llm", 0),
        "rlm_sh_commands": commands.get("rlm-sh", 0),
        "finished_spawns": spawn_count,
        "chunk_files": len(chunk_files),
        "buffer_files": len(buffer_files),
        "mapreduce_signal": (
            len(chunk_files) > 1
            or len(buffer_files) > 1
            or commands.get("split", 0) > 0
            or spawn_count > 0
        ),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def score(args: argparse.Namespace) -> int:
    run_dir = Path(args.run_dir).resolve()
    expected = json.loads(Path(args.expected).read_text(encoding="utf-8"))
    answer_path = run_dir / "work" / "answer.txt"
    if not answer_path.is_file():
        raise SystemExit(f"mapreduce.py: answer not found: {answer_path}")
    answer = answer_path.read_text(encoding="utf-8", errors="replace")
    expected_themes = expected["themes"]
    theme_hits = {
        theme: bool(re.search(rf"\b{re.escape(theme)}\b.*\b{count}\b", answer, re.I | re.S))
        for theme, count in expected_themes.items()
    }
    result = {
        "run_dir": str(run_dir),
        "secret_code_found": expected["secret_code"] in answer,
        "secret_document_found": expected["secret_document"] in answer,
        "theme_counts_found": theme_hits,
        "theme_count_accuracy": round(sum(theme_hits.values()) / len(theme_hits), 3),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def run(args: argparse.Namespace) -> int:
    context_dir = Path(args.context_dir).resolve()
    query = args.query or (context_dir / "query.txt").read_text(encoding="utf-8").strip()
    cmd = [
        str(Path(__file__).resolve().parents[1] / "host" / "loop_shell.sh"),
        "--query",
        query,
        "--context-dir",
        str(context_dir),
    ]
    if args.run_dir:
        cmd.extend(["--run-dir", args.run_dir])
    if args.root_model:
        cmd.extend(["--root-model", args.root_model])
    if args.backend:
        cmd.extend(["--backend", args.backend])
    if args.build:
        cmd.append("--build")
    completed = subprocess.run(cmd, text=True)
    return completed.returncode


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    gen = subparsers.add_parser("generate")
    gen.add_argument("--out", required=True)
    gen.add_argument("--docs", type=int, default=48)
    gen.add_argument("--seed", type=int, default=7)
    gen.add_argument("--words-per-theme", type=int, default=32)
    gen.add_argument("--filler-paragraphs", type=int, default=8)
    gen.add_argument("--filler-words", type=int, default=48)
    gen.set_defaults(func=generate)

    obs = subparsers.add_parser("observe")
    obs.add_argument("--run-dir", required=True)
    obs.set_defaults(func=observe)

    score_parser = subparsers.add_parser("score")
    score_parser.add_argument("--run-dir", required=True)
    score_parser.add_argument("--expected", required=True)
    score_parser.set_defaults(func=score)

    run_parser = subparsers.add_parser("run")
    run_parser.add_argument("--context-dir", required=True)
    run_parser.add_argument("--query", default=None)
    run_parser.add_argument("--run-dir", default=None)
    run_parser.add_argument("--root-model", default=None)
    run_parser.add_argument("--backend", default=None)
    run_parser.add_argument("--build", action="store_true")
    run_parser.set_defaults(func=run)

    return parser.parse_args()


def main() -> int:
    args = parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
