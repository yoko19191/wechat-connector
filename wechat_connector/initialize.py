"""Human-invoked initialization and explicit import of existing local artifacts."""

import importlib.util
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile

from .cipher_db import (KEYS, LOCAL, SNAPSHOTS, check_key, checked_file, fingerprint,
                       load_keys, save_keys, validate)
from .doctor import DEFAULT_ROOT
from .errors import ConnectorError
from .read_chat import load_snapshot
from .snapshot import FORMAT, create_snapshot, require_quit, source_state

APP = Path("/Applications/WeChat.app")


def private_directory(path):
    path = Path(path).absolute()
    if path.resolve() != path:
        raise ConnectorError("INIT_INVALID", "Private data directory may not contain symlinks.")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.stat().st_uid != os.getuid():
        raise ConnectorError("INIT_INVALID", "Private data directory must be owned by you.")
    path.chmod(0o700)
    return path


def select_account(root, account=None):
    if root.resolve() != root:
        raise ConnectorError("INIT_INVALID", "Source path may not contain symlinks.")
    accounts = []
    for directory in sorted(root.iterdir()):
        storage = directory / "db_storage"
        if os.path.lexists(storage):
            if directory.is_symlink() or storage.is_symlink():
                raise ConnectorError("INIT_INVALID", "Account directories may not be linked.")
            if storage.is_dir():
                accounts.append(directory.name)
    if account is None and len(accounts) != 1:
        raise ConnectorError("ACCOUNT_REQUIRED", "Specify --account from these local directories: " + ", ".join(accounts))
    selected = account or accounts[0]
    if selected not in accounts:
        raise ConnectorError("ACCOUNT_NOT_FOUND", "Selected account directory was not found.")
    return selected


def signature(app):
    result = subprocess.run(["codesign", "-dvv", str(app)], capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise ConnectorError("INIT_APP_INVALID", "Cannot inspect the installed WeChat signature.")
    return result.stderr


def capture_account(root, account, *, state_dir=LOCAL, prompt=input, notify=print, seconds=180):
    if platform.system() != "Darwin" or not APP.is_dir():
        raise ConnectorError("INIT_UNSUPPORTED", "Initialization requires macOS and /Applications/WeChat.app.")
    if not sys.stdin.isatty():
        raise ConnectorError("INIT_INTERACTIVE_REQUIRED", "Run init yourself in an interactive terminal.")
    if importlib.util.find_spec("frida") is None:
        raise ConnectorError("INIT_DEPENDENCY_MISSING", "Install wechat-connector with the init extra.")
    notify("Initialization re-signs a COPY of WeChat and instruments its process. Tencent may detect this; account restrictions are possible. The original app and SIP will not be modified.")
    if prompt("Accept this risk for this initialization? Type YES: ").strip() != "YES":
        raise ConnectorError("INIT_CANCELLED", "Initialization cancelled; no capture performed.")
    require_quit()
    names, _ = source_state(root, {account})
    pages = {}
    for name in names:
        with checked_file(root, name).open("rb") as stream:
            pages[name] = stream.read(4096)
    try:
        verified = subprocess.run(["codesign", "--verify", "--deep", "--strict", str(APP)],
                                  capture_output=True, timeout=60)
    except subprocess.SubprocessError:
        raise ConnectorError("INIT_APP_INVALID", "Original application signature verification failed.") from None
    if verified.returncode:
        raise ConnectorError("INIT_APP_INVALID", "Original application signature verification failed.")
    original = signature(APP)
    if "TeamIdentifier=5A4RE8SF68" not in original or "Signature=adhoc" in original:
        raise ConnectorError("INIT_APP_INVALID", "Expected the Tencent-signed original WeChat application.")
    private_directory(state_dir)
    workspace = Path(tempfile.mkdtemp(prefix="init-", dir=state_dir))
    copy = workspace / "WeChat.app"
    try:
        for args in (["cp", "-cR", str(APP), str(copy)],
                     ["codesign", "--force", "--deep", "--sign", "-", str(copy)],
                     ["codesign", "--verify", "--deep", "--strict", str(copy)]):
            result = subprocess.run(args, capture_output=True, timeout=300)
            if result.returncode:
                raise ConnectorError("INIT_PREPARE_FAILED", "Cannot prepare application copy; original app preserved.")
        notify(f"Application copy: {copy}. Sign in to the selected account only.")
        from .capture import capture_process  # Never imported by serve/read/snapshot.
        records = capture_process(copy / "Contents/MacOS/WeChat", pages, seconds=seconds, notify=notify)
        return records
    finally:
        if signature(APP) != original:
            raise ConnectorError("INIT_APP_CHANGED", "Original application signature changed during initialization; stop and inspect.")


def import_snapshot(source, *, keys_path=KEYS, destination_root=SNAPSHOTS):
    loaded = load_snapshot(Path(source).expanduser().absolute(), keys_path=keys_path)
    private_directory(destination_root)
    destination = Path(destination_root) / loaded["path"].name
    if destination.exists():
        current = load_snapshot(destination, keys_path=keys_path)
        expected = {relative: fingerprint(path) for path, relative, _ in loaded["databases"]}
        actual = {relative: fingerprint(path) for path, relative, _ in current["databases"]}
        if expected != actual or current["created_at"] != loaded["created_at"]:
            raise ConnectorError("SNAPSHOT_CONFLICT", "Existing snapshot was preserved; import differs.")
        return destination
    mask = os.umask(0o077)
    temporary = None
    try:
        temporary = Path(tempfile.mkdtemp(prefix=".staging-", dir=destination_root))
        rows = []
        for source_file, relative, key in loaded["databases"]:
            digest = fingerprint(source_file)
            target = temporary / "encrypted" / relative
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            shutil.copyfile(source_file, target)
            if digest != fingerprint(target) or digest != fingerprint(source_file):
                raise ConnectorError("SNAPSHOT_CHANGED", "Source changed while importing.")
            rows.append({"database": relative, "sha256": digest, "table_count": validate(target, key),
                         "integrity_check": "ok"})
        (temporary / "receipt.json").write_text(json.dumps({"format": FORMAT,
            "created_at": loaded["created_at"], "databases": rows}, indent=2) + "\n")
        if destination.exists():
            raise ConnectorError("SNAPSHOT_CONFLICT", "Snapshot destination appeared during import.")
        temporary.rename(destination)
        temporary = None
        return destination
    finally:
        if temporary is not None:
            shutil.rmtree(temporary)
        os.umask(mask)


def run_init(*, root=DEFAULT_ROOT, account=None, import_keys=None, snapshot_source=None,
             keys_path=KEYS, destination_root=SNAPSHOTS, state_dir=LOCAL, prompt=input, notify=print):
    if shutil.which("sqlcipher") is None:
        raise ConnectorError("SQLCIPHER_NOT_FOUND", "Install SQLCipher before initialization: brew install sqlcipher.")
    root = Path(root).expanduser().absolute()
    keys_path = Path(keys_path).expanduser().absolute()
    if os.path.lexists(keys_path):
        records = load_keys(keys_path)
        if import_keys is not None and load_keys(import_keys) != records:
            raise ConnectorError("KEYS_EXIST", "Existing keys differ; file preserved, no capture performed.")
        notify("Existing keys reused; no capture performed.")
    elif import_keys is not None:
        records = load_keys(Path(import_keys).expanduser())
        if snapshot_source is not None:
            # Validate imported material against detached databases, without touching WeChat.
            load_snapshot(snapshot_source, keys_path=Path(import_keys).expanduser())
        else:
            names, _ = source_state(root, {Path(name).parts[0] for name in records})
            if set(names) != set(records):
                raise ConnectorError("KEY_MISSING", "Imported keys do not cover the selected database set.")
            for name in names:
                check_key(checked_file(root, name), records[name])
        save_keys(records, keys_path)
        notify("Existing keys imported; no capture performed.")
    else:
        selected = select_account(root, account)
        records = capture_account(root, selected, state_dir=state_dir, prompt=prompt, notify=notify)
        names, _ = source_state(root, {selected})
        if set(names) != set(records):
            raise ConnectorError("INIT_INCOMPLETE", "Database capture is incomplete; no key file was published.")
        for relative, record in records.items():
            check_key(checked_file(root, relative), record)
        save_keys(records, keys_path)
        notify("All selected database keys authenticated and saved locally.")
    private_directory(state_dir)
    try:
        if snapshot_source is not None:
            destination = import_snapshot(snapshot_source, keys_path=keys_path, destination_root=destination_root)
        else:
            try:
                require_quit()
            except RuntimeError:
                if sys.stdin.isatty():
                    prompt("Keys are saved. Quit WeChat and the copy normally, then press Enter: ")
                require_quit()
            destination = create_snapshot(root, keys_path=keys_path, destination_root=destination_root, account=account)
    except (OSError, ValueError, RuntimeError):
        notify("Keys are saved, but no new usable snapshot was created. Run wechat-connector snapshot after resolving the error; init will reuse existing keys.")
        raise
    return {"keys_file": str(keys_path), "snapshot": str(destination), "ready": True}
