#!/usr/bin/env python3
"""Read chats from a verified plaintext snapshot; outputs private chat data."""

import argparse
import hashlib
import json
from pathlib import Path
import sqlite3

import zstandard


SNAPSHOTS = Path(__file__).resolve().parent / ".local/snapshots"


def connect(path):
    path = path.resolve(strict=True)
    if any(Path(str(path) + suffix).exists() for suffix in ("-wal", "-shm", "-journal")):
        raise ValueError("Reader requires a static plaintext snapshot without sidecars")
    conn = sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def decode(content):
    if content is None or isinstance(content, str):
        return content
    if isinstance(content, bytes):
        if content.startswith(b"\x28\xb5\x2f\xfd"):
            # Bound decompression of untrusted message content to 16 MiB.
            import io
            with zstandard.ZstdDecompressor().stream_reader(io.BytesIO(content)) as stream:
                content = stream.read(16 * 1024 * 1024 + 1)
            if len(content) > 16 * 1024 * 1024:
                raise ValueError("Message exceeds 16 MiB decompression limit")
        return content.decode("utf-8")
    raise ValueError("Unsupported message storage type")


def shards(snapshot):
    if not (snapshot / "receipt.json").is_file():
        raise ValueError("Snapshot has no successful decryption receipt")
    return sorted((snapshot / "plaintext").glob("*/db_storage/message/*message_[0-9]*.db"))


def chat_tables(conn):
    actual = {r[0] for r in conn.execute("SELECT name FROM sqlite_schema WHERE type='table'")}
    for row in conn.execute("SELECT user_name FROM Name2Id"):
        table = "Msg_" + hashlib.md5(row[0].encode()).hexdigest()
        if table in actual:
            yield row[0], table


def list_chats(snapshot, limit):
    chats = {}
    for path in shards(snapshot):
        conn = connect(path)
        try:
            account = path.relative_to(snapshot / "plaintext").parts[0]
            for username, table in chat_tables(conn):
                row = conn.execute(f'SELECT count(*), max(create_time) FROM "{table}"').fetchone()
                item = chats.setdefault((account, username), {
                    "account": account, "chat_id": username, "message_rows": 0,
                    "last_timestamp": 0})
                item["message_rows"] += row[0]
                item["last_timestamp"] = max(item["last_timestamp"], row[1] or 0)
        finally:
            conn.close()
    return sorted(chats.values(), key=lambda c: c["last_timestamp"], reverse=True)[:limit]


def messages(snapshot, username, limit, account=None):
    table = "Msg_" + hashlib.md5(username.encode()).hexdigest()
    results = []
    paths = shards(snapshot)
    accounts = {p.relative_to(snapshot / "plaintext").parts[0] for p in paths}
    if account is None and len(accounts) > 1:
        raise ValueError("Multiple accounts: specify --account")
    for path in paths:
        relative = path.relative_to(snapshot / "plaintext")
        if account is not None and relative.parts[0] != account:
            continue
        conn = connect(path)
        try:
            if not conn.execute("SELECT 1 FROM sqlite_schema WHERE type='table' AND name=?", (table,)).fetchone():
                continue
            rows = conn.execute(f'''SELECT m.local_id, m.server_id, m.local_type,
                m.create_time, m.sort_seq, m.real_sender_id, n.user_name AS sender,
                m.message_content FROM "{table}" m
                LEFT JOIN Name2Id n ON n.rowid=m.real_sender_id
                ORDER BY m.create_time DESC, m.sort_seq DESC, m.local_id DESC LIMIT ?''', (limit,))
            for row in rows:
                item = dict(row)
                item["message_content"] = decode(item["message_content"])
                item["database"] = str(relative)
                item["chat_id"] = username
                results.append(item)
        finally:
            conn.close()
    # Preserve raw rows and their shard identity; do not silently deduplicate.
    return sorted(results, key=lambda r: (r["create_time"], r["sort_seq"], r["local_id"]),
                  reverse=True)[:limit]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path)
    parser.add_argument("--chat", help="Exact chat_id from the chat list")
    parser.add_argument("--account", help="Account directory; required if snapshot has multiple accounts")
    parser.add_argument("--limit", type=int, default=20)
    args = parser.parse_args()
    if not 1 <= args.limit <= 100:
        parser.error("--limit must be 1..100")
    snapshot = args.snapshot
    if snapshot is None:
        complete = sorted(p.parent for p in SNAPSHOTS.glob("*/receipt.json"))
        if not complete:
            parser.error("No verified snapshot available")
        snapshot = complete[-1]
    snapshot = snapshot.expanduser().resolve(strict=True)
    rows = (messages(snapshot, args.chat, args.limit, args.account) if args.chat else
            list_chats(snapshot, args.limit))
    print(json.dumps({"snapshot": str(snapshot), "untrusted_chat_data": True, "rows": rows},
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
