#!/usr/bin/env python3
"""Manually create a verified encrypted snapshot using existing database keys."""

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

from .cipher_db import (KEYS, LOCAL, SNAPSHOTS, SIDECARS, check_key, checked_file,
                       database_name, fingerprint, load_keys, normalize_copy)
from .doctor import DEFAULT_ROOT
from .errors import ConnectorError

FORMAT = "sqlcipher-snapshot-v1"


def require_quit():
    try:
        result = subprocess.run(["pgrep", "-x", "WeChat|Weixin"],
                                capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        raise RuntimeError("Cannot establish that WeChat is closed") from None
    if result.returncode == 0:
        raise RuntimeError("Quit WeChat normally before refreshing; no app will be stopped automatically")
    if result.returncode != 1:
        raise RuntimeError("Cannot establish that WeChat is closed")


def source_state(root, accounts=None):
    """Enumerate the full in-scope file set; errors must not become missing files."""
    databases = []
    state = {}
    if root.is_symlink() or root.resolve() != root:
        raise ValueError("Source root may not contain symlinks")
    seen = set()
    for account in sorted(root.iterdir()):
        if accounts is not None and account.name not in accounts:
            continue
        if not account.is_dir():
            continue
        storage = account / "db_storage"
        try:
            storage.stat()
        except FileNotFoundError:
            continue
        seen.add(account.name)
        paths = [storage / "contact/contact.db", storage / "session/session.db"]
        paths += [p for p in sorted((storage / "message").iterdir())
                  if re.fullmatch(r"(?:biz_)?message_[0-9]+\.db", p.name)]
        for path in paths:
            relative = str(path.relative_to(root))
            database_name(relative)
            checked_file(root, relative)
            databases.append(relative)
            for candidate in [path] + [Path(str(path) + s) for s in SIDECARS
                                       if os.path.lexists(str(path) + s)]:
                rel = str(candidate.relative_to(root))
                info = checked_file(root, rel).stat()
                state[rel] = (info.st_dev, info.st_ino, info.st_size,
                              info.st_mtime_ns, info.st_ctime_ns)
    if accounts is not None and seen != set(accounts):
        raise ValueError("Selected account data is unavailable")
    if not databases:
        raise ValueError("No chat databases found")
    return sorted(databases), state


def create_snapshot(root=DEFAULT_ROOT, *, destination_root=SNAPSHOTS, keys_path=KEYS, account=None):
    root = Path(root).expanduser().absolute()
    destination_root = Path(destination_root).expanduser().absolute()
    if (destination_root == root or root in destination_root.parents
            or destination_root.resolve() != destination_root):
        raise ValueError("Snapshot destination must be separate from source and contain no symlinks")
    require_quit()
    keys = load_keys(keys_path)
    accounts = {Path(name).parts[0] for name in keys}
    if account is not None:
        if account not in accounts:
            raise ConnectorError("KEY_MISSING", "No keys for the requested account.")
        accounts = {account}
    databases, before = source_state(root, accounts)
    missing = [Path(name).name for name in databases if name not in keys]
    if missing:
        raise ConnectorError("KEY_MISSING", "Missing stored keys: " + ", ".join(missing) + "; refresh stopped")
    for relative in databases:
        check_key(checked_file(root, relative), keys[relative])
    created = datetime.now(timezone.utc)
    destination = destination_root / created.strftime("%Y%m%dT%H%M%S.%fZ")
    old_mask = os.umask(0o077)
    staging = None
    try:
        if destination_root == SNAPSHOTS:
            LOCAL.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(LOCAL, 0o700)
        destination_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(destination_root, 0o700)
        staging = Path(tempfile.mkdtemp(prefix=".staging-", dir=destination_root))
        for relative in before:
            if relative.endswith("-shm"):
                continue  # Rebuild SQLite's shared-memory index on the private copy.
            source = checked_file(root, relative)
            target = staging / "encrypted" / relative
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            shutil.copyfile(source, target)
        require_quit()
        if source_state(root, accounts) != (databases, before):
            raise RuntimeError("Source file set or metadata changed during copying; snapshot rejected")
        receipt = []
        for relative in databases:
            path = staging / "encrypted" / relative
            check_key(path, keys[relative])
            count = normalize_copy(path, keys[relative]["key"])
            receipt.append({"database": relative, "table_count": count,
                            "integrity_check": "ok", "sha256": fingerprint(path)})
        # Do not publish if the app restarted or source changed during validation.
        require_quit()
        if source_state(root, accounts) != (databases, before):
            raise RuntimeError("Source changed before publication; snapshot rejected")
        manifest = {"format": FORMAT, "created_at": created.isoformat(), "databases": receipt}
        (staging / "receipt.json").write_text(json.dumps(manifest, indent=2) + "\n")
        if destination.exists():
            raise FileExistsError("Snapshot destination already exists")
        staging.rename(destination)
        staging = None
        return destination
    finally:
        if staging is not None:
            shutil.rmtree(staging)  # Only this invocation's unpublished private copies.
        os.umask(old_mask)


def cli(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT,
                        help="xwechat_files root; WeChat must be closed")
    parser.add_argument("--account")
    args = parser.parse_args(argv)
    try:
        result = create_snapshot(args.root, account=args.account)
    except (OSError, ValueError, RuntimeError) as exc:
        parser.exit(2, f"Snapshot not published: {exc}\n")
    print(json.dumps({"snapshot": str(result), "encrypted": True}, ensure_ascii=False))


if __name__ == "__main__":
    cli()
