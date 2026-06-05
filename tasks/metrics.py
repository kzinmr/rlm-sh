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
from typing import Any


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


def llm_db_summary(db: Path) -> dict | None:
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


def root_db_summary(run_dir: Path) -> dict | None:
    return llm_db_summary(run_dir / "root.db")


def sandbox_llm_summary(run_dir: Path) -> dict | None:
    candidates = [
        run_dir / "work" / ".llm" / "logs.db",
        run_dir / "work" / ".llm" / "logs.db-shm",
    ]
    for candidate in candidates:
        if candidate.name.endswith(".db") and candidate.is_file():
            return llm_db_summary(candidate)
    return None


def spawn_events(run_dir: Path) -> list[dict[str, Any]]:
    path = run_dir / "orchestrator_events.jsonl"
    if not path.is_file():
        return []
    events: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                events.append({"event": "invalid_json", "raw": line})
    return events


def child_run_dirs(run_dir: Path) -> list[Path]:
    children_root = run_dir / "children"
    if not children_root.is_dir():
        return []
    discovered: list[Path] = []
    for path in sorted(child for child in children_root.iterdir() if child.is_dir()):
        discovered.append(path)
        discovered.extend(child_run_dirs(path))
    return discovered


def context_validation(run_dir: Path) -> dict[str, Any] | None:
    end = run_dir / "context_hash.end.json"
    start = run_dir / "context_hash.start.json"
    if end.is_file():
        try:
            return json.loads(end.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {"ok": False, "error": f"invalid JSON: {end}"}
    if start.is_file():
        return {"ok": None, "note": "start hash exists; end validation was not recorded"}
    return None


def summarize_run(run_dir: Path) -> dict[str, Any]:
    text, turns = collect_command_text(run_dir)
    free_counts = {t: _count_command(text, t) for t in FREE_TOOLS}
    free_counts = {t: n for t, n in free_counts.items() if n}
    free_total = sum(free_counts.values())
    llm_calls = _count_command(text, "llm")
    rlm_sh_calls = _count_command(text, "rlm-sh")
    answer = run_dir / "work" / "answer.txt"
    answer_bytes = answer.stat().st_size if answer.is_file() else 0
    return {
        "run_dir": str(run_dir),
        "turns": turns,
        "free_decomposition_total": free_total,
        "free_decomposition_by_tool": free_counts,
        "llm_subcalls": llm_calls,
        "rlm_sh_recursions": rlm_sh_calls,
        "free_to_llm_ratio": round(free_total / llm_calls, 2) if llm_calls else None,
        "answer_produced": answer_bytes > 0,
        "answer_bytes": answer_bytes,
        "root_conversation": root_db_summary(run_dir),
        "sandbox_conversation": sandbox_llm_summary(run_dir),
        "context_validation": context_validation(run_dir),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", required=True, help="rlm-sh run directory")
    parser.add_argument(
        "--no-children",
        action="store_true",
        help="only summarize the root run directory",
    )
    args = parser.parse_args()

    run_dir = Path(args.run_dir).resolve()
    if not run_dir.is_dir():
        raise SystemExit(f"metrics.py: run dir not found: {run_dir}")

    run_dirs = [run_dir]
    if not args.no_children:
        run_dirs.extend(child_run_dirs(run_dir))
    runs = [summarize_run(path) for path in run_dirs]

    aggregate_free = sum(int(run["free_decomposition_total"]) for run in runs)
    aggregate_llm = sum(int(run["llm_subcalls"]) for run in runs)
    aggregate_rlm = sum(int(run["rlm_sh_recursions"]) for run in runs)
    events = spawn_events(run_dir)

    result = {
        "run_dir": str(run_dir),
        "runs": runs,
        "aggregate": {
            "run_count": len(runs),
            "free_decomposition_total": aggregate_free,
            "llm_subcalls": aggregate_llm,
            "rlm_sh_recursions": aggregate_rlm,
            "free_to_llm_ratio": (
                round(aggregate_free / aggregate_llm, 2) if aggregate_llm else None
            ),
            "answer_produced": runs[0]["answer_produced"],
            "answer_bytes": runs[0]["answer_bytes"],
        },
        "spawn_events": {
            "total": len(events),
            "by_event": {
                event: sum(1 for item in events if item.get("event") == event)
                for event in sorted({str(item.get("event")) for item in events})
            },
            "correlations": [
                {
                    "request_id": item.get("request_id"),
                    "parent_call_id": item.get("parent_call_id"),
                    "parent_sandbox_id": item.get("parent_sandbox_id"),
                    "child_run_dir": item.get("child_run_dir"),
                    "event": item.get("event"),
                }
                for item in events
                if item.get("request_id")
            ],
        },
        "note": (
            "Counts are approximate (command-position matching incl. for-do loops "
            "and xargs targets). Root and child host conversations are read from "
            "each run_dir/root.db when the host llm CLI can inspect them. Sandbox "
            "subcall logs are included only when /work/.llm/logs.db is present."
        ),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
