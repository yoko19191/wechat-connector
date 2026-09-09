#!/usr/bin/env python3
"""Read-only WeChat database inventory. Never reads messages or prints keys."""

import argparse
import json
import os
from pathlib import Path
import platform
import plistlib
import subprocess


DEFAULT_ROOT = (Path.home() / "Library/Containers/com.tencent.xinWeChat"
                / "Data/Documents/xwechat_files")
SQLITE_HEADER = b"SQLite format 3\x00"


def inventory(root: Path) -> dict:
    result = {"root": str(root), "databases": [], "errors": []}

    def record_error(exc):
        result["errors"].append({"path": str(exc.filename),
                                 "errno": exc.errno, "message": exc.strerror})

    try:
        accounts = sorted(root.iterdir())
    except OSError as exc:
        record_error(exc)
        accounts = []
    for account in accounts:
        if account.is_symlink():
            continue
        try:
            if not account.is_dir():
                continue
            storage = account / "db_storage"
            if storage.is_symlink():
                continue
            storage.stat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            record_error(exc)
            continue
        for directory, dirs, files in os.walk(storage, onerror=record_error,
                                               followlinks=False):
            dirs[:] = sorted(d for d in dirs if not (Path(directory) / d).is_symlink())
            for name in sorted(files):
                db = Path(directory) / name
                if db.suffix != ".db" or db.is_symlink():
                    continue
                try:
                    before = db.stat()
                    with db.open("rb") as stream:
                        header = stream.read(16)
                    after = db.stat()
                    sidecars = {}
                    for suffix in ("-wal", "-shm", "-journal"):
                        sidecar = Path(str(db) + suffix)
                        if not sidecar.is_symlink() and sidecar.exists():
                            sidecars[suffix] = sidecar.stat().st_size
                    result["databases"].append({
                        "path": str(db.relative_to(root)),
                        "bytes": after.st_size,
                        "header": ("sqlite" if header == SQLITE_HEADER else
                                   "short-or-empty" if len(header) < 16 else
                                   "non-sqlite-possibly-encrypted"),
                        "sidecar_bytes": sidecars,
                        "changed_during_probe": (
                            before.st_size, before.st_mtime_ns) != (
                            after.st_size, after.st_mtime_ns),
                    })
                except OSError as exc:
                    record_error(exc)
    result["status"] = ("blocked-or-incomplete" if result["errors"] else
                        "found" if result["databases"] else "no-databases-found")
    result["database_count"] = len(result["databases"])
    result["total_bytes"] = sum(d["bytes"] for d in result["databases"])
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT,
                        help="xwechat_files root (not a single db_storage directory)")
    args = parser.parse_args(argv)
    result = {"platform": platform.system(), "architecture": platform.machine()}
    info = Path("/Applications/WeChat.app/Contents/Info.plist")
    try:
        with info.open("rb") as stream:
            metadata = plistlib.load(stream)
        result["wechat"] = {key: metadata.get(key) for key in (
            "CFBundleIdentifier", "CFBundleShortVersionString", "CFBundleVersion")}
    except (OSError, plistlib.InvalidFileException) as exc:
        result["app_info_error"] = str(exc)
    if platform.system() == "Darwin":
        probe = subprocess.run(["/usr/bin/csrutil", "status"], capture_output=True,
                               text=True, timeout=10)
        result["sip"] = (probe.stdout + probe.stderr).strip()
    result["inventory"] = inventory(args.root.expanduser().absolute())
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["inventory"]["status"] == "found" else 2


if __name__ == "__main__":
    raise SystemExit(main())
