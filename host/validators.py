#!/usr/bin/env python3
"""Validation and snapshot helpers for rlm-sh host orchestration."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import sys
from typing import Any
from uuid import uuid4


HASH_VERSION = "rlm-sh-tree-v1"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}-{uuid4().hex}")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    atomic_write_text(
        path,
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )


def validate_relative_work_path(value: str) -> PurePosixPath:
    if not value:
        raise ValueError("context path must not be empty")
    path = PurePosixPath(value)
    if path.is_absolute():
        raise ValueError("context path must be relative to /work")
    if any(part == ".." for part in path.parts):
        raise ValueError("context path must not contain '..'")
    if str(path) in ("", "."):
        raise ValueError("context path must not be empty")
    return path


def contained_path(root: Path, relative_path: str) -> Path:
    rel = validate_relative_work_path(relative_path)
    root_real = root.resolve()
    candidate = (root_real / rel.as_posix()).resolve()
    if candidate != root_real and root_real not in candidate.parents:
        raise ValueError(
            f"context path escapes /work: {relative_path!r} -> {candidate}"
        )
    if not candidate.exists():
        raise ValueError(f"context path does not exist under /work: {relative_path}")
    return candidate


def reject_symlinks(path: Path) -> None:
    if path.is_symlink():
        raise ValueError(f"refusing to snapshot symlink: {path}")
    if path.is_dir():
        for child in path.rglob("*"):
            if child.is_symlink():
                raise ValueError(f"refusing to snapshot tree containing symlink: {child}")


def _hash_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    total = 0
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            total += len(chunk)
            digest.update(chunk)
    return digest.hexdigest(), total


def hash_path(path: Path) -> dict[str, Any]:
    """Return a deterministic hash manifest for a file or directory tree."""
    path = path.resolve()
    reject_symlinks(path)
    if path.is_file():
        digest, total = _hash_file(path)
        return {
            "version": HASH_VERSION,
            "kind": "file",
            "path": str(path),
            "sha256": digest,
            "total_bytes": total,
            "file_count": 1,
            "entries": [
                {
                    "path": path.name,
                    "sha256": digest,
                    "bytes": total,
                }
            ],
            "created_at": utc_now(),
        }
    if not path.is_dir():
        raise ValueError(f"path is neither file nor directory: {path}")

    digest = hashlib.sha256()
    digest.update(HASH_VERSION.encode("utf-8"))
    digest.update(b"\0dir\0")
    entries: list[dict[str, Any]] = []
    total_bytes = 0
    for child in sorted(p for p in path.rglob("*") if p.is_file()):
        rel = child.relative_to(path).as_posix()
        file_hash, file_bytes = _hash_file(child)
        digest.update(rel.encode("utf-8"))
        digest.update(b"\0")
        digest.update(file_hash.encode("ascii"))
        digest.update(b"\0")
        total_bytes += file_bytes
        entries.append({"path": rel, "sha256": file_hash, "bytes": file_bytes})
    return {
        "version": HASH_VERSION,
        "kind": "directory",
        "path": str(path),
        "sha256": digest.hexdigest(),
        "total_bytes": total_bytes,
        "file_count": len(entries),
        "entries": entries,
        "created_at": utc_now(),
    }


def snapshot_context(
    *,
    parent_work_dir: Path,
    rel_context_path: str,
    child_context_dir: Path,
    manifest_path: Path,
    run_id: str,
    request_id: str,
) -> dict[str, Any]:
    source = contained_path(parent_work_dir, rel_context_path)
    reject_symlinks(source)
    source_hash = hash_path(source)

    if child_context_dir.exists():
        raise ValueError(f"child context directory already exists: {child_context_dir}")
    child_context_dir.mkdir(parents=True)

    if source.is_file():
        snapshot_root = child_context_dir / source.name
        shutil.copy2(source, snapshot_root)
    elif source.is_dir():
        snapshot_root = child_context_dir
        shutil.copytree(source, child_context_dir, dirs_exist_ok=True)
    else:
        raise ValueError(f"unsupported context source: {source}")

    snapshot_hash = hash_path(snapshot_root)
    if source_hash["sha256"] != snapshot_hash["sha256"]:
        raise ValueError(
            "snapshot hash mismatch: source="
            f"{source_hash['sha256']} snapshot={snapshot_hash['sha256']}"
        )

    manifest: dict[str, Any] = {
        "version": 1,
        "run_id": run_id,
        "request_id": request_id,
        "created_at": utc_now(),
        "parent_work_dir": str(parent_work_dir.resolve()),
        "rel_context_path": rel_context_path,
        "source_path": str(source),
        "child_context_dir": str(child_context_dir.resolve()),
        "child_context_entry": snapshot_root.name if source.is_file() else ".",
        "source_hash": source_hash,
        "snapshot_hash": snapshot_hash,
    }
    atomic_write_json(manifest_path, manifest)
    return manifest


def compare_hashes(expected: dict[str, Any], actual: dict[str, Any]) -> dict[str, Any]:
    ok = expected.get("sha256") == actual.get("sha256")
    return {
        "ok": ok,
        "expected_sha256": expected.get("sha256"),
        "actual_sha256": actual.get("sha256"),
        "expected_file_count": expected.get("file_count"),
        "actual_file_count": actual.get("file_count"),
        "expected_total_bytes": expected.get("total_bytes"),
        "actual_total_bytes": actual.get("total_bytes"),
        "checked_at": utc_now(),
    }


def cmd_hash_tree(args: argparse.Namespace) -> int:
    result = hash_path(Path(args.path))
    text = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.out:
        atomic_write_text(Path(args.out), text)
    else:
        sys.stdout.write(text)
    return 0


def cmd_validate_tree(args: argparse.Namespace) -> int:
    expected = json.loads(Path(args.expected).read_text(encoding="utf-8"))
    actual = hash_path(Path(args.path))
    result = compare_hashes(expected, actual)
    result["actual"] = actual
    text = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.out:
        atomic_write_text(Path(args.out), text)
    else:
        sys.stdout.write(text)
    return 0 if result["ok"] else 1


def cmd_snapshot(args: argparse.Namespace) -> int:
    manifest = snapshot_context(
        parent_work_dir=Path(args.parent_work_dir),
        rel_context_path=args.rel_context_path,
        child_context_dir=Path(args.child_context_dir),
        manifest_path=Path(args.manifest),
        run_id=args.run_id,
        request_id=args.request_id,
    )
    print(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    hash_tree = subparsers.add_parser("hash-tree")
    hash_tree.add_argument("path")
    hash_tree.add_argument("--out", default=None)
    hash_tree.set_defaults(func=cmd_hash_tree)

    validate_tree = subparsers.add_parser("validate-tree")
    validate_tree.add_argument("path")
    validate_tree.add_argument("--expected", required=True)
    validate_tree.add_argument("--out", default=None)
    validate_tree.set_defaults(func=cmd_validate_tree)

    snapshot = subparsers.add_parser("snapshot")
    snapshot.add_argument("--parent-work-dir", required=True)
    snapshot.add_argument("--rel-context-path", required=True)
    snapshot.add_argument("--child-context-dir", required=True)
    snapshot.add_argument("--manifest", required=True)
    snapshot.add_argument("--run-id", required=True)
    snapshot.add_argument("--request-id", required=True)
    snapshot.set_defaults(func=cmd_snapshot)

    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        return args.func(args)
    except ValueError as exc:
        print(f"validators.py: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
