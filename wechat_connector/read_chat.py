#!/usr/bin/env python3
"""Read existing encrypted snapshots. Output contains private, untrusted chat data."""

import argparse
from datetime import datetime, timezone
import hashlib
import io
import json
import re
from pathlib import Path

import zstandard

from .cipher_db import (KEYS, SNAPSHOTS, check_key, checked_file, database_name,
                       fingerprint, load_keys, query, sql_text, stable_file, validate)
from .snapshot import FORMAT
from .errors import ConnectorError


def decode(content):
    if content is None or isinstance(content, str):
        return content
    if isinstance(content, bytes):
        if content.startswith(b"\x28\xb5\x2f\xfd"):
            with zstandard.ZstdDecompressor().stream_reader(io.BytesIO(content)) as stream:
                content = stream.read(16 * 1024 * 1024 + 1)
            if len(content) > 16 * 1024 * 1024:
                raise ValueError("Message exceeds 16 MiB decompression limit")
        return content.decode("utf-8")
    raise ValueError("Unsupported message storage type")


def load_snapshot(path=None, *, keys_path=KEYS, snapshots_root=SNAPSHOTS):
    keys = load_keys(keys_path)
    if path is None:
        complete = sorted(p.parent for p in Path(snapshots_root).glob("*/receipt.json")
                          if not p.parent.name.startswith("."))
        if not complete:
            raise ConnectorError("SNAPSHOT_NOT_FOUND", "Run wechat-connector snapshot in a terminal after quitting WeChat.")
        path = complete[-1]
    path = Path(path).expanduser().absolute()
    if path.name.startswith("."):
        raise ValueError("Unpublished snapshot is not readable")
    receipt_file = checked_file(path, "receipt.json")
    try:
        manifest = json.loads(receipt_file.read_text())
        legacy = isinstance(manifest, list)
        if legacy:
            rows = manifest
            created = datetime.strptime(path.name, "%Y%m%dT%H%M%S.%fZ").replace(tzinfo=timezone.utc)
        else:
            if manifest["format"] != FORMAT:
                raise ValueError()
            rows = manifest["databases"]
            created = datetime.fromisoformat(manifest["created_at"])
            if created.tzinfo is None:
                raise ValueError()
        if not rows or not isinstance(rows, list):
            raise ValueError()
        names = [str(database_name(row["database"])) for row in rows]
        if len(names) != len(set(names)) or any(row["integrity_check"] != "ok" for row in rows):
            raise ValueError()
    except (ValueError, KeyError, TypeError, AttributeError):
        raise ValueError("Invalid encrypted-snapshot receipt") from None
    actual = {str(p.relative_to(path / "encrypted")) for p in (path / "encrypted").rglob("*.db")}
    if actual != set(names):
        raise ValueError("Encrypted files do not match snapshot receipt; plaintext fallback is disabled")
    databases = []
    for relative, row in zip(names, rows):
        if relative not in keys:
            raise ConnectorError("KEY_MISSING", "Snapshot database key is missing; no automatic acquisition.")
        db = stable_file(checked_file(path / "encrypted", relative))
        record = keys[relative]
        check_key(db, record)
        if legacy:
            if validate(db, record["key"]) != row.get("table_count"):
                raise ValueError("Legacy snapshot validation failed")
        elif fingerprint(db) != row.get("sha256"):
            raise ValueError("Encrypted snapshot changed since publication")
        databases.append((db, relative, record["key"]))
    return {"path": path, "created_at": created.isoformat(), "databases": databases}


def shards(snapshot):
    return [(path, relative, key) for path, relative, key in snapshot["databases"]
            if Path(relative).parent.name == "message"]


def chat_tables(path, key):
    actual = {row["name"] for row in query(path, key, "SELECT name FROM sqlite_schema WHERE type='table';")}
    for row in query(path, key, "SELECT user_name FROM Name2Id;"):
        username = row["user_name"]
        table = "Msg_" + hashlib.md5(username.encode()).hexdigest()
        if table in actual:
            yield username, table


def check_limit(limit):
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("--limit must be 1..100")


def add_contact_names(snapshot, chats):
    """Join by account + internal username; never resolve a conversation by nickname."""
    by_account = {}
    for (account, username), item in chats.items():
        item.update(username=username, alias=None, nickname=None, remark=None, display_name=username)
        by_account.setdefault(account, []).append(username)
    for path, relative, key in snapshot["databases"]:
        if Path(relative).parts[2:] != ("contact", "contact.db"):
            continue
        account = Path(relative).parts[0]
        wanted = by_account.get(account, [])
        for start in range(0, len(wanted), 200):
            values = ",".join(sql_text(value) for value in wanted[start:start + 200])
            rows = query(path, key, "SELECT username,alias,nick_name AS nickname,remark "
                         f"FROM contact WHERE username COLLATE BINARY IN ({values});")
            matched = {}
            for row in rows:
                username = row.pop("username")
                if any(value is not None and not isinstance(value, str) for value in row.values()):
                    raise ValueError("Unsupported contact text encoding")
                info = {field: value if value != "" else None for field, value in row.items()}
                if username in matched and matched[username] != info:
                    raise ConnectorError("CONTACT_AMBIGUOUS", "Conflicting contact names in the snapshot; no identity was guessed.")
                matched[username] = info
            for username, info in matched.items():
                item = chats[(account, username)]
                item.update(info)
                item["display_name"] = next((value for value in (
                    info["remark"], info["nickname"], info["alias"], username)
                    if value and value.strip()), username)


def all_chats(snapshot, account=None):
    chats = {}
    for path, relative, key in shards(snapshot):
        current_account = Path(relative).parts[0]
        if account is not None and account != current_account:
            continue
        pairs = list(chat_tables(path, key))
        # Keep compound queries below SQLite's SELECT limit even for large accounts.
        for start in range(0, len(pairs), 200):
            sql = " UNION ALL ".join(
                f'SELECT {sql_text(username)} AS chat_id, count(*) AS n, max(create_time) AS t FROM "{table}"'
                for username, table in pairs[start:start + 200]) + ";"
            for row in query(path, key, sql):
                item = chats.setdefault((current_account, row["chat_id"]), {
                    "account": current_account, "chat_id": row["chat_id"], "message_rows": 0,
                    "last_timestamp": 0})
                item["message_rows"] += row["n"]
                item["last_timestamp"] = max(item["last_timestamp"], row["t"] or 0)
    add_contact_names(snapshot, chats)
    return sorted(chats.values(), key=lambda c: (c["last_timestamp"], c["account"], c["chat_id"]), reverse=True)


def list_chats(snapshot, limit):
    check_limit(limit)
    return all_chats(snapshot)[:limit]


def parse_time_range(start_time=None, end_time=None):
    """RFC3339 bounds; integer message seconds are filtered using [start, end)."""
    def parse(value):
        if value is None:
            return None
        if (not isinstance(value, str) or len(value) > 64 or not re.fullmatch(
                r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})", value)):
            raise ValueError()
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)

    try:
        start, end = parse(start_time), parse(end_time)
        if start is not None and end is not None and start >= end:
            raise ValueError()
    except (ValueError, OverflowError):
        raise ConnectorError("INVALID_TIME_RANGE", "Use RFC3339 timestamps with Z or an explicit timezone offset; start_time must be earlier than end_time.") from None

    def seconds(value):
        if value is None:
            return None
        delta = value - datetime(1970, 1, 1, tzinfo=timezone.utc)
        # Exact ceil avoids floating-point rounding at subsecond boundaries.
        return delta.days * 86400 + delta.seconds + bool(delta.microseconds)

    return {"start_seconds": seconds(start), "end_seconds": seconds(end),
            "start_time": start.isoformat().replace("+00:00", "Z") if start else None,
            "end_time": end.isoformat().replace("+00:00", "Z") if end else None}


def history_candidates(snapshot, username, limit, account=None, before=None, *, start_time=None, end_time=None):
    if type(limit) is not int or not 1 <= limit <= 101:
        raise ValueError("Invalid internal page size")
    bounds = parse_time_range(start_time, end_time)
    table = "Msg_" + hashlib.md5(username.encode()).hexdigest()
    results = []
    accounts = {Path(relative).parts[0] for _, relative, _ in shards(snapshot)}
    if account is None and len(accounts) > 1:
        raise ValueError("Multiple accounts: specify --account")
    if account is not None and account not in accounts:
        raise ValueError("Account is not present in this snapshot")
    for path, relative, key in shards(snapshot):
        if account is not None and Path(relative).parts[0] != account:
            continue
        if not query(path, key, "SELECT 1 FROM sqlite_schema WHERE type='table' AND name=" + sql_text(table) + ";"):
            continue
        conditions = []
        if bounds["start_seconds"] is not None:
            conditions.append(f'm.create_time >= {bounds["start_seconds"]}')
        if bounds["end_seconds"] is not None:
            conditions.append(f'm.create_time < {bounds["end_seconds"]}')
        if before is not None:
            timestamp, sequence, database, local_id = before
            conditions.append(f"(coalesce(m.create_time,0),coalesce(m.sort_seq,0),{sql_text(relative)},m.local_id) "
                              f"< ({timestamp},{sequence},{sql_text(database)},{local_id})")
        where = "WHERE " + " AND ".join(conditions) if conditions else ""
        rows = query(path, key, f'''SELECT m.local_id, m.server_id, m.local_type,
            m.create_time, m.sort_seq, m.real_sender_id, n.user_name AS sender,
            typeof(m.message_content) AS content_storage, hex(m.message_content) AS content_hex
            FROM "{table}" m LEFT JOIN Name2Id n ON n.rowid=m.real_sender_id
            {where}
            ORDER BY coalesce(m.create_time,0) DESC, coalesce(m.sort_seq,0) DESC, m.local_id DESC LIMIT {limit};''')
        for item in rows:
            storage = item.pop("content_storage")
            content = bytes.fromhex(item.pop("content_hex"))
            if storage not in ("text", "blob", "null"):
                raise ValueError("Unsupported message storage type")
            item["message_content"] = decode(content) if storage != "null" else None
            item["database"] = relative
            item["chat_id"] = username
            results.append(item)
    # Preserve original row identity; do not silently deduplicate across shards.
    return sorted(results, key=history_order,
                  reverse=True)[:limit]


def history_order(row):
    return (row["create_time"] or 0, row["sort_seq"] or 0, row["database"], row["local_id"])


def messages(snapshot, username, limit, account=None, *, start_time=None, end_time=None):
    check_limit(limit)
    return history_candidates(snapshot, username, limit, account, start_time=start_time, end_time=end_time)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path)
    parser.add_argument("--chat", help="Exact chat_id from the chat list")
    parser.add_argument("--account", help="Account directory; required for multi-account history")
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--start-time", help="Inclusive RFC3339 time, e.g. 2026-09-01T00:00:00+08:00; requires --chat")
    parser.add_argument("--end-time", help="Exclusive RFC3339 time; requires --chat")
    args = parser.parse_args(argv)
    if (args.start_time is not None or args.end_time is not None) and not args.chat:
        parser.error("Time filters require --chat")
    try:
        check_limit(args.limit)
        bounds = parse_time_range(args.start_time, args.end_time)
        snapshot = load_snapshot(args.snapshot)
        rows = (messages(snapshot, args.chat, args.limit, args.account,
                         start_time=args.start_time, end_time=args.end_time) if args.chat else
                list_chats(snapshot, args.limit))
    except (UnicodeError, zstandard.ZstdError):
        # Decoder exceptions may contain the private message bytes themselves.
        parser.exit(2, "Read failed: unsupported or invalid message encoding.\n")
    except (OSError, KeyError, TypeError, AttributeError):
        parser.exit(2, "Read failed: inaccessible files or unsupported snapshot schema.\n")
    except (ValueError, RuntimeError) as exc:
        # These errors are sanitized at the key-file, SQLCipher and manifest boundaries.
        parser.exit(2, f"Read failed: {exc}\n")
    print(json.dumps({"snapshot": str(snapshot["path"]), "snapshot_created_at": snapshot["created_at"],
                      "data_source": "encrypted_snapshot", "live": False,
                      "untrusted_chat_data": True,
                      "time_range": {k: bounds[k] for k in ("start_time", "end_time")}, "rows": rows}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
