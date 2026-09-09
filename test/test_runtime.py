"""Run: .venv/bin/python -m unittest -v test.test_runtime (synthetic encrypted data only)."""

import ctypes
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import zstandard

from wechat_connector import cipher_db
from wechat_connector.cipher_db import (authenticates, check_key, fingerprint, load_keys, normalize_copy,
                       query, sql_text)
from wechat_connector import snapshot as producer
from wechat_connector import read_chat
from wechat_connector.errors import ConnectorError


class FixtureDB:
    """Native SQLCipher is used only to hold a synthetic WAL writer open in tests."""
    lib = ctypes.CDLL(str(Path(shutil.which("sqlcipher")).resolve().parent.parent / "lib/libsqlcipher.dylib"))
    lib.sqlite3_open.argtypes = [ctypes.c_char_p, ctypes.POINTER(ctypes.c_void_p)]
    lib.sqlite3_exec.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_void_p,
                               ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
    lib.sqlite3_close.argtypes = [ctypes.c_void_p]
    lib.sqlite3_free.argtypes = [ctypes.c_void_p]

    def __init__(self, path, key):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = ctypes.c_void_p()
        if self.lib.sqlite3_open(str(path).encode(), ctypes.byref(self.handle)):
            raise RuntimeError("Synthetic database open failed")
        self.execute(f'PRAGMA key="x\'{key.hex()}\'"; PRAGMA cipher_compatibility=4;')

    def execute(self, sql):
        error = ctypes.c_void_p()
        code = self.lib.sqlite3_exec(self.handle, sql.encode(), None, None, ctypes.byref(error))
        if error:
            self.lib.sqlite3_free(error)
        if code:
            raise RuntimeError("Synthetic fixture SQL failed")

    def close(self):
        if self.lib.sqlite3_close(self.handle):
            raise RuntimeError("Synthetic database close failed")


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.base = Path(self.temp.name).resolve()
        self.source = self.base / "source"
        self.output = self.base / "snapshots"
        self.keyfile = self.base / "keys.jsonl"
        self.table = "Msg_" + hashlib.md5(b"test-chat").hexdigest()
        self.records = []
        for relative, timestamp in [("contact/contact.db", None), ("session/session.db", None),
                                    ("message/message_0.db", 10), ("message/message_1.db", 20)]:
            relative = "test-account/db_storage/" + relative
            key = hashlib.sha256(relative.encode()).digest()
            path = self.source / relative
            db = FixtureDB(path, key)
            if "/contact/" in relative:
                db.execute("CREATE TABLE contact(username TEXT,nick_name TEXT,alias TEXT,remark TEXT);")
            elif timestamp is None:
                db.execute("CREATE TABLE metadata(id INTEGER PRIMARY KEY);")
            else:
                content = ("CAST(X'" + "synthetic 中文".encode().hex() + "' AS TEXT)" if timestamp == 10 else
                           "X'" + zstandard.ZstdCompressor().compress(b"synthetic newer").hex() + "'")
                db.execute(f'''CREATE TABLE Name2Id(user_name TEXT PRIMARY KEY);
                    INSERT INTO Name2Id VALUES ('test-chat'),('test-sender');
                    CREATE TABLE "{self.table}"(local_id INTEGER, server_id INTEGER,
                    local_type INTEGER, create_time INTEGER, sort_seq INTEGER,
                    real_sender_id INTEGER, message_content TEXT);
                    INSERT INTO "{self.table}" VALUES(1,2,1,{timestamp},1,2,{content});''')
            db.close()
            self.records.append({"database": relative, "key": key.hex(), "salt": path.read_bytes()[:16].hex()})
        self.save_keys()

    def tearDown(self):
        self.temp.cleanup()

    def save_keys(self):
        self.keyfile.write_text("".join(json.dumps(row) + "\n" for row in self.records))
        self.keyfile.chmod(0o600)

    def refresh(self):
        with patch.object(producer, "require_quit"):
            return producer.create_snapshot(self.source, destination_root=self.output, keys_path=self.keyfile)

    def load(self, path):
        return read_chat.load_snapshot(path, keys_path=self.keyfile)

    def snapshot_count(self):
        return len(list(self.output.glob("*/receipt.json")))

    def test_refresh_readonly_parity_and_legacy_compatibility(self):
        before = producer.source_state(self.source)
        path = self.refresh()
        self.assertEqual(before, producer.source_state(self.source))
        loaded = self.load(path)
        self.assertTrue(loaded["created_at"])
        self.assertEqual(read_chat.list_chats(loaded, 10)[0]["message_rows"], 2)
        rows = read_chat.messages(loaded, "test-chat", 1)
        self.assertEqual(rows[0]["message_content"], "synthetic newer")
        self.assertEqual(rows[0]["sender"], "test-sender")
        self.assertEqual(len(read_chat.messages(loaded, "test-chat", 10)), 2)
        self.assertEqual(read_chat.messages(loaded, "missing", 10), [])
        for limit in (0, 101):
            with self.assertRaises(ValueError):
                read_chat.messages(loaded, "test-chat", limit)
        db, _, key = read_chat.shards(loaded)[0]
        before_hash = fingerprint(db)
        with self.assertRaises(ConnectorError):
            query(db, key, "DELETE FROM Name2Id;")
        self.assertEqual(before_hash, fingerprint(db))
        self.assertFalse(any(path.rglob("*-wal")))
        self.assertFalse((path / "plaintext").exists())
        for file in path.rglob("*"):
            self.assertEqual(file.stat().st_mode & 0o777, 0o700 if file.is_dir() else 0o600)
        # Legacy receipt remains supported; reader never opens the plaintext branch.
        manifest = json.loads((path / "receipt.json").read_text())
        (path / "receipt.json").write_text(json.dumps(manifest["databases"]))
        (path / "plaintext").mkdir()
        (path / "plaintext/unused.db").write_bytes(b"invalid plaintext")
        self.assertEqual(len(read_chat.messages(self.load(path), "test-chat", 10)), 2)
        Path(str(db) + "-wal").touch()
        with self.assertRaises(ValueError):
            self.load(path)

    def test_committed_wal_is_included_without_source_writes(self):
        record = self.records[-1]
        source = self.source / record["database"]
        writer = FixtureDB(source, bytes.fromhex(record["key"]))
        try:
            writer.execute(f'''PRAGMA journal_mode=WAL; PRAGMA wal_autocheckpoint=0;
                INSERT INTO "{self.table}" VALUES(2,3,1,30,2,2,'committed WAL message');''')
            self.assertGreater(Path(str(source) + "-wal").stat().st_size, 32)
            original_hashes = {p: fingerprint(p) for p in (source, Path(str(source) + "-wal"))}
            path = self.refresh()
            rows = read_chat.messages(self.load(path), "test-chat", 10)
            self.assertEqual(len(rows), 3)
            self.assertEqual(rows[0]["message_content"], "committed WAL message")
            self.assertEqual(original_hashes, {p: fingerprint(p) for p in original_hashes})
            self.assertFalse(any((path / "encrypted").rglob("*-wal")))
        finally:
            writer.close()

    def test_missing_wrong_keys_and_corruption_fail_closed(self):
        previous = self.refresh()
        last = self.records.pop()
        self.save_keys()
        with self.assertRaisesRegex(ValueError, "Missing stored keys"):
            self.refresh()
        self.assertEqual(self.snapshot_count(), 1)
        self.assertTrue(previous.is_dir())
        self.records.append(last)
        self.records[0]["key"] = bytes(32).hex()
        self.save_keys()
        with self.assertRaises(ValueError):
            self.refresh()
        self.assertEqual(self.snapshot_count(), 1)
        with self.assertRaises(ValueError):
            check_key(self.source / self.records[0]["database"], load_keys(self.keyfile)[self.records[0]["database"]])
        path = previous / "encrypted" / self.records[0]["database"]
        data = bytearray(path.read_bytes())
        data[-1] ^= 1
        path.write_bytes(data)
        with self.assertRaises(ValueError):
            self.load(previous)

    def test_first_page_and_later_page_authentication(self):
        record = self.records[-1]
        key = bytes.fromhex(record["key"])
        path = self.source / record["database"]
        data = bytearray(path.read_bytes())
        self.assertTrue(authenticates(data[:4096], key))
        self.assertFalse(authenticates(data[:4096], bytes(32)))
        data[123] ^= 1
        self.assertFalse(authenticates(data[:4096], key))
        # A later page corruption must also fail validation even if page 1 passes.
        data = bytearray(path.read_bytes())
        self.assertGreater(len(data), 4096)
        data[-30] ^= 1
        path.write_bytes(data)
        with self.assertRaises((ValueError, RuntimeError)):
            self.refresh()
        self.assertEqual(self.snapshot_count(), 0)

    def test_source_change_or_new_file_during_copy_rejects_publication(self):
        copy = producer.shutil.copyfile
        changed = False
        def copy_then_change(src, dst):
            nonlocal changed
            result = copy(src, dst)
            if not changed:
                changed = True
                (self.source / "test-account/db_storage/message/message_2.db").write_bytes(b"new file")
            return result
        with patch.object(producer.shutil, "copyfile", side_effect=copy_then_change):
            with self.assertRaisesRegex(RuntimeError, "changed"):
                self.refresh()
        self.assertEqual(self.snapshot_count(), 0)
        self.assertFalse(list(self.output.glob(".staging-*")))

    def test_app_restart_or_interruption_preserves_previous_snapshot(self):
        previous = self.refresh()
        with patch.object(producer, "require_quit", side_effect=[None, RuntimeError("app restarted")]):
            with self.assertRaises(RuntimeError):
                producer.create_snapshot(self.source, destination_root=self.output, keys_path=self.keyfile)
        with patch.object(producer, "require_quit", side_effect=[None, None, RuntimeError("app restarted")]):
            with self.assertRaises(RuntimeError):
                producer.create_snapshot(self.source, destination_root=self.output, keys_path=self.keyfile)
        with patch.object(producer, "normalize_copy", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.refresh()
        self.assertEqual(self.snapshot_count(), 1)
        self.assertTrue(previous.exists())
        self.assertFalse(list(self.output.glob(".staging-*")))

    def test_linked_sources_keys_and_plaintext_only_snapshots_are_rejected(self):
        path = self.refresh()
        db = path / "encrypted" / self.records[-1]["database"]
        db.unlink()
        db.symlink_to(self.source / self.records[-1]["database"])
        with self.assertRaises(ValueError):
            self.load(path)
        self.keyfile.chmod(0o644)
        with self.assertRaises(ValueError):
            load_keys(self.keyfile)
        self.keyfile.chmod(0o600)
        other = self.base / "plaintext-only"
        other.mkdir()
        (other / "receipt.json").write_text(json.dumps({
            "format": producer.FORMAT, "created_at": "2026-09-09T00:00:00+00:00",
            "databases": [{"database": self.records[0]["database"], "integrity_check": "ok"}]}))
        with self.assertRaises(ValueError):
            self.load(other)

    def test_secret_transport_timeouts_and_text_injection(self):
        path = self.refresh()
        db, _, key = read_chat.shards(self.load(path))[0]
        run = cipher_db.subprocess.run
        calls = []
        def record_call(args, **kwargs):
            calls.append((args, kwargs))
            return run(args, **kwargs)
        malicious = "'\n.shell echo should-not-run\n'"
        with patch.object(cipher_db.subprocess, "run", side_effect=record_call):
            self.assertEqual(query(db, key, f"SELECT {sql_text(malicious)} AS value;")[0]["value"], malicious)
        args, options = calls[0]
        self.assertNotIn(key.hex(), " ".join(args))
        self.assertIn(key.hex(), options["input"])
        self.assertIn("-noinit", args)
        self.assertIn("-readonly", args)
        self.assertEqual(options["timeout"], 30)
        with patch.object(cipher_db.subprocess, "run", side_effect=subprocess.TimeoutExpired("cmd", 30, output=key.hex())):
            with self.assertRaises(ConnectorError) as caught:
                query(db, key, "SELECT 1;")
        self.assertNotIn(key.hex(), str(caught.exception))
        with patch.object(cipher_db.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, "", key.hex())):
            with self.assertRaises(ConnectorError) as caught:
                query(db, key, "SELECT 1;")
        self.assertNotIn(key.hex(), str(caught.exception))
        for record in self.records:
            self.assertNotIn(record["key"], (path / "receipt.json").read_text())

    def test_runtime_has_no_capture_dependency(self):
        self.assertIsNone(importlib.util.find_spec("frida"))
        result = subprocess.run([os.sys.executable, str(Path(__file__).resolve().parents[1] / "capture_keys.py")], capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("disabled", result.stderr)


if __name__ == "__main__":
    unittest.main()
