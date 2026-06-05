#!/usr/bin/env python3
"""List and check rlm-sh sandbox backend adapters."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys


BACKENDS = {
    "docker": {
        "status": "implemented",
        "description": "Default Docker container backend with /work and /context mounts.",
        "requires": ["docker"],
    },
    "local-unsafe": {
        "status": "implemented-debug",
        "description": "Runs commands on the host with best-effort /work and /context path rewriting.",
        "requires": ["bash"],
    },
    "e2b": {
        "status": "declared-stub",
        "description": "Reserved for an E2B adapter; start currently fails until configured.",
        "requires": [],
    },
    "docker-sandboxes": {
        "status": "implemented",
        "description": "Docker Sandboxes backend via sbx create/exec/rm with /work and /context aliases.",
        "requires": ["sbx"],
    },
}


def cmd_list(_args: argparse.Namespace) -> int:
    print(json.dumps(BACKENDS, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    if args.backend not in BACKENDS:
        raise SystemExit(f"backends.py: unknown backend: {args.backend}")
    backend = BACKENDS[args.backend]
    missing = [cmd for cmd in backend["requires"] if shutil.which(cmd) is None]
    result = {
        "backend": args.backend,
        "status": backend["status"],
        "missing_commands": missing,
        "ok": not missing and not backend["status"].endswith("stub"),
    }
    if args.backend == "docker" and not missing:
        probe = subprocess.run(
            ["docker", "version", "--format", "{{json .Server.Version}}"],
            text=True,
            capture_output=True,
        )
        result["docker_returncode"] = probe.returncode
        result["docker_server_version"] = probe.stdout.strip()
        result["docker_stderr"] = probe.stderr.strip()
        result["ok"] = probe.returncode == 0
    if args.backend == "docker-sandboxes" and not missing:
        probe = subprocess.run(
            ["sbx", "version"],
            text=True,
            capture_output=True,
        )
        result["sbx_returncode"] = probe.returncode
        result["sbx_stdout"] = probe.stdout.strip()
        result["sbx_stderr"] = probe.stderr.strip()
        result["ok"] = probe.returncode == 0 and "Server Version:  Unavailable" not in probe.stdout
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result["ok"] else 1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    list_parser = subparsers.add_parser("list")
    list_parser.set_defaults(func=cmd_list)

    check = subparsers.add_parser("check")
    check.add_argument("--backend", required=True)
    check.set_defaults(func=cmd_check)

    return parser.parse_args()


def main() -> int:
    args = parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
