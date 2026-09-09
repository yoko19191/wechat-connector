"""Run with python3 test_crypto.py; needs sqlcipher, uses synthetic data only."""

from pathlib import Path
import sqlite3
import subprocess
from tempfile import TemporaryDirectory

from capture_keys import authenticates
from decrypt_snapshot import export


with TemporaryDirectory() as directory:
    source = Path(directory) / "synthetic.db"
    output = Path(directory) / "plaintext.db"
    key = bytes(range(32))
    subprocess.run(["sqlcipher", str(source)], input=(
        f'.bail on\nPRAGMA key="x\'{key.hex()}\'";\n'
        "CREATE TABLE messages(id INTEGER PRIMARY KEY, text TEXT);\n"
        "INSERT INTO messages(text) VALUES ('synthetic only');\n"),
        capture_output=True, text=True, check=True)
    page = source.read_bytes()[:4096]
    assert authenticates(page, key)
    assert not authenticates(page, bytes(32))
    damaged = bytearray(page)
    damaged[123] ^= 1
    assert not authenticates(bytes(damaged), key)
    assert export(source, output, key) == 1
    with sqlite3.connect(output.as_uri() + "?mode=ro", uri=True) as conn:
        assert conn.execute("select text from messages").fetchone() == ("synthetic only",)
    try:
        export(source, output, key)
    except FileExistsError:
        pass
    else:
        raise AssertionError("Existing plaintext must not be overwritten")
print("PASS: real SQLCipher HMAC, wrong key, corruption, export, row read, no overwrite")
