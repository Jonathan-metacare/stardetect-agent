#!/usr/bin/env python3
"""Minimal local receiver for the SpaceZenith APP telemetry Unix socket.

It accepts the fixed 1066-byte InnerTeleFrame emitted by app_1 and prints only
the frame metadata/status. It is a local validation tool, not flight-platform
software.
"""

from __future__ import annotations

import argparse
import signal
import socket
import stat
import struct
import sys
from pathlib import Path


FRAME_SIZE = 1066
STATUS_NAMES = {
    0x00: "running",
    0x01: "success",
    0x02: "failure",
    0x03: "cancelled",
}
stopping = False


def request_stop(_signum: int, _frame: object) -> None:
    global stopping
    stopping = True


def remove_stale_socket(path: Path) -> None:
    try:
        details = path.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISSOCK(details.st_mode):
        raise RuntimeError(f"refusing to replace non-socket path: {path}")
    path.unlink()


def handle_connection(connection: socket.socket, exit_on_final: bool) -> bool:
    pending = b""
    while not stopping:
        data = connection.recv(4096)
        if not data:
            return False
        pending += data
        while len(pending) >= FRAME_SIZE:
            frame, pending = pending[:FRAME_SIZE], pending[FRAME_SIZE:]
            source_device, command, payload_length = struct.unpack_from("<BBI", frame)
            status = frame[6]
            status_name = STATUS_NAMES.get(status, f"unknown(0x{status:02x})")
            print(
                "telemetry"
                f" device={source_device}"
                f" command={command}"
                f" payload_length={payload_length}"
                f" status={status_name}",
                flush=True,
            )
            if exit_on_final and status in {0x01, 0x02, 0x03}:
                return True
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description="Receive local app_1 telemetry frames.")
    parser.add_argument("path", nargs="?", default="/tmp/app1-telemetry.sock")
    parser.add_argument(
        "--exit-on-final",
        action="store_true",
        help="exit after success, failure, or cancelled status",
    )
    args = parser.parse_args()
    path = Path(args.path)
    path.parent.mkdir(parents=True, exist_ok=True)
    remove_stale_socket(path)

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        server.bind(str(path))
        server.listen()
        server.settimeout(0.5)
        print(f"listening on {path}", flush=True)
        while not stopping:
            try:
                connection, _ = server.accept()
            except TimeoutError:
                continue
            with connection:
                if handle_connection(connection, args.exit_on_final):
                    return 0
        return 0
    finally:
        server.close()
        try:
            remove_stale_socket(path)
        except RuntimeError:
            pass


if __name__ == "__main__":
    sys.exit(main())
