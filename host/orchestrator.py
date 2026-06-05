#!/usr/bin/env python3
"""Host-side file orchestrator for recursive rlm-sh spawn requests."""

from __future__ import annotations

import argparse
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(SCRIPT_DIR))

from validators import atomic_write_json, atomic_write_text, snapshot_context  # noqa: E402


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Orchestrator:
    def __init__(self, args: argparse.Namespace) -> None:
        self.parent_work_dir = Path(args.parent_work_dir).resolve()
        self.spawn_dir = Path(args.spawn_dir).resolve() if args.spawn_dir else (
            self.parent_work_dir / ".spawn"
        )
        self.run_dir = Path(args.run_dir).resolve()
        self.loop_shell = Path(args.loop_shell).resolve()
        self.args = args
        self.spawn_dir.mkdir(parents=True, exist_ok=True)
        (self.run_dir / "spawns").mkdir(parents=True, exist_ok=True)
        (self.run_dir / "children").mkdir(parents=True, exist_ok=True)
        self._events_lock = threading.Lock()
        self._active: dict[Path, Future[None]] = {}
        self._seen = 0

    @property
    def events_path(self) -> Path:
        return self.run_dir / "orchestrator_events.jsonl"

    def log_event(self, event: str, **payload: Any) -> None:
        record = {
            "event": event,
            "time": utc_now(),
            "run_id": self.args.run_id,
            "depth": self.args.depth,
            **payload,
        }
        with self._events_lock:
            with self.events_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")

    def run_forever(self) -> int:
        self.log_event(
            "orchestrator_started",
            parent_work_dir=str(self.parent_work_dir),
            spawn_dir=str(self.spawn_dir),
            max_depth=self.args.max_depth,
            max_spawns=self.args.max_spawns,
            backend=self.args.backend,
        )
        with ThreadPoolExecutor(max_workers=self.args.max_parallel_children) as pool:
            try:
                while True:
                    self.scan(pool)
                    self.reap_finished()
                    time.sleep(self.args.poll_interval)
            except KeyboardInterrupt:
                self.log_event("orchestrator_stopped", reason="keyboard_interrupt")
                return 130

    def run_once(self) -> int:
        with ThreadPoolExecutor(max_workers=self.args.max_parallel_children) as pool:
            self.scan(pool)
            while self._active:
                self.reap_finished()
                time.sleep(self.args.poll_interval)
        return 0

    def scan(self, pool: ThreadPoolExecutor) -> None:
        if self._seen >= self.args.max_spawns:
            return
        for request_path in sorted(self.spawn_dir.glob("*.json")):
            if request_path in self._active:
                continue
            if self._seen >= self.args.max_spawns:
                break
            request_id = request_path.stem
            if (self.spawn_dir / f"{request_id}.out").exists():
                continue
            if (self.spawn_dir / f"{request_id}.err").exists():
                continue
            lock_path = self.spawn_dir / f"{request_id}.lock"
            try:
                fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                continue
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(f"pid={os.getpid()} time={utc_now()}\n")
            self._seen += 1
            self.log_event("spawn_queued", request_id=request_id, path=str(request_path))
            self._active[request_path] = pool.submit(self.process_request, request_path)

    def reap_finished(self) -> None:
        done = [path for path, future in self._active.items() if future.done()]
        for path in done:
            future = self._active.pop(path)
            try:
                future.result()
            except Exception as exc:
                request_id = path.stem
                err_path = self.spawn_dir / f"{request_id}.err"
                atomic_write_text(err_path, f"orchestrator internal error: {exc}\n")
                self.log_event("spawn_internal_error", request_id=request_id, error=str(exc))

    def process_request(self, request_path: Path) -> None:
        request_id = request_path.stem
        lock_path = self.spawn_dir / f"{request_id}.lock"
        out_path = self.spawn_dir / f"{request_id}.out"
        err_path = self.spawn_dir / f"{request_id}.err"
        try:
            payload = self.load_request(request_path)
            request_id = str(payload["id"])
            out_path = self.spawn_dir / f"{request_id}.out"
            err_path = self.spawn_dir / f"{request_id}.err"
            self.handle_payload(payload, out_path, err_path)
        except SpawnError as exc:
            atomic_write_text(err_path, exc.render())
            self.log_event("spawn_rejected", request_id=request_id, error=exc.message)
        finally:
            lock_path.unlink(missing_ok=True)

    def load_request(self, request_path: Path) -> dict[str, Any]:
        try:
            payload = json.loads(request_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise SpawnError(f"invalid spawn JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise SpawnError("spawn request must be a JSON object")
        if payload.get("protocol_version") != 1:
            raise SpawnError("unsupported spawn protocol_version")
        request_id = payload.get("id")
        if not isinstance(request_id, str) or not request_id:
            raise SpawnError("spawn id must be a non-empty string")
        if request_path.stem != request_id:
            raise SpawnError(
                f"spawn id {request_id!r} does not match filename {request_path.name!r}"
            )
        run_id = payload.get("run_id")
        if run_id and run_id != self.args.run_id:
            raise SpawnError(
                f"spawn run_id {run_id!r} does not match orchestrator {self.args.run_id!r}"
            )
        for field in ("query", "rel_context_path"):
            if not isinstance(payload.get(field), str) or not payload[field]:
                raise SpawnError(f"spawn field {field} must be a non-empty string")
        try:
            requested_depth = int(payload.get("requested_depth"))
        except (TypeError, ValueError) as exc:
            raise SpawnError("requested_depth must be an integer") from exc
        if requested_depth > self.args.max_depth:
            raise SpawnError(
                f"requested_depth={requested_depth} exceeds max_depth={self.args.max_depth}"
            )
        return payload

    def handle_payload(
        self,
        payload: dict[str, Any],
        out_path: Path,
        err_path: Path,
    ) -> None:
        request_id = str(payload["id"])
        requested_depth = int(payload["requested_depth"])
        child_root = self.run_dir / "spawns" / request_id
        child_context_dir = child_root / "context"
        child_manifest = child_root / "manifest.json"
        child_run_dir = self.run_dir / "children" / request_id
        child_root.mkdir(parents=True, exist_ok=True)
        child_run_dir.mkdir(parents=True, exist_ok=True)

        self.log_event(
            "spawn_started",
            request_id=request_id,
            requested_depth=requested_depth,
            rel_context_path=payload["rel_context_path"],
            parent_sandbox_id=payload.get("parent_sandbox_id", ""),
            parent_call_id=payload.get("parent_call_id", ""),
        )
        manifest = snapshot_context(
            parent_work_dir=self.parent_work_dir,
            rel_context_path=str(payload["rel_context_path"]),
            child_context_dir=child_context_dir,
            manifest_path=child_manifest,
            run_id=self.args.run_id,
            request_id=request_id,
        )

        cmd = self.child_loop_command(payload, child_context_dir, child_run_dir)
        started = time.monotonic()
        try:
            completed = subprocess.run(
                cmd,
                text=True,
                capture_output=True,
                timeout=self.args.child_timeout,
            )
        except subprocess.TimeoutExpired as exc:
            elapsed = round(time.monotonic() - started, 3)
            stdout = exc.stdout if isinstance(exc.stdout, str) else (exc.stdout or b"").decode(
                "utf-8",
                errors="replace",
            )
            stderr = exc.stderr if isinstance(exc.stderr, str) else (exc.stderr or b"").decode(
                "utf-8",
                errors="replace",
            )
            atomic_write_text(
                err_path,
                f"child rlm-sh timed out after {self.args.child_timeout:g}s\n"
                f"child_run_dir: {child_run_dir}\n\n"
                f"stdout:\n{stdout}\n"
                f"stderr:\n{stderr}\n",
            )
            atomic_write_json(
                child_root / "result.json",
                {
                    "request_id": request_id,
                    "run_id": self.args.run_id,
                    "requested_depth": requested_depth,
                    "child_run_dir": str(child_run_dir),
                    "child_context_dir": str(child_context_dir),
                    "manifest": str(child_manifest),
                    "returncode": 124,
                    "elapsed_seconds": elapsed,
                    "finished_at": utc_now(),
                    "source_sha256": manifest["source_hash"]["sha256"],
                    "snapshot_sha256": manifest["snapshot_hash"]["sha256"],
                },
            )
            self.log_event(
                "spawn_failed",
                request_id=request_id,
                returncode=124,
                elapsed_seconds=elapsed,
                child_run_dir=str(child_run_dir),
            )
            return
        elapsed = round(time.monotonic() - started, 3)
        result = {
            "request_id": request_id,
            "run_id": self.args.run_id,
            "requested_depth": requested_depth,
            "child_run_dir": str(child_run_dir),
            "child_context_dir": str(child_context_dir),
            "manifest": str(child_manifest),
            "returncode": completed.returncode,
            "elapsed_seconds": elapsed,
            "stdout_bytes": len(completed.stdout.encode("utf-8")),
            "stderr_bytes": len(completed.stderr.encode("utf-8")),
            "finished_at": utc_now(),
            "source_sha256": manifest["source_hash"]["sha256"],
            "snapshot_sha256": manifest["snapshot_hash"]["sha256"],
        }
        atomic_write_json(child_root / "result.json", result)

        if completed.returncode == 0:
            atomic_write_text(out_path, completed.stdout)
            if completed.stderr:
                atomic_write_text(child_root / "stderr.txt", completed.stderr)
            self.log_event(
                "spawn_finished",
                request_id=request_id,
                returncode=completed.returncode,
                elapsed_seconds=elapsed,
                child_run_dir=str(child_run_dir),
            )
        else:
            message = (
                f"child rlm-sh failed with exit status {completed.returncode}\n"
                f"child_run_dir: {child_run_dir}\n\n"
                "stdout:\n"
                f"{completed.stdout}\n"
                "stderr:\n"
                f"{completed.stderr}\n"
            )
            atomic_write_text(err_path, message)
            self.log_event(
                "spawn_failed",
                request_id=request_id,
                returncode=completed.returncode,
                elapsed_seconds=elapsed,
                child_run_dir=str(child_run_dir),
            )

    def child_loop_command(
        self,
        payload: dict[str, Any],
        child_context_dir: Path,
        child_run_dir: Path,
    ) -> list[str]:
        model = payload.get("model") or self.args.root_model
        requested_depth = int(payload["requested_depth"])
        parent_sandbox_id = str(payload.get("parent_sandbox_id") or "")
        cmd = [
            str(self.loop_shell),
            "--query",
            str(payload["query"]),
            "--context-dir",
            str(child_context_dir),
            "--run-dir",
            str(child_run_dir),
            "--run-id",
            self.args.run_id,
            "--image",
            self.args.image,
            "--root-model",
            str(model),
            "--max-iters",
            str(self.args.child_max_iters),
            "--max-root-calls",
            str(self.args.child_max_root_calls),
            "--exec-timeout",
            str(self.args.exec_timeout),
            "--truncate-chars",
            str(self.args.truncate_chars),
            "--api-key-env",
            self.args.api_key_env,
            "--system-prompt",
            self.args.system_prompt,
            "--depth",
            str(requested_depth),
            "--parent-sandbox-id",
            parent_sandbox_id,
            "--max-depth",
            str(self.args.max_depth),
            "--child-timeout",
            str(self.args.child_timeout),
            "--backend",
            self.args.backend,
        ]
        if self.args.allow_openai_key_fallback:
            cmd.append("--allow-openai-key-fallback")
        if self.args.read_only_root:
            cmd.append("--read-only-root")
        if self.args.skip_preflight:
            cmd.append("--skip-preflight")
        return cmd


class SpawnError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message

    def render(self) -> str:
        return f"rlm-sh orchestrator: {self.message}\n"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--parent-work-dir", required=True)
    parser.add_argument("--spawn-dir", default=None)
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--depth", type=int, required=True)
    parser.add_argument("--max-depth", type=int, default=2)
    parser.add_argument("--max-spawns", type=int, default=32)
    parser.add_argument("--max-parallel-children", type=int, default=2)
    parser.add_argument("--poll-interval", type=float, default=0.25)
    parser.add_argument("--child-timeout", type=float, default=900.0)
    parser.add_argument("--child-max-iters", type=int, default=8)
    parser.add_argument("--child-max-root-calls", type=int, default=12)
    parser.add_argument("--exec-timeout", type=float, default=30.0)
    parser.add_argument("--truncate-chars", type=int, default=12000)
    parser.add_argument("--image", default="rlm-sh-sandbox:dev")
    parser.add_argument("--backend", default="docker")
    parser.add_argument("--api-key-env", default="RLMSH_KEY")
    parser.add_argument("--allow-openai-key-fallback", action="store_true")
    parser.add_argument("--read-only-root", action="store_true")
    parser.add_argument("--skip-preflight", action="store_true")
    parser.add_argument("--root-model", default="gpt-5")
    parser.add_argument(
        "--system-prompt",
        default=str(PROJECT_ROOT / "conf" / "system_prompt.md"),
    )
    parser.add_argument("--loop-shell", default=str(SCRIPT_DIR / "loop_shell.sh"))
    parser.add_argument("--once", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.max_depth < args.depth:
        print(
            f"orchestrator.py: max_depth={args.max_depth} is below depth={args.depth}",
            file=sys.stderr,
        )
        return 2
    if args.max_parallel_children < 1:
        print("orchestrator.py: --max-parallel-children must be >= 1", file=sys.stderr)
        return 2
    orch = Orchestrator(args)
    if args.once:
        return orch.run_once()
    return orch.run_forever()


if __name__ == "__main__":
    raise SystemExit(main())
