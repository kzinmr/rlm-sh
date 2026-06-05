#!/usr/bin/env python3
"""Docker sandbox helpers for rlm-sh M0/M1."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import subprocess
import sys
from uuid import uuid4


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_IMAGE = "rlm-sh-sandbox:dev"


def sanitize_name(value: str) -> str:
    name = re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-")
    if not name:
        name = uuid4().hex
    if not re.match(r"^[A-Za-z0-9]", name):
        name = f"rlm-{name}"
    return name[:63]


def require_key(env_name: str, *, allow_openai_fallback: bool = False) -> str:
    value = os.environ.get(env_name)
    if value:
        return value
    if (
        allow_openai_fallback
        and env_name != "OPENAI_API_KEY"
        and os.environ.get("OPENAI_API_KEY")
    ):
        print(
            f"sandbox.py: {env_name} is not set; falling back to OPENAI_API_KEY",
            file=sys.stderr,
        )
        return os.environ["OPENAI_API_KEY"]
    raise SystemExit(
        f"sandbox.py: missing API key. Set {env_name} to a low-budget provider key."
    )


def run_checked(cmd: list[str], *, capture: bool = False, timeout: float | None = None):
    try:
        return subprocess.run(
            cmd,
            text=True,
            capture_output=capture,
            check=True,
            timeout=timeout,
        )
    except subprocess.CalledProcessError as exc:
        if exc.stdout:
            print(exc.stdout, end="", file=sys.stdout)
        if exc.stderr:
            print(exc.stderr, end="", file=sys.stderr)
        raise SystemExit(exc.returncode) from exc
    except subprocess.TimeoutExpired as exc:
        print(f"sandbox.py: command timed out after {timeout}s", file=sys.stderr)
        if exc.stdout:
            stdout = exc.stdout if isinstance(exc.stdout, str) else exc.stdout.decode()
            print(stdout, end="", file=sys.stdout)
        if exc.stderr:
            stderr = exc.stderr if isinstance(exc.stderr, str) else exc.stderr.decode()
            print(stderr, end="", file=sys.stderr)
        raise SystemExit(124) from exc


def docker_run_args(args: argparse.Namespace, key: str) -> list[str]:
    work_dir = Path(args.work_dir).resolve()
    context_dir = Path(args.context_dir).resolve()
    work_dir.mkdir(parents=True, exist_ok=True)
    if not context_dir.is_dir():
        raise SystemExit(f"sandbox.py: context dir not found: {context_dir}")

    name = sanitize_name(args.name or f"rlm-sh-{args.run_id}")
    cmd = [
        "docker",
        "run",
        "-d",
        "--rm",
        "--name",
        name,
        "--memory",
        args.memory,
        "--cpus",
        str(args.cpus),
        "--pids-limit",
        str(args.pids_limit),
        "-e",
        f"OPENAI_API_KEY={key}",
        "-e",
        "LLM_USER_PATH=/work/.llm",
        "-e",
        f"RLM_SH_RUN_ID={args.run_id}",
        "-e",
        f"RLM_SH_DEPTH={args.depth}",
        "-e",
        f"RLM_SH_SANDBOX_ID={name}",
        "-v",
        f"{work_dir}:/work",
        "-v",
        f"{context_dir}:/context:ro",
    ]
    if args.read_only_root:
        cmd.extend(
            [
                "--read-only",
                "--tmpfs",
                "/tmp:rw,nosuid,nodev,size=256m",
            ]
        )
    cmd.extend([args.image, "tail", "-f", "/dev/null"])
    return cmd


def preflight_mounts(container_name: str, work_dir: Path) -> None:
    probe_name = f".rlmsh_mount_probe_{uuid4().hex}"
    probe_path = work_dir / probe_name
    script = (
        "set -euo pipefail; "
        f"printf ok > /work/{probe_name}; "
        f"test \"$(cat /work/{probe_name})\" = ok; "
        "test -d /context; "
        "test -r /context; "
        "if touch /context/.rlmsh_write_probe 2>/dev/null; then "
        "rm -f /context/.rlmsh_write_probe; "
        "echo '/context is writable but must be read-only' >&2; "
        "exit 1; "
        "fi"
    )
    try:
        subprocess.run(
            ["docker", "exec", container_name, "bash", "-lc", script],
            text=True,
            capture_output=True,
            check=True,
            timeout=15,
        )
    except subprocess.CalledProcessError as exc:
        if exc.stdout:
            print(exc.stdout, end="", file=sys.stdout)
        if exc.stderr:
            print(exc.stderr, end="", file=sys.stderr)
        raise SystemExit(
            "sandbox.py: mount preflight failed. /work must be writable and "
            "/context must be readable read-only. Choose a Docker-shared writable "
            "run directory with --run-dir/--work-dir."
        ) from exc
    except subprocess.TimeoutExpired as exc:
        if exc.stdout:
            stdout = exc.stdout if isinstance(exc.stdout, str) else exc.stdout.decode()
            print(stdout, end="", file=sys.stdout)
        if exc.stderr:
            stderr = exc.stderr if isinstance(exc.stderr, str) else exc.stderr.decode()
            print(stderr, end="", file=sys.stderr)
        raise SystemExit("sandbox.py: mount preflight timed out") from exc

    if not probe_path.is_file():
        raise SystemExit(
            "sandbox.py: mount preflight failed. Container wrote to /work, but "
            "the probe was not visible on the host bind mount."
        )
    content = probe_path.read_text(encoding="utf-8")
    probe_path.unlink(missing_ok=True)
    if content != "ok":
        raise SystemExit("sandbox.py: mount preflight failed. Probe content mismatch.")


def cmd_build(args: argparse.Namespace) -> int:
    cmd = [
        "docker",
        "build",
        "-f",
        str(PROJECT_ROOT / "Dockerfile.sandbox"),
        "-t",
        args.image,
        str(PROJECT_ROOT),
    ]
    run_checked(cmd)
    return 0


def cmd_start(args: argparse.Namespace) -> int:
    key = require_key(
        args.api_key_env,
        allow_openai_fallback=args.allow_openai_key_fallback,
    )
    name = sanitize_name(args.name or f"rlm-sh-{args.run_id}")
    args.name = name
    cmd = docker_run_args(args, key)
    started = False
    try:
        run_checked(cmd, capture=True)
        started = True
        if not args.skip_preflight:
            preflight_mounts(name, Path(args.work_dir).resolve())
    except (Exception, SystemExit):
        if started:
            subprocess.run(["docker", "rm", "-f", name], capture_output=True)
        raise
    print(name)
    return 0


def cmd_stop(args: argparse.Namespace) -> int:
    run_checked(["docker", "rm", "-f", args.container], capture=True)
    return 0


def cmd_exec(args: argparse.Namespace) -> int:
    if args.command and args.command[0] == "--":
        args.command = args.command[1:]
    if not args.command:
        raise SystemExit("sandbox.py exec: missing command after --")
    command = args.command[0] if len(args.command) == 1 else " ".join(args.command)
    timeout_seconds = f"{args.timeout:g}s"
    cmd = [
        "docker",
        "exec",
        args.container,
        "timeout",
        "--kill-after=2s",
        timeout_seconds,
        "bash",
        "-lc",
        command,
    ]
    try:
        completed = subprocess.run(cmd, text=True, timeout=args.timeout + 5)
    except subprocess.TimeoutExpired:
        print(f"sandbox.py exec: command timed out after {args.timeout:g}s")
        return 124
    if completed.returncode == 124:
        print(f"sandbox.py exec: command timed out after {args.timeout:g}s")
    return completed.returncode


def cmd_m0_check(args: argparse.Namespace) -> int:
    run_id = args.run_id or f"m0-{uuid4().hex[:12]}"
    temp_root = (
        Path(args.run_dir).resolve()
        if args.run_dir
        else PROJECT_ROOT / ".runs" / run_id
    )
    context_dir = Path(args.context_dir).resolve() if args.context_dir else temp_root / "context"
    work_dir = temp_root / "work"
    context_dir.mkdir(parents=True, exist_ok=True)
    (context_dir / "context.txt").write_text("M0 context placeholder\n", encoding="utf-8")

    start_args = argparse.Namespace(
        image=args.image,
        name=args.name or f"rlm-sh-{run_id}",
        work_dir=str(work_dir),
        context_dir=str(context_dir),
        memory=args.memory,
        cpus=args.cpus,
        pids_limit=args.pids_limit,
        api_key_env=args.api_key_env,
        run_id=run_id,
        depth=0,
        read_only_root=args.read_only_root,
        allow_openai_key_fallback=args.allow_openai_key_fallback,
        skip_preflight=args.skip_preflight,
    )
    if args.build:
        cmd_build(argparse.Namespace(image=args.image))
    container_name = sanitize_name(start_args.name)
    try:
        cmd_start(start_args)
        version = subprocess.run(
            ["docker", "exec", container_name, "llm", "--version"],
            text=True,
            check=True,
            capture_output=True,
        )
        print(version.stdout, end="")
        if args.live_llm:
            live = subprocess.run(
                [
                    "docker",
                    "exec",
                    container_name,
                    "llm",
                    "-m",
                    args.model,
                    "--no-stream",
                    "Return exactly: ok",
                ],
                text=True,
                check=True,
                capture_output=True,
                timeout=args.timeout,
            )
            print(live.stdout, end="")
    finally:
        if not args.keep:
            subprocess.run(["docker", "rm", "-f", container_name], capture_output=True)
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    build = subparsers.add_parser("build")
    build.add_argument("--image", default=DEFAULT_IMAGE)
    build.set_defaults(func=cmd_build)

    start = subparsers.add_parser("start")
    start.add_argument("--image", default=DEFAULT_IMAGE)
    start.add_argument("--name", default=None)
    start.add_argument("--work-dir", required=True)
    start.add_argument("--context-dir", required=True)
    start.add_argument("--memory", default="4g")
    start.add_argument("--cpus", type=float, default=2.0)
    start.add_argument("--pids-limit", type=int, default=512)
    start.add_argument("--api-key-env", default="RLMSH_KEY")
    start.add_argument("--allow-openai-key-fallback", action="store_true")
    start.add_argument("--run-id", default=f"run-{uuid4().hex[:12]}")
    start.add_argument("--depth", type=int, default=0)
    start.add_argument("--read-only-root", action="store_true")
    start.add_argument("--skip-preflight", action="store_true")
    start.set_defaults(func=cmd_start)

    stop = subparsers.add_parser("stop")
    stop.add_argument("--container", required=True)
    stop.set_defaults(func=cmd_stop)

    exec_parser = subparsers.add_parser("exec")
    exec_parser.add_argument("--container", required=True)
    exec_parser.add_argument("--timeout", type=float, default=30.0)
    exec_parser.add_argument("command", nargs=argparse.REMAINDER)
    exec_parser.set_defaults(func=cmd_exec)

    m0 = subparsers.add_parser("m0-check")
    m0.add_argument("--image", default=DEFAULT_IMAGE)
    m0.add_argument("--name", default=None)
    m0.add_argument("--run-dir", default=None)
    m0.add_argument("--context-dir", default=None)
    m0.add_argument("--api-key-env", default="RLMSH_KEY")
    m0.add_argument("--allow-openai-key-fallback", action="store_true")
    m0.add_argument("--memory", default="1g")
    m0.add_argument("--cpus", type=float, default=1.0)
    m0.add_argument("--pids-limit", type=int, default=256)
    m0.add_argument("--model", default="gpt-5-mini")
    m0.add_argument("--timeout", type=float, default=60.0)
    m0.add_argument("--run-id", default=None)
    m0.add_argument("--build", action="store_true")
    m0.add_argument("--live-llm", action="store_true")
    m0.add_argument("--keep", action="store_true")
    m0.add_argument("--read-only-root", action="store_true")
    m0.add_argument("--skip-preflight", action="store_true")
    m0.set_defaults(func=cmd_m0_check)

    return parser.parse_args()


def main() -> int:
    args = parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
