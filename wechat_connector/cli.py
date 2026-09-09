"""Installed command entry point; initialize/serve imports remain separate."""

import argparse
import json
import subprocess
from pathlib import Path

from .errors import ConnectorError


def main():
    parser = argparse.ArgumentParser(prog="wechat-connector")
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="Human-operated initialization or import existing keys")
    init.add_argument("--root", type=Path)
    init.add_argument("--account")
    init.add_argument("--import-keys", type=Path)
    init.add_argument("--import-snapshot", type=Path, help="Import only the encrypted branch of a verified snapshot")
    commands.add_parser("serve", help="Run two local read-only MCP tools over stdio")
    for name in ("snapshot", "read", "doctor"):
        commands.add_parser(name, add_help=False)
    args, extra = parser.parse_known_args()
    try:
        if args.command == "init":
            if extra:
                parser.error("Unexpected initialization arguments")
            from .initialize import run_init
            options = {"account": args.account, "import_keys": args.import_keys,
                       "snapshot_source": args.import_snapshot}
            if args.root is not None:
                options["root"] = args.root
            print(json.dumps(run_init(**options), ensure_ascii=False))
        elif args.command == "serve":
            if extra:
                parser.error("serve accepts no tool-controlled paths or initialization options")
            from .server import create_server
            create_server().run(transport="stdio")
        elif args.command == "snapshot":
            from .snapshot import cli
            cli(extra)
        elif args.command == "read":
            from .read_chat import main as read
            read(extra)
        else:
            from .doctor import main as doctor
            raise SystemExit(doctor(extra))
    except ConnectorError as exc:
        parser.exit(2, str(exc) + "\n")
    except KeyboardInterrupt:
        parser.exit(130, "Interrupted. No automatic key retry; check the application copy and quit it normally.\n")
    except (OSError, ValueError, RuntimeError, EOFError, subprocess.SubprocessError):
        parser.exit(2, "Operation failed; private diagnostics withheld. Existing keys and published snapshots were preserved.\n")


if __name__ == "__main__":
    main()
