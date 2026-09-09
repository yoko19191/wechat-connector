"""Run with .venv/bin/python test_read_chat.py; no real chat data required."""

import hashlib
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory

import zstandard

from read_chat import connect, decode, list_chats, messages


with TemporaryDirectory() as directory:
    snapshot = Path(directory)
    (snapshot / "receipt.json").write_text("[]")
    table = "Msg_" + hashlib.md5(b"test-chat").hexdigest()
    for shard, timestamp in [(0, 10), (1, 20)]:
        db = snapshot / f"plaintext/test-account/db_storage/message/message_{shard}.db"
        db.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(db) as conn:
            conn.execute("CREATE TABLE Name2Id(user_name TEXT PRIMARY KEY)")
            conn.executemany("INSERT INTO Name2Id VALUES (?)", [("test-chat",), ("test-sender",)])
            conn.execute(f'''CREATE TABLE "{table}"(local_id INTEGER, server_id INTEGER,
                local_type INTEGER, create_time INTEGER, sort_seq INTEGER,
                real_sender_id INTEGER, message_content TEXT)''')
            content = "synthetic 中文" if shard == 0 else zstandard.ZstdCompressor().compress(b"synthetic newer")
            conn.execute(f'INSERT INTO "{table}" VALUES (1,2,1,?,1,2,?)', (timestamp, content))
    chats = list_chats(snapshot, 10)
    assert len(chats) == 1 and chats[0]["message_rows"] == 2
    rows = messages(snapshot, "test-chat", 1)
    assert len(rows) == 1 and rows[0]["message_content"] == "synthetic newer"
    assert rows[0]["sender"] == "test-sender"
    assert len(messages(snapshot, "test-chat", 10)) == 2
    assert messages(snapshot, "absent", 10) == []
    conn = connect(db)
    try:
        conn.execute("DELETE FROM Name2Id")
    except sqlite3.OperationalError:
        pass
    else:
        raise AssertionError("Reader must reject writes")
    finally:
        conn.close()
    Path(str(db) + "-wal").touch()
    try:
        connect(db)
    except ValueError:
        pass
    else:
        raise AssertionError("Reader must reject live sidecars")
    try:
        decode(b"\xff")
    except UnicodeDecodeError:
        pass
    else:
        raise AssertionError("Invalid text must not silently become empty content")
print("PASS: shard merge, limit, sender join, Zstandard, readonly, sidecar and decode errors")
