#!/usr/bin/env python3
"""Snapshot all durable conversations on a pane's verified local Codex server.

A shared server does not expose a TUI-to-thread mapping. Cover its whole live
inventory instead of guessing which writer belongs to a pane. Never start a
server, subscribe to a conversation, send a turn, or change pane metadata.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import runpy
import socket
import stat
import struct
import subprocess
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
UUID = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\Z")
MAX_MESSAGE = 4 * 1024 * 1024


def server_process(pid: int) -> bool:
    proc = Path(os.environ.get("CODEX_PROC_ROOT", "/proc")) / str(pid)
    try:
        if proc.stat().st_uid != os.getuid():
            return False
        argv = [value.decode() for value in (proc / "cmdline").read_bytes().split(b"\0") if value]
    except (OSError, UnicodeDecodeError):
        return False
    hook = runpy.run_path(str(SCRIPT_DIR / "codex-session-hook.py"))
    arguments = hook["codex_arguments"](["codex", *argv])
    return arguments is not None and hook["is_codex_app_server"](arguments)


def shared_endpoint(pid: int) -> str:
    """Match reciprocal kernel socket peers, not inherited argv, cwd or metadata."""
    result = subprocess.run(["ss", "-xnpH"], capture_output=True, text=True, check=False, timeout=5)
    if result.returncode:
        raise ValueError("local socket ownership could not be inspected")
    sockets = []
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) < 9 or fields[:2] != ["u_str", "ESTAB"]:
            continue
        if not fields[5].isdigit() or not fields[7].isdigit():
            continue
        owners = {int(value) for value in re.findall(r"\bpid=(\d+),", " ".join(fields[8:]))}
        sockets.append((fields[4], fields[5], fields[7], owners))
    endpoints = set()
    for _path, inode, peer, owners in sockets:
        if pid not in owners:
            continue
        for path, other_inode, other_peer, other_owners in sockets:
            if other_inode != peer or other_peer != inode or not path.startswith("/"):
                continue
            if any(server_process(owner) for owner in other_owners):
                endpoints.add(path)
    if len(endpoints) != 1:
        raise ValueError("pane has no unique verified local shared-server connection")
    return endpoints.pop()


class Client:
    """A bounded, read-only JSON-RPC client over the local Unix WebSocket."""

    def __init__(self, endpoint: str):
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.socket.settimeout(5)
        self.deadline = time.monotonic() + 20
        self.sequence = 0
        try:
            metadata = Path(endpoint).stat()
            if not stat.S_ISSOCK(metadata.st_mode) or metadata.st_uid != os.getuid():
                raise ValueError("server socket is not owned by the current user")
            self.socket.connect(endpoint)
            peer = self.socket.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
            pid, uid, _gid = struct.unpack("3i", peer)
            if uid != os.getuid() or not server_process(pid):
                raise ValueError("socket peer is not a local Codex app server")
            key = base64.b64encode(os.urandom(16)).decode()
            self.socket.sendall(
                (
                    "GET / HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\n"
                    f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
                    "Sec-WebSocket-Version: 13\r\n\r\n"
                ).encode()
            )
            header = b""
            while not header.endswith(b"\r\n\r\n"):
                header += self.read(1)
                if len(header) > 16384:
                    raise ValueError("invalid server handshake")
            accept = base64.b64encode(
                hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()
            ).decode()
            headers = header.decode("ascii").split("\r\n")
            values = {
                name.lower(): value.strip()
                for line in headers[1:]
                if ":" in line
                for name, value in [line.split(":", 1)]
            }
            if " 101 " not in headers[0] or values.get("sec-websocket-accept") != accept:
                raise ValueError("invalid server handshake")
            self.request(
                "initialize",
                {
                    "clientInfo": {"name": "codexfarm-recovery", "version": "1"},
                    "capabilities": {"experimentalApi": True},
                },
            )
            self.send({"method": "initialized", "params": {}})
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        self.socket.close()

    def read(self, count: int) -> bytes:
        data = b""
        while len(data) < count:
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("server inventory timed out")
            self.socket.settimeout(min(5, remaining))
            chunk = self.socket.recv(count - len(data))
            if not chunk:
                raise ValueError("server connection closed")
            data += chunk
        return data

    def frame(self, opcode: int, data: bytes) -> None:
        mask = os.urandom(4)
        size = len(data)
        header = bytes([0x80 | opcode])
        if size < 126:
            header += bytes([0x80 | size])
        elif size < 65536:
            header += b"\xfe" + struct.pack("!H", size)
        else:
            header += b"\xff" + struct.pack("!Q", size)
        self.socket.sendall(header + mask + bytes(c ^ mask[i % 4] for i, c in enumerate(data)))

    def send(self, value: dict) -> None:
        self.frame(1, json.dumps(value).encode())

    def receive(self) -> dict:
        message = b""
        started = False
        while True:
            flags, length = self.read(2)
            opcode = flags & 15
            size = length & 127
            if size == 126:
                size = struct.unpack("!H", self.read(2))[0]
            elif size == 127:
                size = struct.unpack("!Q", self.read(8))[0]
            if flags & 0x70 or length & 0x80 or size + len(message) > MAX_MESSAGE:
                raise ValueError("invalid server frame")
            data = self.read(size)
            if opcode == 9 and flags & 0x80 and size <= 125:
                self.frame(10, data)
                continue
            if opcode == 10 and flags & 0x80 and size <= 125:
                continue
            if (opcode == 1 and not started) or (opcode == 0 and started):
                started = True
                message += data
                if flags & 0x80:
                    value = json.loads(message)
                    if not isinstance(value, dict):
                        raise ValueError("invalid server response")
                    return value
            else:
                raise ValueError("unexpected server frame")

    def request(self, method: str, params: dict) -> dict:
        if method not in {"initialize", "thread/loaded/list", "thread/read"}:
            raise ValueError("unsupported recovery request")
        self.sequence += 1
        self.send({"id": self.sequence, "method": method, "params": params})
        while True:
            value = self.receive()
            if value.get("id") != self.sequence:
                continue
            if "error" in value or not isinstance(value.get("result"), dict):
                raise ValueError("server rejected recovery metadata request")
            return value["result"]

    def loaded(self) -> set[str]:
        ids: set[str] = set()
        cursor = None
        cursors = set()
        while True:
            result = self.request("thread/loaded/list", {"cursor": cursor, "limit": 100})
            data = result.get("data")
            if not isinstance(data, list) or any(
                not isinstance(value, str) or not UUID.fullmatch(value) for value in data
            ):
                raise ValueError("invalid server conversation inventory")
            ids.update(value.lower() for value in data)
            cursor = result.get("nextCursor")
            if cursor is None:
                return ids
            if not isinstance(cursor, str) or not cursor or cursor in cursors:
                raise ValueError("invalid server inventory cursor")
            cursors.add(cursor)


def snapshot(endpoint: str, manifest: Path | None) -> list[tuple[str, str, str, str]]:
    validator = runpy.run_path(str(SCRIPT_DIR / "codex-manifest.py"))
    previous = {}
    if manifest is not None and manifest.is_file():
        try:
            previous = {
                args.split()[1]: name
                for name, _directory, command, args in validator["parse_manifest"](
                    manifest.read_bytes()
                )
                if command == "codex"
            }
        except ValueError:
            pass
    client = Client(endpoint)
    try:
        loaded = client.loaded()
        rows = {}
        for thread_id in sorted(loaded):
            thread = client.request(
                "thread/read", {"threadId": thread_id, "includeTurns": False}
            ).get("thread")
            if not isinstance(thread, dict) or thread.get("id", "").lower() != thread_id:
                raise ValueError("invalid server thread metadata")
            source = thread.get("source")
            is_child = isinstance(source, dict) and any(key.lower() == "subagent" for key in source)
            if is_child:
                root = thread.get("sessionId")
                if not isinstance(root, str) or root.lower() not in loaded:
                    raise ValueError("child conversation has no loaded recovery root")
                continue
            if thread.get("ephemeral") is not False:
                raise ValueError("server contains a conversation without durable restore data")
            directory = thread.get("cwd")
            if not isinstance(directory, str) or not directory.startswith("/"):
                raise ValueError("invalid server conversation directory")
            name = previous.get(thread_id, f"shared-codex-{thread_id}")
            rows[thread_id] = (name, directory, "codex", f"resume {thread_id}")
        if client.loaded() - loaded:
            raise ValueError("server conversations changed during capture; retry save")
        return validator["parse_manifest"](validator["serialize"](list(rows.values())))
    finally:
        client.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    endpoint = commands.add_parser("endpoint")
    endpoint.add_argument("pid", type=int)
    capture = commands.add_parser("snapshot")
    capture.add_argument("endpoint")
    capture.add_argument("--manifest", type=Path)
    verify = commands.add_parser("verify")
    verify.add_argument("endpoint")
    verify.add_argument("manifest", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "endpoint":
            if args.pid <= 0:
                raise ValueError("invalid provider process")
            print(shared_endpoint(args.pid))
        else:
            rows = snapshot(args.endpoint, args.manifest)
            validator = runpy.run_path(str(SCRIPT_DIR / "codex-manifest.py"))
            if args.command == "verify":
                saved = validator["identities"](
                    validator["parse_manifest"](args.manifest.read_bytes())
                )
                if validator["identities"](rows) - saved:
                    raise ValueError(
                        "live shared-server conversations are missing from the manifest"
                    )
            else:
                sys.stdout.buffer.write(validator["serialize"](rows))
    except (OSError, ValueError, subprocess.SubprocessError, AttributeError, TypeError):
        # Server replies and thread names can contain private conversation IDs.
        print(
            "Local shared-server recovery could not be verified; previous snapshot preserved.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
