#!/usr/bin/env python3
"""Capture chat database keys from the prepared app copy; never print secrets."""

import argparse
import hashlib
import hmac
import json
import os
from pathlib import Path
import subprocess
import threading

from doctor import DEFAULT_ROOT, SQLITE_HEADER


APP = Path(__file__).resolve().parent / ".local/WeChat.app"
HOOK = r"""
rpc.exports.arm = function(salts) {
    const allowed = new Set(salts);
    const hex = p => Array.from(new Uint8Array(p.readByteArray(16)),
                               x => x.toString(16).padStart(2, '0')).join('');
    const address = Module.getGlobalExportByName('CCKeyDerivationPBKDF');
    Interceptor.attach(address, {
        onEnter(args) {
            this.match = false;
            if (args[4].toUInt32() !== 16 || args[8].toUInt32() !== 32) return;
            this.salt = hex(args[3]);
            if (!allowed.has(this.salt)) return;
            this.match = true;
            this.output = args[7];
        },
        onLeave(retval) {
            if (this.match && retval.toInt32() === 0)
                send({salt: this.salt}, this.output.readByteArray(32));
        }
    });
    return true;
};
"""


def authenticates(page: bytes, key: bytes) -> bool:
    """SQLCipher 4 default first-page HMAC, including little-endian page number."""
    if len(page) != 4096 or len(key) != 32:
        return False
    salt = bytes(b ^ 0x3A for b in page[:16])
    mac_key = hashlib.pbkdf2_hmac("sha512", key, salt, 2, dklen=32)
    digest = hmac.digest(mac_key, page[16:4032] + (1).to_bytes(4, "little"), "sha512")
    return hmac.compare_digest(digest, page[4032:])


def main():
    import frida

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--seconds", type=int, default=180)
    args = parser.parse_args()
    if not 1 <= args.seconds <= 600:
        parser.error("--seconds must be 1..600")
    root = args.root.expanduser().resolve(strict=True)
    if subprocess.run(["pgrep", "-x", "WeChat"], capture_output=True).returncode == 0:
        parser.error("Quit WeChat normally before starting the prepared copy.")
    binary = APP / "Contents/MacOS/WeChat"
    if not binary.is_file() or APP.is_symlink():
        parser.error("Prepared .local/WeChat.app is missing or is a symlink.")
    # Only metadata and text-chat shards; no Moments, Favorites or media keys.
    targets = {}
    for storage in sorted(root.glob("*/db_storage")):
        paths = [storage / "contact/contact.db", storage / "session/session.db"]
        paths += sorted((storage / "message").glob("message_[0-9]*.db"))
        paths += sorted((storage / "message").glob("biz_message_[0-9]*.db"))
        for path in paths:
            if path.is_symlink() or not path.resolve().is_relative_to(root):
                continue
            with path.open("rb") as stream:
                page = stream.read(4096)
            if len(page) == 4096 and page[:16] != SQLITE_HEADER:
                targets.setdefault(page[:16].hex(), []).append((str(path.relative_to(root)), page))
    if not targets:
        parser.error("No candidate chat databases found.")
    private = APP.parent
    os.chmod(private, 0o700)
    keyfile = private / "captured-keys.jsonl"
    fd = os.open(keyfile, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    captured = set()
    done = threading.Event()
    errors = []
    device = frida.get_local_device()
    pid = session = None
    resumed = False
    with os.fdopen(fd, "w") as output:
        def on_message(message, data):
            if message.get("type") == "error":
                errors.append("Frida hook failed")
                done.set()
                return
            if message.get("type") != "send" or not data:
                return
            salt = message.get("payload", {}).get("salt")
            for relative, page in targets.get(salt, []):
                if relative in captured or not authenticates(page, data):
                    continue
                output.write(json.dumps({"database": relative, "salt": salt,
                                         "key": data.hex()}) + "\n")
                output.flush()
                os.fsync(output.fileno())
                captured.add(relative)
                print(f"Authenticated key {len(captured)}: {Path(relative).name}", flush=True)
            if len(captured) == sum(map(len, targets.values())):
                done.set()

        try:
            pid = device.spawn([str(binary)])
            session = device.attach(pid)
            script = session.create_script(HOOK)
            script.on("message", on_message)
            script.load()
            if not script.exports_sync.arm(list(targets)):
                raise RuntimeError("Hook was not installed")
            device.resume(pid)
            resumed = True
            print(f"Hook ready; copy PID {pid}. Complete login in the copy if prompted.", flush=True)
            done.wait(args.seconds)
        finally:
            if session is not None:
                session.detach()
            if pid is not None and not resumed:
                device.kill(pid)
            # A resumed app is left open for normal quit, never force-killed.
    print(f"Saved {len(captured)} authenticated keys in private .local storage.")
    if errors:
        raise RuntimeError(errors[0])
    return 0 if len(captured) == sum(map(len, targets.values())) else 2


if __name__ == "__main__":
    raise SystemExit(main())
