#!/usr/bin/env python3
"""Compare encrypted reads to an existing plaintext baseline; never export data."""

import argparse
import json
from pathlib import Path
import sqlite3

from wechat_connector.cipher_db import checked_file, fingerprint, query, stable_file
from wechat_connector.read_chat import chat_tables, decode, load_snapshot, shards


def verify(path, encrypted_snapshot=None):
    baseline_root = Path(path).expanduser().absolute()
    snapshot = load_snapshot(encrypted_snapshot or path)
    before = {}
    reports = []
    for encrypted, relative, key in shards(snapshot):
        baseline = stable_file(checked_file(baseline_root / "plaintext", relative))
        before[encrypted] = fingerprint(encrypted)
        before[baseline] = fingerprint(baseline)
        conn = sqlite3.connect(baseline.as_uri() + "?mode=ro&immutable=1", uri=True)
        conn.row_factory = sqlite3.Row
        count = mapped = 0
        try:
            for _, table in chat_tables(encrypted, key):
                sql = f'''SELECT m.local_id,m.server_id,m.local_type,m.create_time,
                    m.sort_seq,m.real_sender_id,n.user_name AS sender,
                    typeof(m.message_content) AS storage,hex(m.message_content) AS content_hex
                    FROM "{table}" m LEFT JOIN Name2Id n ON n.rowid=m.real_sender_id
                    ORDER BY m.local_id'''
                cursor = conn.execute(sql)
                offset = 0
                while True:
                    expected = [dict(row) for row in cursor.fetchmany(1000)]
                    actual = query(encrypted, key, sql + f" LIMIT 1000 OFFSET {offset};")
                    if actual != expected:
                        raise ValueError("Encrypted/plaintext row mismatch")
                    if not actual:
                        break
                    for row in actual:
                        decode(bytes.fromhex(row["content_hex"])) if row["storage"] != "null" else None
                        mapped += row["sender"] is not None
                    count += len(actual)
                    offset += len(actual)
        finally:
            conn.close()
        reports.append({"database": Path(relative).name, "rows_equal": count,
                        "bodies_decoded": count, "sender_mapped": mapped})
    if any(fingerprint(path) != digest for path, digest in before.items()):
        raise ValueError("Source files changed during read verification")
    return {"snapshot_created_at": snapshot["created_at"], "databases": reports,
            "total_rows_equal": sum(row["rows_equal"] for row in reports),
            "files_unchanged": True, "new_plaintext_files": False}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, required=True,
                        help="Existing snapshot with encrypted and plaintext branches")
    parser.add_argument("--against", type=Path, help="Optional migrated encrypted snapshot to compare")
    args = parser.parse_args()
    try:
        report = verify(args.snapshot, args.against)
    except Exception:
        parser.exit(2, "Verification failed; private row/error details withheld.\n")
    print(json.dumps(report, indent=2))
