"""Shared SQLCipher file/CLI operations. No process injection or WeChat control."""

import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile

from .errors import ConnectorError

LOCAL = Path.home() / ".local/share/wechat-connector"
SNAPSHOTS = LOCAL / "snapshots"
KEYS = Path.home() / ".local/wechat-connector-keys.jsonl"
SIDECARS = ("-wal", "-shm", "-journal")


def database_name(relative):
    path = Path(relative)
    parts = path.parts
    if (path.is_absolute() or len(parts) != 4 or parts[0] in (".", "..")
            or parts[1] != "db_storage" or str(path) != relative):
        raise ValueError("Invalid database relative path")
    allowed = ((parts[2] == "contact" and parts[3] == "contact.db") or
               (parts[2] == "session" and parts[3] == "session.db") or
               (parts[2] == "message" and re.fullmatch(r"(?:biz_)?message_[0-9]+\.db", parts[3])))
    if not allowed:
        raise ValueError("Database is outside text-chat scope")
    return path


def checked_file(root, relative):
    """Reject linked paths, including ancestor links, before opening a file."""
    root = Path(root).absolute()
    relative = Path(relative)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("Path escapes configured root")
    path = root / relative
    for item in (path, *path.parents):
        if item.is_symlink():
            raise ValueError("Symlink paths are not accepted")
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError("Expected an unlinked regular file")
    return path


def check_key_parent(parent):
    parent = Path(parent).absolute()
    if parent.resolve() != parent:
        raise ConnectorError("KEYS_INVALID", "Key parent may not contain symlinks.")
    info = parent.stat()
    if info.st_uid != os.getuid() or info.st_mode & 0o022:
        raise ConnectorError("KEYS_INVALID", "Key parent must be owned by you and not group/world writable.")


def load_keys(path=KEYS):
    try:
        path = checked_file(Path(path).parent, Path(path).name)
        check_key_parent(path.parent)
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd) as stream:
            info = os.fstat(stream.fileno())
            if (info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600
                    or info.st_nlink != 1 or info.st_size > 8 * 1024 * 1024):
                raise ValueError()
            result = {}
            for line in stream:
                row = json.loads(line)
                relative = str(database_name(row["database"]))
                key, salt = bytes.fromhex(row["key"]), bytes.fromhex(row["salt"])
                if len(key) != 32 or len(salt) != 16 or relative in result:
                    raise ValueError()
                result[relative] = {"key": key, "salt": salt}
            if not result:
                raise ValueError()
            return result
    except FileNotFoundError:
        raise ConnectorError("KEYS_NOT_FOUND", "Run wechat-connector init in a terminal; MCP cannot acquire keys.") from None
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        raise ConnectorError("KEYS_INVALID", "Invalid keys, ownership or permissions; require a regular 0600 file.") from None


def save_keys(records, path=KEYS):
    """Publish all verified keys atomically, without replacing an existing file."""
    path = Path(path).absolute()
    if path.parent.resolve() != path.parent:
        raise ConnectorError("KEYS_INVALID", "Key parent may not contain symlinks.")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
    check_key_parent(path.parent)
    fd, temporary = tempfile.mkstemp(prefix=".wechat-keys-", dir=path.parent)
    temporary = Path(temporary)
    try:
        with os.fdopen(fd, "w") as stream:
            for relative, record in sorted(records.items()):
                stream.write(json.dumps({"database": relative, "key": record["key"].hex(),
                                         "salt": record["salt"].hex()}) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        load_keys(temporary)
        # Hard-link publication is atomic and fails if the destination already exists.
        os.link(temporary, path)
    except FileExistsError:
        raise ConnectorError("KEYS_EXIST", "Existing key file was preserved.") from None
    finally:
        temporary.unlink(missing_ok=True)


def authenticates(page, key):
    if len(page) != 4096 or len(key) != 32:
        return False
    salt = bytes(b ^ 0x3A for b in page[:16])
    mac_key = hashlib.pbkdf2_hmac("sha512", key, salt, 2, dklen=32)
    digest = hmac.digest(mac_key, page[16:4032] + (1).to_bytes(4, "little"), "sha512")
    return hmac.compare_digest(digest, page[4032:])


def check_key(path, record):
    with path.open("rb") as stream:
        page = stream.read(4096)
    if page[:16] != record["salt"] or not authenticates(page, record["key"]):
        raise ConnectorError("KEY_MISMATCH", "Stored key does not authenticate this database; no automatic acquisition.")


def stable_file(path):
    path = checked_file(path.parent, path.name)
    if any(os.path.lexists(str(path) + suffix) for suffix in SIDECARS):
        raise ValueError("Snapshot still has WAL/SHM/journal sidecars")
    return path


def fingerprint(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def sql_text(text):
    # Hex avoids SQL quoting mistakes and CLI dot-command injection via newlines.
    return "CAST(X'" + text.encode("utf-8").hex() + "' AS TEXT)"


def _run(path, key, sql, *, readonly=True):
    """Internal trusted SQL only; input/diagnostics must never be logged."""
    path = stable_file(path) if readonly else checked_file(path.parent, path.name)
    with path.open("rb") as stream:
        if not authenticates(stream.read(4096), key):
            raise ValueError("Encrypted page authentication failed")
    args = ["sqlcipher", "-noinit", "-batch", "-bail", "-nofollow", "-ifexists"]
    if readonly:
        args += ["-readonly", path.as_uri() + "?mode=ro&immutable=1"]
    else:
        args += [str(path)]
    setup = (f'PRAGMA key="x\'{key.hex()}\'";\n'
             "PRAGMA cipher_compatibility=4;\nPRAGMA temp_store=MEMORY;\n"
             "PRAGMA trusted_schema=OFF;\n")
    if readonly:
        setup += "PRAGMA query_only=ON;\n"
    try:
        result = subprocess.run(args, input=setup + ".mode json\n" + sql + "\n",
                                text=True, capture_output=True, timeout=30)
    except FileNotFoundError:
        raise ConnectorError("SQLCIPHER_NOT_FOUND", "Install SQLCipher and check the MCP process PATH.") from None
    except subprocess.TimeoutExpired:
        raise ConnectorError("QUERY_TIMEOUT", "The read exceeded 30 seconds; retry with a smaller time range.") from None
    except (OSError, subprocess.SubprocessError):
        raise ConnectorError("DATABASE_READ_FAILED", "SQLCipher could not run; check file and process permissions.") from None
    if result.returncode or result.stderr or not result.stdout.startswith("ok\n"):
        raise ConnectorError("DATABASE_READ_FAILED", "SQLCipher read or validation failed; private diagnostics withheld.")
    data = result.stdout[3:].strip()
    sets = []
    try:
        decoder = json.JSONDecoder()
        while data:
            rows, end = decoder.raw_decode(data)
            if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
                raise ValueError()
            sets.append(rows)
            data = data[end:].strip()
    except ValueError:
        raise ConnectorError("DATABASE_READ_FAILED", "Invalid SQLCipher response; private diagnostics withheld.") from None
    return sets


def query(path, key, sql):
    sets = _run(path, key, sql)
    if len(sets) > 1:
        raise ValueError("Expected a single query result")
    return sets[0] if sets else []


def validate(path, key):
    sets = _run(path, key, "PRAGMA cipher_integrity_check;\nPRAGMA integrity_check;\n"
                "SELECT count(*) AS n FROM sqlite_schema WHERE type='table';")
    # A successful cipher_integrity_check returns no rows; errors add a result set.
    if (len(sets) != 2 or sets[0] != [{"integrity_check": "ok"}]
            or len(sets[1]) != 1 or not isinstance(sets[1][0].get("n"), int)):
        raise ValueError("Snapshot failed page authentication or SQLite integrity check")
    return sets[1][0]["n"]


def normalize_copy(path, key):
    """Only call on a newly created private copy, never on WeChat source files."""
    sets = _run(path, key, "PRAGMA wal_checkpoint(TRUNCATE);\nPRAGMA journal_mode=DELETE;",
                readonly=False)
    if (len(sets) != 2 or not sets[0] or sets[0][0].get("busy") != 0
            or sets[1] != [{"journal_mode": "delete"}]):
        raise ValueError("Private-copy WAL checkpoint did not complete")
    stable_file(path)
    return validate(path, key)
