#!/usr/bin/env python3
"""Non-destructive backup and restore helper for Amphi Studio.

Backups are timestamped directory snapshots. Restore never overwrites the live
DATA_DIR: it writes a separate recovery directory, allowing an operator to
inspect or atomically swap it after validation.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path


def _digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def backup(data_dir: Path, backup_dir: Path) -> Path:
    data_dir = data_dir.resolve()
    backup_dir = backup_dir.resolve()
    if not data_dir.is_dir():
        raise FileNotFoundError(f"data directory not found: {data_dir}")
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    destination = backup_dir / stamp
    suffix = 1
    while destination.exists():
        destination = backup_dir / f"{stamp}-{suffix}"
        suffix += 1
    temporary = Path(tempfile.mkdtemp(prefix=f".{destination.name}-", dir=backup_dir))
    try:
        shutil.copytree(data_dir, temporary / "data", symlinks=False)
        files = {}
        for path in sorted((temporary / "data").rglob("*")):
            if path.is_file():
                files[str(path.relative_to(temporary / "data"))] = _digest(path)
        (temporary / "manifest.json").write_text(json.dumps({
            "format": 1, "createdAt": datetime.now(timezone.utc).isoformat(),
            "files": files,
        }, indent=2, sort_keys=True), encoding="utf-8")
        temporary.replace(destination)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return destination


def restore(backup_path: Path, output_dir: Path) -> Path:
    backup_path = backup_path.resolve()
    source = backup_path / "data"
    manifest_path = backup_path / "manifest.json"
    if not source.is_dir() or not manifest_path.is_file():
        raise ValueError("invalid Amphi backup (data/ and manifest.json required)")
    try:
        manifest = json.loads(manifest_path.read_text("utf-8"))
        files = manifest["files"]
        if manifest.get("format") != 1 or not isinstance(files, dict):
            raise ValueError
        expected = {str(path.relative_to(source)): _digest(path)
                    for path in sorted(source.rglob("*")) if path.is_file()}
        if files != expected:
            raise ValueError("backup manifest does not match data")
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise ValueError("invalid or corrupted Amphi backup") from exc
    output_dir = output_dir.resolve()
    if output_dir.exists():
        raise FileExistsError(f"recovery output already exists: {output_dir}")
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{output_dir.name}-", dir=output_dir.parent))
    try:
        shutil.copytree(source, temporary, dirs_exist_ok=True, symlinks=False)
        temporary.replace(output_dir)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return output_dir


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    b = sub.add_parser("backup")
    b.add_argument("--data", default=os.environ.get("AMPHI_DATA_DIR", "data/studio"), type=Path)
    b.add_argument("--destination", default=os.environ.get("AMPHI_BACKUP_DIR", str(Path.home() / "Backups" / "amphi")), type=Path)
    r = sub.add_parser("restore")
    r.add_argument("backup", type=Path)
    r.add_argument("output", type=Path)
    args = parser.parse_args()
    result = backup(args.data, args.destination) if args.command == "backup" else restore(args.backup, args.output)
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
