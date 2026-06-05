#!/usr/bin/env python3
"""External brain adapters for running rlm-sh with non-default controllers."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent

ADAPTERS: dict[str, dict[str, str]] = {
    "pure-shell": {
        "kind": "host-loop",
        "description": "Use host/loop_shell.sh and the llm CLI conversation loop.",
    },
    "claude-code": {
        "kind": "template",
        "env": "RLM_SH_ADAPTER_CLAUDE_CODE_CMD",
        "description": "Run a Claude Code command template inside the sandbox.",
    },
    "codex": {
        "kind": "template",
        "env": "RLM_SH_ADAPTER_CODEX_CMD",
        "description": "Run a Codex CLI command template inside the sandbox.",
    },
    "pi": {
        "kind": "template",
        "env": "RLM_SH_ADAPTER_PI_CMD",
        "description": "Run a Pi CLI command template inside the sandbox.",
    },
}


def adapter_prompt(query: str) -> str:
    return f"""You are controlling an rlm-sh sandbox.

Task:
{query}

Contract:
- Input is under /context and must be treated as read-only.
- Use /work for all working files.
- Finish by writing the final answer to /work/answer.txt or by running submit.
- Do not print large context files to stdout.
"""


def run_pure_shell(args: argparse.Namespace) -> int:
    cmd = [
        str(SCRIPT_DIR / "loop_shell.sh"),
        "--query",
        args.query,
        "--context-dir",
        args.context_dir,
        "--backend",
        args.backend,
        "--root-model",
        args.root_model,
    ]
    if args.run_dir:
        cmd.extend(["--run-dir", args.run_dir])
    if args.build:
        cmd.append("--build")
    if args.allow_openai_key_fallback:
        cmd.append("--allow-openai-key-fallback")
    return subprocess.run(cmd, text=True).returncode


def template_for(adapter: str, explicit: str | None) -> str:
    if explicit:
        return explicit
    spec = ADAPTERS[adapter]
    env_name = spec.get("env")
    if env_name and os.environ.get(env_name):
        return os.environ[env_name]
    raise SystemExit(
        f"adapters.py: {adapter} requires --command-template or {env_name}. "
        "The template runs inside the sandbox and should write /work/answer.txt."
    )


def run_template_adapter(args: argparse.Namespace) -> int:
    run_id = args.run_id or f"adapter-{os.getpid()}"
    run_dir = Path(args.run_dir or PROJECT_ROOT / ".runs" / run_id).resolve()
    work_dir = run_dir / "work"
    work_dir.mkdir(parents=True, exist_ok=True)
    prompt_path = work_dir / "adapter_prompt.txt"
    prompt_path.write_text(adapter_prompt(args.query), encoding="utf-8")

    start_cmd = [
        str(SCRIPT_DIR / "sandbox.py"),
        "start",
        "--backend",
        args.backend,
        "--image",
        args.image,
        "--work-dir",
        str(work_dir),
        "--context-dir",
        args.context_dir,
        "--api-key-env",
        args.api_key_env,
        "--run-id",
        run_id,
    ]
    if args.allow_openai_key_fallback:
        start_cmd.append("--allow-openai-key-fallback")
    if args.skip_preflight:
        start_cmd.append("--skip-preflight")
    container = subprocess.run(
        start_cmd,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()
    template = template_for(args.adapter, args.command_template)
    command = template.format(
        prompt="/work/adapter_prompt.txt",
        query=args.query.replace("'", "'\"'\"'"),
    )
    try:
        completed = subprocess.run(
            [
                str(SCRIPT_DIR / "sandbox.py"),
                "exec",
                "--container",
                container,
                "--timeout",
                str(args.timeout),
                "--",
                command,
            ],
            text=True,
        )
        answer = work_dir / "answer.txt"
        if answer.is_file():
            sys.stdout.write(answer.read_text(encoding="utf-8"))
            return completed.returncode
        print(
            f"adapters.py: adapter finished without /work/answer.txt; run_dir={run_dir}",
            file=sys.stderr,
        )
        return completed.returncode or 1
    finally:
        if not args.keep_container:
            subprocess.run(
                [str(SCRIPT_DIR / "sandbox.py"), "stop", "--container", container],
                text=True,
                capture_output=True,
            )


def cmd_list(_args: argparse.Namespace) -> int:
    print(json.dumps(ADAPTERS, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    if args.adapter not in ADAPTERS:
        raise SystemExit(f"adapters.py: unknown adapter: {args.adapter}")
    if ADAPTERS[args.adapter]["kind"] == "host-loop":
        return run_pure_shell(args)
    return run_template_adapter(args)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    list_parser = subparsers.add_parser("list")
    list_parser.set_defaults(func=cmd_list)

    run = subparsers.add_parser("run")
    run.add_argument("--adapter", choices=sorted(ADAPTERS), required=True)
    run.add_argument("--query", required=True)
    run.add_argument("--context-dir", required=True)
    run.add_argument("--run-dir", default=None)
    run.add_argument("--run-id", default=None)
    run.add_argument("--backend", default="docker")
    run.add_argument("--image", default="rlm-sh-sandbox:dev")
    run.add_argument("--root-model", default="gpt-5")
    run.add_argument("--api-key-env", default="RLMSH_KEY")
    run.add_argument("--command-template", default=None)
    run.add_argument("--timeout", type=float, default=900.0)
    run.add_argument("--build", action="store_true")
    run.add_argument("--allow-openai-key-fallback", action="store_true")
    run.add_argument("--skip-preflight", action="store_true")
    run.add_argument("--keep-container", action="store_true")
    run.set_defaults(func=cmd_run)

    return parser.parse_args()


def main() -> int:
    args = parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
