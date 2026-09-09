"""Run with python3 -m test.test_doctor; synthetic files only."""

from pathlib import Path
from tempfile import TemporaryDirectory

from wechat_connector.doctor import inventory, SQLITE_HEADER


with TemporaryDirectory() as directory:
    root = Path(directory)
    db = root / "test-account/db_storage/message/message_0.db"
    db.parent.mkdir(parents=True)
    db.write_bytes(bytes(range(16)) + bytes(4080))
    wal = Path(str(db) + "-wal")
    wal.write_bytes(b"synthetic-wal")
    before = db.read_bytes(), db.stat().st_mtime_ns, wal.read_bytes()
    report = inventory(root)
    assert report["status"] == "found" and report["database_count"] == 1
    row = report["databases"][0]
    assert row["header"] == "non-sqlite-possibly-encrypted"
    assert row["sidecar_bytes"]["-wal"] == 13
    assert before == (db.read_bytes(), db.stat().st_mtime_ns, wal.read_bytes())
    db.write_bytes(SQLITE_HEADER + bytes(4080))
    assert inventory(root)["databases"][0]["header"] == "sqlite"
    db.write_bytes(b"")
    assert inventory(root)["databases"][0]["header"] == "short-or-empty"
    db.with_name("linked.db").symlink_to(db)
    assert inventory(root)["database_count"] == 1
    missing = inventory(root / "missing")
    assert missing["status"] == "blocked-or-incomplete" and missing["errors"]
print("PASS: inventory classification, sidecars, no source writes, links and errors")
