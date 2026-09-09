"""Optional initialization-only capture bridge; never imported by the MCP server."""

import threading
from pathlib import Path

from .cipher_db import authenticates
from .errors import ConnectorError

HOOK = r"""
rpc.exports.arm = function(salts) {
    const allowed = new Set(salts);
    const hex = p => Array.from(new Uint8Array(p.readByteArray(16)),
                               x => x.toString(16).padStart(2, '0')).join('');
    Interceptor.attach(Module.getGlobalExportByName('CCKeyDerivationPBKDF'), {
        onEnter(args) {
            this.match = false;
            if (args[4].toUInt32() !== 16 || args[8].toUInt32() !== 32) return;
            this.salt = hex(args[3]);
            if (!allowed.has(this.salt)) return;
            this.output = args[7];
            this.match = true;
        },
        onLeave(result) {
            if (this.match && result.toInt32() === 0)
                send({salt: this.salt}, this.output.readByteArray(32));
        }
    });
    return true;
};
"""


def capture_process(binary, pages, *, seconds=180, notify=print):
    try:
        import frida
    except ImportError:
        raise ConnectorError("INIT_DEPENDENCY_MISSING", "Install the init extra before initialization.") from None
    if not pages or not 1 <= seconds <= 600:
        raise ConnectorError("INIT_INVALID", "No capture targets or invalid timeout.")
    by_salt = {}
    for relative, page in pages.items():
        if len(page) != 4096 or page.startswith(b"SQLite format 3\0"):
            raise ConnectorError("INIT_UNSUPPORTED", "Expected SQLCipher 4 database pages.")
        by_salt.setdefault(page[:16].hex(), []).append(relative)
    captured = {}
    done = threading.Event()
    errors = []

    def receive(message, data):
        if message.get("type") == "error":
            errors.append(True)
            done.set()
            return
        if message.get("type") != "send" or not isinstance(data, bytes) or len(data) != 32:
            return
        for relative in by_salt.get(message.get("payload", {}).get("salt"), []):
            if relative not in captured and authenticates(pages[relative], data):
                captured[relative] = {"key": data, "salt": pages[relative][:16]}
                notify(f"Authenticated database {len(captured)}/{len(pages)}: {Path(relative).name}")
        if len(captured) == len(pages):
            done.set()

    device = frida.get_local_device()
    pid = session = None
    resumed = False
    try:
        # Pipe application output so native logs cannot spill into the host terminal.
        pid = device.spawn([str(binary)], stdio="pipe")
        session = device.attach(pid)
        script = session.create_script(HOOK)
        script.on("message", receive)
        script.load()
        if not script.exports_sync.arm(list(by_salt)):
            raise RuntimeError()
        device.resume(pid)
        resumed = True
        notify("Capture ready. Complete login in the application copy if prompted.")
        done.wait(seconds)
        if errors or len(captured) != len(pages):
            raise ConnectorError("INIT_INCOMPLETE", "Capture incomplete; no key file was published. Quit the copy normally.")
        return captured
    except ConnectorError:
        raise
    except Exception:
        raise ConnectorError("INIT_CAPTURE_FAILED", "Capture failed; no key file was published. Check OS permissions and quit the copy.") from None
    finally:
        if session is not None:
            try:
                session.detach()
            except Exception:
                pass  # A process that already exited is already detached.
        if pid is not None and not resumed:
            try:
                device.kill(pid)
            except Exception:
                pass
        # A resumed application is left for the user to quit, never force-killed.
