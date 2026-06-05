#!/usr/bin/env python3
"""Small helpers for the pure-shell rlm-sh loop."""

from __future__ import annotations

import argparse
import json
import re
import sys


FENCE_RE = re.compile(r"```(?P<lang>[^\n`]*)\n(?P<body>.*?)```", re.DOTALL)
SHELL_LANGS = {"", "bash", "sh", "shell", "zsh"}


def cmd_extract_bash(_args: argparse.Namespace) -> int:
    text = sys.stdin.read()
    for match in FENCE_RE.finditer(text):
        lang = match.group("lang").strip().lower()
        if lang in SHELL_LANGS:
            body = match.group("body").strip()
            if body:
                sys.stdout.write(body)
                if not body.endswith("\n"):
                    sys.stdout.write("\n")
                return 0
    return 1


def cmd_truncate(args: argparse.Namespace) -> int:
    data = sys.stdin.buffer.read()
    if len(data) <= args.max_chars:
        sys.stdout.write(data.decode("utf-8", errors="replace"))
        return 0

    head_chars = min(args.head_chars, args.max_chars)
    tail_chars = min(args.tail_chars, max(args.max_chars - head_chars, 0))
    if head_chars + tail_chars > args.max_chars:
        tail_chars = args.max_chars - head_chars

    text = data.decode("utf-8", errors="replace")
    line_count = text.count("\n")
    head = text[:head_chars]
    tail = text[-tail_chars:] if tail_chars else ""
    omitted = len(data) - len(head.encode("utf-8")) - len(tail.encode("utf-8"))
    marker = (
        f"\n\n[rlm-sh: output truncated; original={len(data)} bytes, "
        f"lines={line_count}, omitted~={max(omitted, 0)} bytes]\n\n"
    )
    sys.stdout.write(head)
    sys.stdout.write(marker)
    sys.stdout.write(tail)
    return 0


def cmd_cid(_args: argparse.Namespace) -> int:
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"loop_utils cid: invalid JSON: {exc}", file=sys.stderr)
        return 1
    if isinstance(payload, list):
        record = payload[0] if payload else {}
    elif isinstance(payload, dict):
        record = payload
    else:
        record = {}
    cid = record.get("conversation_id")
    if not cid:
        print("loop_utils cid: conversation_id not found", file=sys.stderr)
        return 1
    print(cid)
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    extract = subparsers.add_parser("extract-bash")
    extract.set_defaults(func=cmd_extract_bash)

    truncate = subparsers.add_parser("truncate")
    truncate.add_argument("--max-chars", type=int, default=12000)
    truncate.add_argument("--head-chars", type=int, default=8000)
    truncate.add_argument("--tail-chars", type=int, default=4000)
    truncate.set_defaults(func=cmd_truncate)

    cid = subparsers.add_parser("cid")
    cid.set_defaults(func=cmd_cid)

    return parser.parse_args()


def main() -> int:
    args = parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
