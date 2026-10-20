#!/usr/bin/env python3
"""Start a dedicated graphical Writer session with a localhost UNO listener."""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

SOFFICE = Path("/usr/bin/soffice")


def wait_listener(host: str, port: int, process: subprocess.Popen, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            return False
        try:
            with socket.create_connection((host, port), timeout=0.25):
                return True
        except OSError:
            time.sleep(0.15)
    return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=int(os.environ.get("YBERU_WRITER_PORT", "2002")))
    parser.add_argument("--profile", type=Path, help="isolated profile directory (defaults to one profile per port)")
    parser.add_argument("--wait", type=float, default=0, metavar="SECONDS", help="wait for the listener before returning")
    parser.add_argument("--json", action="store_true", help="print startup details as JSON")
    args = parser.parse_args(argv)
    if not SOFFICE.is_file() or not os.access(SOFFICE, os.X_OK):
        parser.error("/usr/bin/soffice is not executable")
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    try:
        with socket.socket() as port_probe:
            port_probe.bind(("127.0.0.1", args.port))
    except OSError:
        parser.error(f"127.0.0.1:{args.port} is already in use; refusing to connect to another session")
    if args.wait < 0:
        parser.error("--wait cannot be negative")
    default_profile_root = Path.home() / ".local/share/yberu-writer-mcp"
    profile = (args.profile or default_profile_root / f"profile-{args.port}").expanduser().resolve()
    system_profile = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")).expanduser().resolve() / "libreoffice/4"
    if profile in {system_profile, system_profile / "user"}:
        parser.error("refusing to use the default LibreOffice user profile; choose a dedicated profile path")
    if any((profile / relative).exists() for relative in (Path(".lock"), Path("user/.lock"))):
        parser.error("the selected LibreOffice profile appears to be in use; choose an isolated profile")
    profile.mkdir(parents=True, exist_ok=True)
    log_path = profile / "yberu-writer.log"
    log_stream = log_path.open("ab", buffering=0)
    command = [
        str(SOFFICE), "--writer", "--norestore", "--nofirststartwizard",
        f"--accept=socket,host=127.0.0.1,port={args.port};urp;StarOffice.ServiceManager",
        f"-env:UserInstallation={profile.as_uri()}",
    ]
    # A dedicated profile ensures this cannot silently forward to the user's ordinary session.
    # This process is intentionally detached from MCP lifetime; stopping MCP never closes Writer.
    process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log_stream, stderr=subprocess.STDOUT,
                               env=os.environ.copy(), start_new_session=True, close_fds=True)
    log_stream.close()
    ready = wait_listener("127.0.0.1", args.port, process, args.wait) if args.wait else None
    result = {"pid": process.pid, "port": args.port, "host": "127.0.0.1", "profile": str(profile),
              "soffice": str(SOFFICE), "log": str(log_path), "listener_ready": ready}
    print(json.dumps(result) if args.json else f"Writer launched (PID {process.pid}), profile {profile}, listener 127.0.0.1:{args.port}.")
    if ready is False:
        print("Writer listener did not become ready; inspect the launcher log.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
