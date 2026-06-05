#!/usr/bin/env python3
"""Summarize an rlm-sh run for RQ1 ("free decomposition") and basic cost.

Reads a run directory produced by host/loop_shell.sh:
  <run_dir>/iter_*.sh, final.sh   -> the exact bash the root model ran
  <run_dir>/work/answer.txt       -> final answer (if produced)
  <run_dir>/root.db               -> root conversation log (optional, via `llm`)

Counts, across all command blocks:
  - free-decomposition shell tools (grep/rg/awk/sed/split/... = no LLM call)
  - `llm` subcalls (the in-sandbox llm_query equivalent)
  - `rlm-sh` recursive calls

RQ1 is the ratio of free-decomposition commands to llm subcalls: a high ratio
means the model narrowed the context with cheap shell tools before spending tokens.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path


# Tools that decompose/inspect context without an LLM call ("free decomposition").
FREE_TOOLS = [
    "grep", "rg", "awk", "sed", "split", "csplit", "sort", "uniq", "cut",
    "head", "tail", "wc", "jq", "tr", "comm", "paste", "fold", "nl", "find", "ls",
]

# A command appears at start-of-line, after a shell separator / brace / backtick,
# after a loop/branch keyword (do/then/else), or as an xargs target. This catches
# `for f in ...; do llm ...; done` and `... | xargs -I{} llm ...`.
# Approximate: tools inside `sh -c '...'` or named at the very start of a quoted
# llm prompt may be mis-counted. Good enough for the coarse RQ1 ratio.
_CMD_PREFIX = r"(?:(?:^|[\n;|&`(){}])\s*|\b(?:do|then|else)\s+|\bxargs(?:\s+\S+)*?\s+)"


def _count_command(text: str, name: str) -> int:
    pattern = re.compile(_CMD_PREFIX + re.escape(name) + r"\b")
    return len(pattern.findall(text))


def collect_command_text(run_dir: Path) -> tuple[str, int]:
    """Return (concatenated command text, number of non-empty command turns)."""
    files = sorted(run_dir.glob("iter_*.sh"))
    final = run_dir / "final.sh"
    if final.is_file():
        files.append(final)
    chunks: list[str] = []
    turns = 0
    for f in files:
        body = f.read_text(encoding="utf-8", errors="replace").strip()
        if body:
            turns += 1
            chunks.append(body)
    return "\n".join(chunks), turns


def root_db_summary(run_dir: Path) -> dict | None:
    db = run_dir / "root.db"
    if not db.is_file():
        return None
    try:
        out = subprocess.run(
            ["llm", "logs", "list", "-d", str(db), "-n", "0", "--json"],
            text=True, capture_output=True, check=True, timeout=30,
        ).stdout
        rows = json.loads(out)
    except Exception:
        return None
    return {
        "responses": len(rows),
        "conversations": len({r.get("conversation_id") for r in rows}),
        "input_tokens": sum(int(r.get("input_tokens") or 0) for r in rows),
        "output_tokens": sum(int(r.get("output_tokens") or 0) for r in rows),
        "models": sorted({r.get("model") for r in rows if r.get("model")}),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", required=True, help="rlm-sh run directory")
    args = parser.parse_args()

    run_dir = Path(args.run_dir).resolve()
    if not run_dir.is_dir():
        raise SystemExit(f"metrics.py: run dir not found: {run_dir}")

    text, turns = collect_command_text(run_dir)

    free_counts = {t: _count_command(text, t) for t in FREE_TOOLS}
    free_counts = {t: n for t, n in free_counts.items() if n}
    free_total = sum(free_counts.values())
    llm_calls = _count_command(text, "llm")
    rlm_sh_calls = _count_command(text, "rlm-sh")

    answer = run_dir / "work" / "answer.txt"
    answer_bytes = answer.stat().st_size if answer.is_file() else 0

    ratio = round(free_total / llm_calls, 2) if llm_calls else None

    result = {
        "run_dir": str(run_dir),
        "turns": turns,
        "free_decomposition_total": free_total,
        "free_decomposition_by_tool": free_counts,
        "llm_subcalls": llm_calls,
        "rlm_sh_recursions": rlm_sh_calls,
        "free_to_llm_ratio": ratio,  # RQ1: higher = more cheap-tool narrowing before LLM
        "answer_produced": answer_bytes > 0,
        "answer_bytes": answer_bytes,
        "root_conversation": root_db_summary(run_dir),
        "note": (
            "Counts are approximate (command-position matching incl. for-do loops "
            "and xargs targets). root_conversation covers host root.db only; in-sandbox "
            "llm subcall tokens/cost live in the container's /work/.llm DB."
        ),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
