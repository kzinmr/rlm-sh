#!/usr/bin/env python3
"""Sandbox helpers for rlm-sh M0-M6."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path
from uuid import uuid4

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_IMAGE = "rlm-sh-sandbox:dev"
SUPPORTED_BACKENDS = {"docker", "local-unsafe", "docker-sandboxes"}
LOCAL_PREFIX = "local-unsafe:"
SBX_PREFIX = "docker-sandboxes:"


def sanitize_name(value: str) -> str:
    name = re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-")
    if not name:
        name = uuid4().hex
    if not re.match(r"^[A-Za-z0-9]", name):
        name = f"rlm-{name}"
    return name[:63]


def default_sandbox_name(args: argparse.Namespace) -> str:
    suffix = uuid4().hex[:8]
    return sanitize_name(f"rlm-sh-{args.run_id}-d{args.depth}-{suffix}")


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
        "--label",
        "rlm-sh.managed=true",
        "--label",
        f"rlm-sh.run_id={args.run_id}",
        "--label",
        f"rlm-sh.depth={args.depth}",
        "--label",
        f"rlm-sh.sandbox_id={name}",
        "--label",
        f"rlm-sh.parent_sandbox_id={args.parent_sandbox_id}",
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
        "-e",
        "RLM_SH_SPAWN_DIR=/work/.spawn",
        "-e",
        f"RLM_SH_BACKEND={args.backend}",
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


def local_state_path(work_dir: Path) -> Path:
    return work_dir / ".rlmsh_local_sandbox.json"


def local_handle(state_path: Path) -> str:
    return f"{LOCAL_PREFIX}{state_path}"


def read_local_state(handle: str) -> dict[str, object]:
    if not handle.startswith(LOCAL_PREFIX):
        raise SystemExit(f"sandbox.py: invalid local-unsafe handle: {handle}")
    state_path = Path(handle[len(LOCAL_PREFIX) :])
    try:
        return json.loads(state_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SystemExit(
            f"sandbox.py: local backend state not found: {state_path}"
        ) from exc
    except json.JSONDecodeError as exc:
        raise SystemExit(
            f"sandbox.py: invalid local backend state: {state_path}"
        ) from exc


def cmd_start_local(args: argparse.Namespace) -> int:
    # The local backend is intentionally unsafe: commands run on the host.
    # It exists for fast debugging when Docker is unavailable.
    work_dir = Path(args.work_dir).resolve()
    context_dir = Path(args.context_dir).resolve()
    work_dir.mkdir(parents=True, exist_ok=True)
    (work_dir / ".spawn").mkdir(parents=True, exist_ok=True)
    if not context_dir.is_dir():
        raise SystemExit(f"sandbox.py: context dir not found: {context_dir}")
    require_key(
        args.api_key_env,
        allow_openai_fallback=args.allow_openai_key_fallback,
    )
    name = sanitize_name(args.name) if args.name else default_sandbox_name(args)
    state = {
        "backend": "local-unsafe",
        "name": name,
        "work_dir": str(work_dir),
        "context_dir": str(context_dir),
        "api_key_env": args.api_key_env,
        "allow_openai_key_fallback": args.allow_openai_key_fallback,
        "run_id": args.run_id,
        "depth": args.depth,
        "sandbox_id": name,
        "parent_sandbox_id": args.parent_sandbox_id,
    }
    state_path = local_state_path(work_dir)
    state_path.write_text(
        json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(local_handle(state_path))
    return 0


def sbx_state_path(work_dir: Path) -> Path:
    return work_dir / ".rlmsh_sbx_sandbox.json"


def sbx_handle(state_path: Path) -> str:
    return f"{SBX_PREFIX}{state_path}"


def read_sbx_state(handle: str) -> dict[str, object]:
    if not handle.startswith(SBX_PREFIX):
        raise SystemExit(f"sandbox.py: invalid docker-sandboxes handle: {handle}")
    state_path = Path(handle[len(SBX_PREFIX) :])
    try:
        return json.loads(state_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SystemExit(
            f"sandbox.py: docker-sandboxes state not found: {state_path}"
        ) from exc
    except json.JSONDecodeError as exc:
        raise SystemExit(
            f"sandbox.py: invalid docker-sandboxes state: {state_path}"
        ) from exc


def sbx_cpus(value: float) -> str:
    return str(max(1, math.ceil(value)))


def sbx_exec_base() -> list[str]:
    return ["sbx", "exec"]


def sbx_setup_mount_aliases(name: str, work_dir: Path, context_dir: Path) -> None:
    script = (
        "set -euo pipefail; "
        "rm -rf /work /context; "
        f"ln -s {shlex.quote(str(work_dir))} /work; "
        f"ln -s {shlex.quote(str(context_dir))} /context; "
        "mkdir -p /work/.spawn"
    )
    run_checked(
        [
            "sbx",
            "exec",
            "-u",
            "root",
            name,
            "bash",
            "-lc",
            script,
        ],
        capture=True,
        timeout=60,
    )


def preflight_sbx_mounts(name: str, work_dir: Path) -> None:
    probe_name = f".rlmsh_mount_probe_{uuid4().hex}"
    probe_path = work_dir / probe_name
    script = (
        "set -euo pipefail; "
        f"printf ok > /work/{probe_name}; "
        f'test "$(cat /work/{probe_name})" = ok; '
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
            ["sbx", "exec", "-u", "root", name, "bash", "-lc", script],
            text=True,
            capture_output=True,
            check=True,
            timeout=30,
        )
    except subprocess.CalledProcessError as exc:
        if exc.stdout:
            print(exc.stdout, end="", file=sys.stdout)
        if exc.stderr:
            print(exc.stderr, end="", file=sys.stderr)
        raise SystemExit(
            "sandbox.py: docker-sandboxes mount preflight failed. /work must be "
            "writable and /context must be readable read-only."
        ) from exc
    except subprocess.TimeoutExpired as exc:
        if exc.stdout:
            stdout = exc.stdout if isinstance(exc.stdout, str) else exc.stdout.decode()
            print(stdout, end="", file=sys.stdout)
        if exc.stderr:
            stderr = exc.stderr if isinstance(exc.stderr, str) else exc.stderr.decode()
            print(stderr, end="", file=sys.stderr)
        raise SystemExit(
            "sandbox.py: docker-sandboxes mount preflight timed out"
        ) from exc

    if not probe_path.is_file():
        raise SystemExit(
            "sandbox.py: docker-sandboxes mount preflight failed. Sandbox wrote to "
            "/work, but the probe was not visible on the host bind mount."
        )
    content = probe_path.read_text(encoding="utf-8")
    probe_path.unlink(missing_ok=True)
    if content != "ok":
        raise SystemExit(
            "sandbox.py: docker-sandboxes mount preflight content mismatch."
        )


def cmd_start_sbx(args: argparse.Namespace) -> int:
    work_dir = Path(args.work_dir).resolve()
    context_dir = Path(args.context_dir).resolve()
    work_dir.mkdir(parents=True, exist_ok=True)
    (work_dir / ".spawn").mkdir(parents=True, exist_ok=True)
    if not context_dir.is_dir():
        raise SystemExit(f"sandbox.py: context dir not found: {context_dir}")
    require_key(
        args.api_key_env,
        allow_openai_fallback=args.allow_openai_key_fallback,
    )

    name = sanitize_name(args.name) if args.name else default_sandbox_name(args)
    create_cmd = [
        "sbx",
        "create",
        "shell",
        "--name",
        name,
        "--template",
        args.image,
        "--memory",
        args.memory,
        "--cpus",
        sbx_cpus(args.cpus),
        "--quiet",
        str(work_dir),
        f"{context_dir}:ro",
    ]
    created = False
    try:
        run_checked(create_cmd, capture=True, timeout=120)
        created = True
        sbx_setup_mount_aliases(name, work_dir, context_dir)
        if not args.skip_preflight:
            preflight_sbx_mounts(name, work_dir)
    except (Exception, SystemExit):
        if created:
            subprocess.run(["sbx", "rm", "--force", name], capture_output=True)
        raise

    state = {
        "backend": "docker-sandboxes",
        "name": name,
        "work_dir": str(work_dir),
        "context_dir": str(context_dir),
        "api_key_env": args.api_key_env,
        "allow_openai_key_fallback": args.allow_openai_key_fallback,
        "run_id": args.run_id,
        "depth": args.depth,
        "sandbox_id": name,
        "parent_sandbox_id": args.parent_sandbox_id,
    }
    state_path = sbx_state_path(work_dir)
    state_path.write_text(
        json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(sbx_handle(state_path))
    return 0


def cmd_exec_sbx(args: argparse.Namespace) -> int:
    state = read_sbx_state(args.container)
    api_key_env = str(state.get("api_key_env") or "RLMSH_KEY")
    key = require_key(
        api_key_env,
        allow_openai_fallback=bool(state.get("allow_openai_key_fallback")),
    )
    name = str(state["name"])
    command = args.command[0] if len(args.command) == 1 else " ".join(args.command)
    cmd = sbx_exec_base()
    env_pairs = [
        f"OPENAI_API_KEY={key}",
        "PATH=/root/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "LLM_USER_PATH=/work/.llm",
        f"RLM_SH_RUN_ID={state.get('run_id') or ''}",
        f"RLM_SH_DEPTH={state.get('depth') or 0}",
        f"RLM_SH_SANDBOX_ID={state.get('sandbox_id') or name}",
        "RLM_SH_SPAWN_DIR=/work/.spawn",
        "RLM_SH_BACKEND=docker-sandboxes",
    ]
    env_pairs.extend(args.env)
    if args.parent_call_id:
        env_pairs.append(f"RLM_SH_PARENT_CALL_ID={args.parent_call_id}")
    for pair in env_pairs:
        key_name, sep, _value = pair.partition("=")
        if not sep or not key_name:
            raise SystemExit(f"sandbox.py exec: invalid --env value: {pair}")
        cmd.extend(["-e", pair])
    cmd.extend(
        [
            "-u",
            "root",
            "-w",
            "/work",
            name,
            "timeout",
            "--kill-after=2s",
            f"{args.timeout:g}s",
            "bash",
            "-c",
            command,
        ]
    )
    try:
        completed = subprocess.run(cmd, text=True, timeout=args.timeout + 15)
    except subprocess.TimeoutExpired:
        print(f"sandbox.py exec: command timed out after {args.timeout:g}s")
        return 124
    if completed.returncode == 124:
        print(f"sandbox.py exec: command timed out after {args.timeout:g}s")
    return completed.returncode


def cmd_stop_sbx(args: argparse.Namespace) -> int:
    state = read_sbx_state(args.container)
    run_checked(["sbx", "rm", "--force", str(state["name"])], capture=True)
    return 0


def sbx_names_for_run(run_id: str) -> list[str]:
    prefix = sanitize_name(f"rlm-sh-{run_id}-")
    names: list[str] = []
    json_result = subprocess.run(
        ["sbx", "ls", "--json"],
        text=True,
        capture_output=True,
        timeout=30,
    )
    if json_result.returncode == 0:
        try:
            payload = json.loads(json_result.stdout)
            rows = (
                payload if isinstance(payload, list) else payload.get("sandboxes", [])
            )
            for row in rows:
                if isinstance(row, dict):
                    name = str(row.get("name") or row.get("Name") or "")
                else:
                    name = str(row)
                if name.startswith(prefix):
                    names.append(name)
            return sorted(set(names))
        except json.JSONDecodeError:
            pass
    quiet = subprocess.run(
        ["sbx", "ls", "--quiet"],
        text=True,
        capture_output=True,
        timeout=30,
    )
    if quiet.returncode != 0:
        if quiet.stdout:
            print(quiet.stdout, end="", file=sys.stdout)
        if quiet.stderr:
            print(quiet.stderr, end="", file=sys.stderr)
        raise SystemExit(quiet.returncode)
    return sorted(
        {line.strip() for line in quiet.stdout.splitlines() if line.startswith(prefix)}
    )


def cmd_stop_run_sbx(args: argparse.Namespace) -> int:
    names = sbx_names_for_run(args.run_id)
    if not names:
        return 0
    run_checked(["sbx", "rm", "--force", *names], capture=True, timeout=60)
    return 0


def rewrite_local_paths(command: str, work_dir: Path, context_dir: Path) -> str:
    # Best-effort compatibility for the /work and /context contract. Paths in this
    # repo do not contain spaces; local-unsafe callers with exotic paths should use
    # Docker instead.
    command = re.sub(r"/context(?=/|$)", str(context_dir), command)
    return re.sub(r"/work(?=/|$)", str(work_dir), command)


def cmd_exec_local(args: argparse.Namespace) -> int:
    state = read_local_state(args.container)
    work_dir = Path(str(state["work_dir"]))
    context_dir = Path(str(state["context_dir"]))
    api_key_env = str(state.get("api_key_env") or "RLMSH_KEY")
    key = require_key(
        api_key_env,
        allow_openai_fallback=bool(state.get("allow_openai_key_fallback")),
    )
    command = args.command[0] if len(args.command) == 1 else " ".join(args.command)
    command = rewrite_local_paths(command, work_dir, context_dir)
    env = os.environ.copy()
    env.update(
        {
            "OPENAI_API_KEY": key,
            "LLM_USER_PATH": str(work_dir / ".llm"),
            "RLM_SH_RUN_ID": str(state.get("run_id") or ""),
            "RLM_SH_DEPTH": str(state.get("depth") or 0),
            "RLM_SH_SANDBOX_ID": str(state.get("sandbox_id") or ""),
            "RLM_SH_SPAWN_DIR": str(work_dir / ".spawn"),
            "RLM_SH_BACKEND": "local-unsafe",
        }
    )
    for pair in args.env:
        key_name, sep, value = pair.partition("=")
        if not sep or not key_name:
            raise SystemExit(f"sandbox.py exec: invalid --env value: {pair}")
        env[key_name] = value
    if args.parent_call_id:
        env["RLM_SH_PARENT_CALL_ID"] = args.parent_call_id
    try:
        completed = subprocess.run(
            ["bash", "-lc", command],
            cwd=work_dir,
            text=True,
            timeout=args.timeout,
            env=env,
        )
    except subprocess.TimeoutExpired:
        print(f"sandbox.py exec: command timed out after {args.timeout:g}s")
        return 124
    if completed.returncode == 124:
        print(f"sandbox.py exec: command timed out after {args.timeout:g}s")
    return completed.returncode


def preflight_mounts(container_name: str, work_dir: Path) -> None:
    probe_name = f".rlmsh_mount_probe_{uuid4().hex}"
    probe_path = work_dir / probe_name
    script = (
        "set -euo pipefail; "
        f"printf ok > /work/{probe_name}; "
        f'test "$(cat /work/{probe_name})" = ok; '
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
    backend = getattr(args, "backend", "docker")
    if backend == "local-unsafe":
        print("sandbox.py: local-unsafe backend does not require an image build")
        return 0
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
    if backend == "docker-sandboxes":
        with tempfile.TemporaryDirectory(prefix="rlm-sh-sbx-template-") as tmp:
            tar_path = Path(tmp) / "rlm-sh-sandbox.tar"
            run_checked(["docker", "save", "--output", str(tar_path), args.image])
            run_checked(["sbx", "template", "load", str(tar_path)])
    return 0


def cmd_start(args: argparse.Namespace) -> int:
    if args.backend == "local-unsafe":
        return cmd_start_local(args)
    if args.backend == "docker-sandboxes":
        return cmd_start_sbx(args)
    key = require_key(
        args.api_key_env,
        allow_openai_fallback=args.allow_openai_key_fallback,
    )
    name = sanitize_name(args.name) if args.name else default_sandbox_name(args)
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
    if args.container.startswith(LOCAL_PREFIX):
        return 0
    if args.container.startswith(SBX_PREFIX):
        return cmd_stop_sbx(args)
    run_checked(["docker", "rm", "-f", args.container], capture=True)
    return 0


def cmd_stop_run(args: argparse.Namespace) -> int:
    if args.backend == "docker-sandboxes":
        return cmd_stop_run_sbx(args)
    if args.backend != "docker":
        return 0
    ps = run_checked(
        [
            "docker",
            "ps",
            "-aq",
            "--filter",
            "label=rlm-sh.managed=true",
            "--filter",
            f"label=rlm-sh.run_id={args.run_id}",
        ],
        capture=True,
    )
    container_ids = [line for line in ps.stdout.splitlines() if line]
    if not container_ids:
        return 0
    run_checked(["docker", "rm", "-f", *container_ids], capture=True)
    return 0


def cmd_exec(args: argparse.Namespace) -> int:
    if args.command and args.command[0] == "--":
        args.command = args.command[1:]
    if not args.command:
        raise SystemExit("sandbox.py exec: missing command after --")
    if args.container.startswith(LOCAL_PREFIX):
        return cmd_exec_local(args)
    if args.container.startswith(SBX_PREFIX):
        return cmd_exec_sbx(args)
    command = args.command[0] if len(args.command) == 1 else " ".join(args.command)
    timeout_seconds = f"{args.timeout:g}s"
    cmd = [
        "docker",
        "exec",
    ]
    for pair in args.env:
        key_name, sep, _value = pair.partition("=")
        if not sep or not key_name:
            raise SystemExit(f"sandbox.py exec: invalid --env value: {pair}")
        cmd.extend(["-e", pair])
    if args.parent_call_id:
        cmd.extend(["-e", f"RLM_SH_PARENT_CALL_ID={args.parent_call_id}"])
    cmd.extend(
        [
            args.container,
            "timeout",
            "--kill-after=2s",
            timeout_seconds,
            "bash",
            "-lc",
            command,
        ]
    )
    try:
        completed = subprocess.run(cmd, text=True, timeout=args.timeout + 5)
    except subprocess.TimeoutExpired:
        print(f"sandbox.py exec: command timed out after {args.timeout:g}s")
        return 124
    if completed.returncode == 124:
        print(f"sandbox.py exec: command timed out after {args.timeout:g}s")
    return completed.returncode


def cmd_m0_check(args: argparse.Namespace) -> int:
    if args.backend not in {"docker", "docker-sandboxes"}:
        raise SystemExit(
            "sandbox.py m0-check currently validates docker and docker-sandboxes only"
        )
    run_id = args.run_id or f"m0-{uuid4().hex[:12]}"
    temp_root = (
        Path(args.run_dir).resolve()
        if args.run_dir
        else PROJECT_ROOT / ".runs" / run_id
    )
    context_dir = (
        Path(args.context_dir).resolve() if args.context_dir else temp_root / "context"
    )
    work_dir = temp_root / "work"
    context_dir.mkdir(parents=True, exist_ok=True)
    (context_dir / "context.txt").write_text(
        "M0 context placeholder\n", encoding="utf-8"
    )

    start_args = argparse.Namespace(
        backend=args.backend,
        image=args.image,
        name=args.name or f"rlm-sh-{run_id}-m0",
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
        parent_sandbox_id="",
    )
    if args.build:
        cmd_build(argparse.Namespace(backend=args.backend, image=args.image))
    if args.backend == "docker-sandboxes":
        handle = sbx_handle(sbx_state_path(work_dir))
        try:
            cmd_start(start_args)
            version_status = cmd_exec(
                argparse.Namespace(
                    container=handle,
                    timeout=args.timeout,
                    env=[],
                    parent_call_id="",
                    command=["llm --version"],
                )
            )
            if version_status != 0:
                return version_status
            if args.live_llm:
                return cmd_exec(
                    argparse.Namespace(
                        container=handle,
                        timeout=args.timeout,
                        env=[],
                        parent_call_id="",
                        command=[
                            f"llm -m {shlex.quote(args.model)} --no-stream 'Return exactly: ok'"
                        ],
                    )
                )
        finally:
            if not args.keep and handle:
                subprocess.run(
                    [
                        sys.executable,
                        str(Path(__file__).resolve()),
                        "stop",
                        "--container",
                        handle,
                    ],
                    capture_output=True,
                    text=True,
                )
        return 0
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
    build.add_argument(
        "--backend",
        choices=sorted(SUPPORTED_BACKENDS),
        default="docker",
    )
    build.add_argument("--image", default=DEFAULT_IMAGE)
    build.set_defaults(func=cmd_build)

    start = subparsers.add_parser("start")
    start.add_argument(
        "--backend",
        choices=sorted(SUPPORTED_BACKENDS),
        default="docker",
    )
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
    start.add_argument("--parent-sandbox-id", default="")
    start.add_argument("--read-only-root", action="store_true")
    start.add_argument("--skip-preflight", action="store_true")
    start.set_defaults(func=cmd_start)

    stop = subparsers.add_parser("stop")
    stop.add_argument("--container", required=True)
    stop.set_defaults(func=cmd_stop)

    stop_run = subparsers.add_parser("stop-run")
    stop_run.add_argument("--run-id", required=True)
    stop_run.add_argument(
        "--backend",
        choices=sorted(SUPPORTED_BACKENDS),
        default="docker",
    )
    stop_run.set_defaults(func=cmd_stop_run)

    exec_parser = subparsers.add_parser("exec")
    exec_parser.add_argument("--container", required=True)
    exec_parser.add_argument("--timeout", type=float, default=30.0)
    exec_parser.add_argument(
        "--env",
        action="append",
        default=[],
        help="environment variable to inject into the sandbox exec, KEY=VALUE",
    )
    exec_parser.add_argument("--parent-call-id", default="")
    exec_parser.add_argument("command", nargs=argparse.REMAINDER)
    exec_parser.set_defaults(func=cmd_exec)

    m0 = subparsers.add_parser("m0-check")
    m0.add_argument(
        "--backend",
        choices=sorted(SUPPORTED_BACKENDS),
        default="docker",
    )
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
