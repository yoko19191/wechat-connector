#!/usr/bin/env python3
"""Export authenticated chat database copies with SQLCipher. WeChat must be quit."""

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess

from capture_keys import APP, authenticates
from doctor import DEFAULT_ROOT


def require_quit():
    if subprocess.run(["pgrep", "-x", "WeChat"], capture_output=True).returncode == 0:
        raise RuntimeError("Quit both WeChat apps normally before snapshotting.")


def quote(value):
    return "'" + str(value).replace("'", "''") + "'"


def export(source: Path, output: Path, key: bytes):
    with source.open("rb") as stream:
        if not authenticates(stream.read(4096), key):
            raise ValueError("Source salt/key/page authentication mismatch")
    if output.exists():
        raise FileExistsError(output)
    sql = (f'.bail on\nPRAGMA key="x\'{key.hex()}\'";\n'
           'PRAGMA cipher_compatibility=4;\nPRAGMA cipher_integrity_check;\n'
           f"ATTACH DATABASE {quote(output)} AS plaintext KEY '';\n"
           "SELECT sqlcipher_export('plaintext');\nDETACH DATABASE plaintext;\n")
    result = subprocess.run(["sqlcipher", str(source)], input=sql,
                            text=True, capture_output=True, timeout=600)
    if result.returncode or result.stderr or result.stdout.strip() != "ok":
        # Do not echo CLI diagnostics: they can include SQL containing key material.
        raise RuntimeError("SQLCipher authentication/export failed; output is not accepted")
    conn = sqlite3.connect(output.as_uri() + "?mode=ro", uri=True)
    try:
        conn.execute("PRAGMA query_only=ON")
        if conn.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise RuntimeError("SQLite integrity_check failed")
        tables = conn.execute("SELECT name FROM sqlite_schema WHERE type='table'").fetchall()
    finally:
        conn.close()
    return len(tables)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    args = parser.parse_args()
    require_quit()
    os.umask(0o077)
    root = args.root.expanduser().resolve(strict=True)
    records = [json.loads(line) for line in (APP.parent / "captured-keys.jsonl").read_text().splitlines()]
    if not records:
        parser.error("No authenticated keys have been captured.")
    destination = APP.parent / "snapshots" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    destination.mkdir(parents=True, mode=0o700)
    receipt = []
    for record in records:
        require_quit()
        relative = Path(record["database"])
        source = (root / relative).resolve(strict=True)
        if relative.is_absolute() or ".." in relative.parts or not source.is_relative_to(root):
            raise ValueError("Database path is outside the configured root")
        # Captured files are metadata or text chat shards only.
        if source.parent.name not in {"contact", "session", "message"} or source.suffix != ".db":
            raise ValueError("Not a supported chat database path")
        key = bytes.fromhex(record["key"])
        encrypted = destination / "encrypted" / relative
        plaintext = destination / "plaintext" / relative
        encrypted.parent.mkdir(parents=True, exist_ok=True)
        plaintext.parent.mkdir(parents=True, exist_ok=True)
        originals = [source] + [Path(str(source) + suffix) for suffix in ("-wal", "-journal")
                                if Path(str(source) + suffix).exists()]
        stamps = [(p.stat().st_size, p.stat().st_mtime_ns) for p in originals]
        for path in originals:
            if path.is_symlink():
                raise ValueError("Database sidecar may not be a symlink")
            copy = encrypted.parent / path.name
            shutil.copyfile(path, copy)
        require_quit()
        if stamps != [(p.stat().st_size, p.stat().st_mtime_ns) for p in originals]:
            raise RuntimeError("Source changed during copy; snapshot is not accepted")
        table_count = export(encrypted, plaintext, key)
        receipt.append({"database": str(relative), "table_count": table_count,
                        "integrity_check": "ok", "source_sidecars": [p.suffix for p in originals[1:]]})
        print(f"Verified {source.name}: {table_count} tables", flush=True)
    (destination / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
    print(f"Verified snapshot: {destination}")


if __name__ == "__main__":
    main()
